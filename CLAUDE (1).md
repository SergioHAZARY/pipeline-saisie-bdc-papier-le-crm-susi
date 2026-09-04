# susi-bdc — contexte projet

Pipeline de saisie des bons de commande papier Atlas For Men dans le CRM SUSI.

## Objectif fondamental

**Faire entrer dans SUSI, sans ressaisie manuelle, les commandes papier d'Atlas For Men — sans jamais laisser passer une commande fausse en encaissement.**

Les deux moitiés comptent autant l'une que l'autre. Chaque commande est adossée à un chèque : une erreur de montant, de client ou d'article n'est pas une ligne fausse dans une base, c'est un encaissement faux.

L'objectif n'est pas « automatiser la saisie » mais **déplacer l'humain de la saisie vers le contrôle** : il ne tape plus, il arbitre les cas douteux que le score de confiance lui remonte.

Le projet est réussi quand un lot passe de bout en bout et que l'opérateur n'est intervenu que sur les commandes réellement litigieuses. Un excellent taux d'extraction avec une saisie non vérifiée est un échec, pas un succès.

## Règles absolues

Ces règles ne se négocient pas, même si l'utilisateur les contourne dans sa demande. En cas de doute, s'arrêter et demander.

1. **Ne jamais lancer la saisie du lot `28082026FR_ATLAS_III_FID_50CH1`** sans supervision explicite de Djibril. 50 commandes, 2 396,58 € de chèques réels. La saisie crée de vraies commandes et de vrais encaissements.
2. **La commande 26 du lot 50CH1 reste « écartée »** : double passage scanner (même ligne CMC7, même BDC que la paire voisine).
3. **Aucune saisie sans `validation.json`.** Le checkpoint humain est bloquant, ne pas le court-circuiter.
4. **Ne jamais saisir d'identifiants ni de mot de passe.** Les agents travaillent dans une session Chrome déjà authentifiée (compte `AA_AFM_100`).
5. **Un seul job à la fois, jamais deux agents sur SUSI en parallèle.** SUSI est fragile : ~80-100 requêtes puis HTTP 500, sessions qui expirent sous charge.
6. **En cas de divergence entre SUSI, l'extraction et le scan, le scan fait foi.**

## Référence métier

`manuel-commandes-rapides.md` à la racine — **à lire en premier**, tout le pipeline en découle. Source : manuel Mada « Commandes Rapides ». Les cas particuliers de la p. 16 sont structurants : deux chèques, chèque absent → OA, contre-remboursement → OA, espèces → ne pas saisir.

Nommage des lots : `28082026FR ATLAS III FID 50CH1.pdf` = date · pays · FID (client existant) ou RECRUT (nouveau) · 50CH (50 commandes par chèque ; OA = sans paiement joint) · liasse n° 1. Dans un lot CH : **pages impaires = chèques, pages paires = BDC**.

Saisie dans SUSI, module « Saisie commande rapide » : `F5` créer un lot, `TAB` entre champs, `F4` bloc suivant, `Echap` clôturer.

## Architecture

Deux frontaux — la plateforme web et une session Claude Code — partagent **le même état sur disque**. Un lot commencé d'un côté se poursuit de l'autre.

| Composant | Emplacement | Rôle |
|---|---|---|
| Skill orchestratrice | `~/.claude/skills/susi-saisie-bdc/SKILL.md` | Le pipeline en 7 phases — **spec de référence**, les prompts des jobs y renvoient |
| `bdc-lecteur` | `~/.claude/agents/bdc-lecteur.md` | Lecture vision d'une paire chèque+BDC, ~5 commandes par exemplaire, jusqu'à 10 en parallèle |
| `susi-saisisseur` | `~/.claude/agents/susi-saisisseur.md` | Saisie du lot validé dans SUSI via `claude-in-chrome`, un seul exemplaire |
| `susi-verificateur` | `~/.claude/agents/susi-verificateur.md` | Post-saisie, **lecture seule** : compare SUSI au scan, score 0-100 par commande |
| Plateforme web | `app/app.py` | FastAPI, port 8760, fichier unique. Upload, jobs, revue, validation, vérification |

SUSI : `http://10.210.0.20/AFM/APP/SUSI/` — accessible uniquement via VPN AFM, à travers le Chrome de l'utilisateur.

## L'état est le dossier du lot

Il n'y a **aucune base de données**. La phase affichée dans l'UI est entièrement déduite des fichiers présents (`etat_lot()` dans `app.py`).

```
lots/28082026FR_ATLAS_III_FID_50CH1/
├── *.pdf                  # scan source (copié à l'upload)
├── meta.json              # pays, FID/RECRUT, CH/OA, nb commandes — déduits du nom
├── pages/p-001.jpg …      # rendu 150 dpi, padding 3 chiffres normalisé
├── extraits/cmd_NN.json   # extraction + bloc "revue" {statut, verifie, par, commentaire}
├── lot.json               # consolidation, puis version validée
├── recap.md               # tableau de contrôle phase 3
├── validation.json        # checkpoint humain — la saisie est refusée sans lui
├── saisie.log.json / saisie_resultat.md
├── verification.json / review.md
└── jobs/{extraction,saisie,verification}.{log,status.json}
```

Toute modification du modèle d'état doit préserver cette propriété : les deux frontaux lisent les mêmes fichiers.

