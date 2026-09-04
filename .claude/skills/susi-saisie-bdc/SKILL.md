---
name: susi-saisie-bdc
description: Traite un lot de bons de commande papier Atlas For Men de bout en bout — rendu du scan, lecture vision par sous-agents bdc-lecteur, consolidation, revue humaine, saisie dans SUSI (« Saisie commande rapide ») par susi-saisisseur, puis vérification post-saisie par susi-verificateur. Déclencher sur « traite mes bdc », « saisie des bons de commande », « lot BDC », « /susi-saisie-bdc », ou quand l'utilisateur glisse un PDF de liasse Atlas For Men.
---

# Saisie des BDC papier dans SUSI

Projet : la racine du dépôt `susi-bdc`. Plateforme web équivalente : `app/app.py` sur le
port 8760. **Cette skill est la spec de référence** : les prompts des jobs de la plateforme
y renvoient, et un écart entre l'app et cette skill est toujours un bug de l'app.

À lire avant tout : **`manuel-commandes-rapides.md`** à la racine. Tout le pipeline en
découle, y compris les cas particuliers de la page 16.

L'état vit **entièrement sur disque**, dans `lots/<LOT>/`. Il n'y a aucune base de données.
Une session Claude Code et la plateforme web lisent et écrivent les mêmes fichiers : un lot
commencé d'un côté se poursuit de l'autre.

## Phase 1 — accueillir le lot

Le nom du fichier porte les métadonnées :

```
03092026FR ATLAS FID 50CH1.pdf
│       │        │   │ │ │
│       │        │   │ │ └─ liasse n° 1
│       │        │   │ └─── CH = chèque joint (BDC = bons seuls)
│       │        │   └───── 50 commandes
│       │        └───────── FID = client connu · REC/RECRUT = nouveau client
│       └────────────────── pays
└────────────────────────── date du lot
```

Variantes réellement présentes sur le SFTP : le `III` de campagne est optionnel, le type
s'écrit `FID`, `REC` ou `RECRUT`, le paiement peut être absent (chèque), `OA` (sans
paiement), `CB`, `C3M` ou `SANS PAIEMENTS`, le compteur s'écrit `1` ou `001` — et on
rencontre la coquille `8DC` pour `8BDC`.

Crée `lots/<NOM_NORMALISÉ>/`, copie le PDF, écris `meta.json`, puis rends les pages :

```sh
pdftoppm -jpeg -r 150 "<le.pdf>" "lots/<LOT>/pages/p"
```

**Renomme ensuite systématiquement en `p-NNN.jpg`** : `pdftoppm` cale le zéro-padding sur
le nombre de pages (4 pages → `p-1.jpg`, 100 pages → `p-001.jpg`), et sans normalisation le
tri lexicographique casse.

Dans un lot `CH`, **pages impaires = chèques, pages paires = BDC**. La commande `NN` occupe
donc les pages `2·NN-1` et `2·NN`.

## Phase 2 — lecture vision

L'OCR des scans est inutilisable : le BDC est manuscrit. Tout passe par la vision.

Lance des sous-agents `bdc-lecteur` (outil Agent, `subagent_type: "bdc-lecteur"`,
`run_in_background: true`), **~5 commandes par exemplaire, jusqu'à 10 en parallèle**.
Chacun reçoit le dossier du lot et sa plage de commandes, et écrit un
`extraits/cmd_NN.json` par commande.

Compter environ 16 minutes pour 50 commandes avec 10 lecteurs.

## Phase 3 — consolidation et contrôles

Rassemble les extraits dans `lot.json` et écris le tableau de contrôle `recap.md`.

Contrôles à faire ici, pas plus tard :

1. **Arithmétique** — pour chaque commande, `Σ(prix × quantité) + frais de port = total`.
2. **Chèque contre total** — un écart est un signal fort, pas un arrondi.
3. **Doublons de scan** — compare les lignes **CMC7** et les n° clients entre commandes du
   lot. Un même chèque vu deux fois est un double passage scanner, pas une seconde
   commande : marque-la `ecarte`.
