# susi-bdc

Pipeline de saisie des bons de commande papier Atlas For Men dans le CRM SUSI
(« Saisie commande rapide ») : lecture vision des scans par sous-agents Claude, revue
humaine avec score de confiance, saisie pilotée dans SUSI, vérification post-saisie.

Deux frontaux partagent le même état sur disque — la plateforme web et une session Claude
Code — donc un lot commencé d'un côté se poursuit de l'autre.

> **Origine de cette copie.** Réimplémentation sur Windows d'après la passation du
> 03/09/2026, le code d'origine (`/Users/djisse/susi-bdc`, macOS) n'ayant pas été rapatrié.
> Les briques marquées « testé » dans la passation l'ont été sur le Mac : ici, seul ce qui
> figure dans la section [État vérifié](#état-vérifié) a été prouvé sur cette machine.

## Le métier en deux minutes

Atlas For Men reçoit des commandes papier : un BDC manuscrit accompagné le plus souvent
d'un chèque. Les liasses sont scannées en PDF, nom de fichier normé :

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

Le `III` de campagne est optionnel, le paiement peut valoir `OA` (sans paiement), `CB`,
`C3M` ou `SANS PAIEMENTS`, le compteur s'écrit `1` ou `001`, et la coquille `8DC` pour
`8BDC` existe en production. Dans un lot `CH` : **pages impaires = chèques, pages paires
= BDC**.

Référence fonctionnelle : `manuel-commandes-rapides.md` — **à lire en premier**, tout le
pipeline en découle (y compris les cas particuliers p. 16 : deux chèques, chèque absent →
OA, contre-remboursement → OA, espèces → ne pas saisir). *Ce fichier est absent de cette
copie : il est à récupérer depuis le Mac.*

## Démarrer

Prérequis : Python 3.11+, `pdftoppm` (poppler), CLI Claude Code connecté.

```sh
winget install --id oschwartz10612.Poppler   # PAS choco : ce paquet ne livre que les sources
claude -p "ok" --output-format text          # doit répondre

app\start.ps1                                # Windows → http://0.0.0.0:8760
app/start.sh                                 # macOS / Linux
app\tunnel.ps1                               # URL publique https (Cloudflare, éphémère)
```

Comptes dans `app/.env` (non versionné), format `nom:mdp:role`, rôles `admin|viewer` :

```
SUSI_BDC_UTILISATEURS=admin:motdepasse:admin,revue:autremdp:viewer
```

Si le fichier est absent, un compte admin est généré au premier démarrage et affiché en
console — il n'est pas réaffiché.

```sh
python -m unittest discover -s tests         # 45 tests
```

## Le pipeline

| Phase | Qui | Sortie |
|---|---|---|
| 1. Accueil | plateforme | `meta.json`, `pages/p-NNN.jpg` (150 dpi) |
| 2. Extraction | `bdc-lecteur` ×10 | `extraits/cmd_NN.json` |
| 3. Consolidation | orchestrateur | `lot.json`, `recap.md` |
| 4. Revue | **un humain** | `validation.json` |
| 5. Saisie | `susi-saisisseur` | `saisie.log.json`, `saisie_resultat.md` |
| 6. Vérification | `susi-verificateur` | `verification.json`, `review.md` |

La spec détaillée des phases vit dans `.claude/skills/susi-saisie-bdc/SKILL.md` : les
prompts des jobs de la plateforme y renvoient, et **un écart entre l'app et la skill est
toujours un bug de l'app**.

En session Claude Code : glisser un PDF de liasse et dire « traite mes bdc » — même état
sur disque, revue ensuite possible via l'app.

## Anatomie d'un lot

```
lots/29082026FR_ATLAS_III_FID_2CH9/
├── *.pdf                  # scan source, copié à l'accueil
├── meta.json              # déduit du nom de fichier
├── pages/p-001.jpg …      # rendu 150 dpi, padding 3 chiffres normalisé
├── extraits/cmd_NN.json   # extraction + bloc "revue"
├── lot.json               # consolidation, puis version validée
├── recap.md               # tableau de contrôle phase 3
├── validation.json        # checkpoint humain — la saisie est refusée sans lui
├── saisie.log.json / saisie_resultat.md
├── verification.json / review.md
└── jobs/{extraction,saisie,verification}.{log,status.json}
```

**Il n'y a aucune base de données.** Le dossier du lot est la seule source de vérité et la
phase affichée est recalculée depuis les fichiers par `etat_lot()`. Supprimer un fichier,
c'est revenir en arrière dans le pipeline.

## Le score de confiance

Deux scores distincts, même échelle : **≥ 90** OK · **70-89** à vérifier · **< 70** revue
obligatoire.

**Pré-saisie**, par `score_extraction()` — 100 moins : arithmétique lignes + frais ≠ total
−40, chèque ≠ total −40, refco illisible −25, confiance d'article basse/moyenne −20/−10,
anomalies −5 chacune (plafond −20).

**Post-saisie**, par `susi-verificateur` — écart montant −40, article faux −25, mauvais
client −30, commande absente ou en double = 0. Cet agent **retourne toujours au scan**
quand SUSI et l'extraction divergent : comparer SUSI à l'extraction laisserait une erreur
de lecture se confirmer elle-même.

## État vérifié sur cette machine

| Brique | Statut | Preuve |
|---|---|---|
| Accueil : upload, méta, rendu | **vérifié** | lot `2CH9` synthétique, 4 pages, `p-001`…`p-004` |
| Parsing du nom de lot | **vérifié** | 45 tests, dont les variantes réelles du SFTP |
| Score de confiance | **vérifié** | 45 tests, cumuls et plafonds compris |
| Auth et rôles | **vérifié** | 401 sans auth · admin 200 · viewer GET 200 / POST 403 |
| Job runner `claude -p` | **vérifié** | session ouverte, skill découverte, log lisible en direct |
| Phases 1 à 3 de bout en bout | **vérifié** | lot `2CH7` synthétique : 2/2 extraits, zoom 300 dpi, `lot.json`, `recap.md`, scores 100 et 55 |
| Extraction vision réelle | **non prouvée ici** | jamais lancée sur un scan manuscrit véritable |
| Saisie SUSI | **jamais exécutée** | exige VPN + session Chrome + supervision |
| Vérification post-saisie | **jamais exécutée** | dépend de la première saisie |

⚠️ **La saisie crée de vraies commandes et de vrais encaissements.** La première doit être
supervisée, sur 2 commandes, jamais sur un lot de 50.

## Les pièges déjà payés

- `stdin=subprocess.DEVNULL` est **obligatoire** sur le `claude -p` du job runner : sinon
  il hérite du stdin d'uvicorn et se fige indéfiniment (constaté : 3 h à 0 % CPU).
- **Épurer l'environnement** avant de lancer `claude -p` depuis un process lancé par une
  session Claude (regex `CLAUDE|ANTHROPIC|BAGGAGE|AI_AGENT|SENTRY`) : les variables
  héritées court-circuitent l'auth → 401 « OAuth access token is invalid ».
- `--mcp-config` exige `{"mcpServers": {}}`, **pas** `{}`. L'extraction tourne en
  `--strict-mcp-config` ; la saisie garde la config par défaut, il lui faut
  `claude-in-chrome`.
- `--output-format stream-json --verbose` + parsing ligne à ligne : en mode `text`, rien ne
  sort avant la fin du job.
- `pdftoppm` **cale le zéro-padding sur le nombre de pages** — 4 pages → `p-1.jpg`,
  100 pages → `p-001.jpg`. L'accueil renomme donc systématiquement en `p-NNN.jpg`, sinon le
  tri lexicographique casse. Le mini-lot de 4 pages est précisément le cas qui déclenche le
  bug ; un lot de 50CH (100 pages) ne le montre pas.
- **SUSI est fragile** : ~80-100 requêtes puis HTTP 500, sessions qui expirent sous charge.
  Un seul job à la fois (verrou global), saisie strictement séquentielle, jamais deux agents
  sur SUSI en parallèle.
- Métier : sur un **chèque**, les lettres font foi ; sur un **BDC**, recalculer depuis les
  lignes. Confusions manuscrites `0/O`, `1/I`, `2/Z`, `5/S`, `B/8` sur les refco. Scans
  parfois à 180°.

## Reste à faire

1. **Récupérer `manuel-commandes-rapides.md`** depuis le Mac — sans lui, impossible de juger
   une extraction, et les cas particuliers de la page 16 restent de seconde main.
2. Première extraction sur un **scan manuscrit réel**, puis première saisie supervisée.
3. Validation des refco au catalogue dès l'extraction (atlasformen.fr ou export SUSI).
4. Détection de doublons **déterministe** dans la consolidation : comparer lignes CMC7 et
   n° clients entre commandes d'un lot.
5. Durcir : comptes nominatifs, tunnel stable (Cloudflare nommé ou Tailscale), file de jobs
   persistante — le verrou est en mémoire, un restart pendant un job laisse un `en_cours`
   orphelin, réparable en relançant le job.