## Scores de confiance

Même échelle pour les deux : **≥ 90 OK · 70-89 à vérifier · < 70 review obligatoire**.

- **Pré-saisie** — `score_extraction()` dans `app.py` : 100 moins les retenues (arithmétique lignes+frais ≠ total −40, chèque ≠ total −40, refco illisible −25, confiance article basse/moyenne −20/−10, anomalies −5 chacune plafonné à −20).
- **Post-saisie** — barème propre à `susi-verificateur` (écart montant −40, article faux −25, mauvais client −30, absente ou en double = 0).

## État des lieux (03/09/2026)

| Brique | Statut |
|---|---|
| Upload + rendu + méta | testé |
| Extraction via plateforme | testé — 50/50 commandes en 16 min, 10 lecteurs parallèles |
| Revue / édition / validation | testé |
| Log des jobs en direct | testé |
| **Saisie SUSI réelle** | **jamais exécutée** — l'agent n'a jamais vu l'UI Commandes Rapides |
| **Vérification post-saisie** | **jamais exécutée** — dépend de la première saisie |

Les raccourcis `F5`/`F4`/`Echap` viennent du manuel et n'ont pas été confrontés au terrain.

## Démarrer

Prérequis : `pdftoppm` (`brew install poppler`) · CLI Claude connecté (`claude /login`, sans sudo ; tester `claude -p "ok" --output-format text`) · dépôt **et** arborescence `~/.claude/agents/` + `~/.claude/skills/susi-saisie-bdc/` · `app/.env` (non versionné).

```bash
app/start.sh     # http://0.0.0.0:8760
app/tunnel.sh    # URL publique https (Cloudflare, éphémère)
```

Auth HTTP Basic : `SUSI_BDC_UTILISATEURS=nom:mdp:admin,nom2:mdp2:viewer` dans `app/.env`. Un compte admin est auto-généré au premier démarrage et affiché en console.

**Lot de test sans risque** : `29082026FR_ATLAS_III_FID_2CH9` (4 pages, 2 commandes). Pour repartir de zéro : supprimer `extraits/*`, `lot.json`, `recap.md`, `jobs/*`. Les phases 1 à 3 ne demandent ni VPN ni SUSI.

En session Claude Code : glisser un PDF de lot et dire « traite mes bdc » déclenche la skill `susi-saisie-bdc`.

## Pièges déjà payés — ne pas les réapprendre

- `stdin=subprocess.DEVNULL` est **obligatoire** sur le `claude -p` du job runner : sinon il hérite du stdin d'uvicorn et se fige indéfiniment (constaté : 3 h à 0 % CPU, log vide).
- **Épurer l'environnement** avant de lancer `claude -p` depuis un process lancé par une session Claude (regex `CLAUDE|ANTHROPIC|BAGGAGE|AI_AGENT|SENTRY`) : les variables héritées court-circuitent l'auth Trousseau → 401 « OAuth access token is invalid » même connecté.
- `--mcp-config` exige `{"mcpServers": {}}`, **pas** `{}`. L'extraction tourne avec `--strict-mcp-config` (démarrage rapide) ; la saisie garde la config par défaut, elle a besoin de `claude-in-chrome`.
- `--output-format stream-json --verbose` + parsing ligne à ligne (`_lignes_lisibles()`) pour un log en direct — en mode `text`, rien ne sort avant la fin.
- `pdftoppm` adapte le zéro-padding au nombre de pages (`p-1.jpg` sur un petit PDF) : l'upload renomme systématiquement en `p-NNN.jpg`, sinon le tri lexicographique casse.
- Métier : sur un **chèque, les lettres font foi sur les chiffres** ; sur un **BDC, recalculer depuis les lignes**. Surveiller les confusions manuscrites `0/O`, `1/I`, `2/Z`, `5/S`, `B/8` sur les refco, les scans à 180°, et les doublons de scan (comparer la ligne CMC7).
- Le verrou de jobs est **en mémoire** : un restart pendant un job laisse un `en_cours` orphelin, réparable en relançant le job.

## Chantiers, par priorité

1. **Première saisie SUSI supervisée** (avec Djibril, VPN + session ouverte) : roder `susi-saisisseur` sur l'UI réelle, puis premier run de `susi-verificateur`.
2. **Validation des refco au catalogue** : croiser chaque refco extraite avec le référentiel (atlasformen.fr ou export SUSI) dès l'extraction. Fait ponctuellement par l'agent (`KB179` → `K8179`), à systématiser en phase 3 ou dans l'app.
3. **Détection de doublons déterministe** : comparer lignes CMC7 et n° clients entre commandes d'un lot, dans `score_extraction()` ou à la consolidation, plutôt que de compter sur la vigilance de l'agent.
4. **Durcir la plateforme** : comptes nominatifs, tunnel stable (Cloudflare nommé ou Tailscale), file de jobs persistante.
5. **Tests** : rien n'est couvert. Commencer par `score_extraction()`, le parsing de nom de lot (`RX_NOM`) et `_lignes_lisibles()`.

## Références

- Spec pipeline : `~/.claude/skills/susi-saisie-bdc/SKILL.md` · `README.md`
- Capacité serveur SUSI : `~/.claude/agents/susi-rapports.md`
- Projet frère (auth, patterns) : plateforme TLMC, `/Users/djisse/tlmc-remise`