4. **Refco au catalogue** — croise chaque refco avec le référentiel (atlasformen.fr ou
   export SUSI). Cas déjà vu : `KB179` lu pour `K8179`.
5. **Total du lot** — le cumul doit s'équilibrer avec la somme des chèques.

## Phase 4 — revue humaine (point de passage obligatoire)

Le score de confiance part de 100 et retranche : arithmétique fausse −40, chèque ≠ total
−40, refco illisible −25, confiance d'article basse/moyenne −20/−10, anomalies −5 chacune
(plafond −20).

| Score | Sens | Geste |
|---|---|---|
| ≥ 90 | OK | survol rapide |
| 70-89 | à vérifier | rouvrir le scan sur les champs signalés |
| < 70 | revue obligatoire | ressaisie manuelle du bloc |

La revue se fait dans l'app (`/lot/<nom>`), scans en face du formulaire. Chaque commande
porte un bloc `revue {statut, verifie, par, commentaire}` avec `statut ∈
a_verifier|valide|ecarte`.

Écris `validation.json` **seulement** quand un humain a tranché. **Sans ce fichier, la
saisie est refusée** — et c'est voulu.

## Phase 5 — saisie dans SUSI

Prérequis : VPN AFM actif, session SUSI ouverte dans le Chrome de l'utilisateur (login
`AA_AFM_100`), intégration `claude-in-chrome` disponible.

Lance **un seul** `susi-saisisseur`. Jamais deux agents sur SUSI en parallèle : le serveur
rend des HTTP 500 au-delà d'environ 80-100 requêtes et les sessions expirent sous charge.

L'agent journalise chaque commande dans `saisie.log.json`, ce qui permet la reprise après
interruption. Il ne saisit **jamais** d'identifiants.

⚠️ **La première saisie sur un lot réel doit être supervisée.** Les raccourcis F5/TAB/F4/
Echap viennent du manuel et n'ont pas été confrontés à l'UI réelle. Au moindre écart,
interrompre et mettre à jour `susi-saisisseur.md`.

## Phase 6 — vérification post-saisie

Lance `susi-verificateur`, en lecture seule. Il compare SUSI (rapport Excel + détail UI) au
**scan d'origine** — jamais aux extraits, sinon une erreur de lecture se confirmerait
elle-même. Barème : écart montant −40, article faux −25, mauvais client −30, commande
absente ou en double = 0.

Sorties : `verification.json` et `review.md`, triés par **score croissant** — le pire en
tête.

## Phase 7 — traitement des écarts

Reprends à la main dans SUSI les commandes en tête de `review.md`, puis mets à jour la
passation.

## Anatomie d'un lot

```
lots/03092026FR_ATLAS_FID_50CH1/
├── *.pdf                  # scan source, copié à l'accueil
├── meta.json              # pays, type, paiement, nb commandes — déduits du nom
├── pages/p-001.jpg …      # rendu 150 dpi, padding 3 chiffres normalisé
├── extraits/cmd_NN.json   # extraction + bloc "revue"
├── lot.json               # consolidation, puis version validée
├── recap.md               # tableau de contrôle phase 3
├── validation.json        # checkpoint humain — la saisie est refusée sans lui
├── saisie.log.json / saisie_resultat.md
├── verification.json / review.md
└── jobs/{extraction,saisie,verification}.{log,status.json}
```

Supprimer un fichier, c'est revenir en arrière dans le pipeline : l'état affiché est
recalculé depuis le disque par `etat_lot()`.

## Règles métier à ne jamais réinventer

- Sur un **chèque**, le montant en **lettres fait foi** sur les chiffres (règle bancaire).
- Sur un **BDC**, le total se **recalcule depuis les lignes** — ne recopie pas un total
  manuscrit sans le vérifier.
- Confusions manuscrites sur les refco : `0/O`, `1/I`, `2/Z`, `5/S`, `B/8`.
- Scans parfois à 180° : le score ne s'en aperçoit pas tout seul.
- Cas particuliers (manuel p. 16) : deux chèques → cumul ; chèque absent → **OA** ;
  contre-remboursement → **OA** ; **espèces → ne pas saisir**.
