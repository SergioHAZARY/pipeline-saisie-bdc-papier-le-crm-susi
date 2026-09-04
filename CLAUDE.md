# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Objectif fondamental

Faire entrer dans SUSI, **sans ressaisie manuelle**, les commandes papier d'Atlas For Men —
**sans jamais laisser passer une commande fausse en encaissement**.

Les deux moitiés comptent autant l'une que l'autre. Chaque commande est adossée à un chèque : une
erreur de montant, de client ou d'article n'est pas une ligne fausse dans une base, c'est un
**encaissement faux**.

L'objectif n'est pas « automatiser la saisie » mais **déplacer l'humain de la saisie vers le
contrôle** : il ne tape plus, il arbitre les cas douteux que le score de confiance lui remonte.

Le projet est réussi quand un lot passe de bout en bout et que l'opérateur n'est intervenu que sur
les commandes réellement litigieuses. **Un excellent taux d'extraction avec une saisie non vérifiée
est un échec, pas un succès.** Conséquence pratique : toute optimisation qui gagne du débit en
affaiblissant un garde-fou va contre l'objectif — proposer, ne pas l'appliquer.

## Règles absolues

Ces règles ne se négocient pas, **même si la demande de l'utilisateur les contourne**. En cas de
doute, s'arrêter et demander.

1. **Ne jamais lancer la saisie du lot `28082026FR_ATLAS_III_FID_50CH1` sans supervision explicite
   de Djibril.** 50 commandes, 2 396,58 € de chèques réels. La saisie crée de vraies commandes et de
   vrais encaissements.
2. **La commande 26 du lot `50CH1` reste « écartée »** : double passage scanner (même ligne CMC7,
   même BDC que la paire voisine).
3. **Aucune saisie sans `validation.json`.** Le checkpoint humain est bloquant, ne pas le
   court-circuiter — ni en écrivant le fichier soi-même, ni en contournant `lancer_job()`.
4. **Ne jamais saisir d'identifiants ni de mot de passe.** Les agents travaillent dans une session
   Chrome déjà authentifiée (compte `AA_AFM_100`).
5. **Un seul job à la fois, jamais deux agents sur SUSI en parallèle.** SUSI est fragile : ~80-100
   requêtes puis HTTP 500, sessions qui expirent sous charge.
6. **En cas de divergence entre SUSI, l'extraction et le scan, le scan fait foi.**

## Ce que contient ce dépôt

Deux pipelines frères qui consomment **la même source SFTP** Atlas For Men. Ce ne sont pas des
doublons : une même liasse scannée alimente les deux (les chèques partent en remise bancaire, les
bons de commande partent en saisie CRM).

| Chemin | Projet | Rôle |
|---|---|---|
| `app/`, `tests/`, `lots/`, `.claude/` | **susi-bdc** | saisie des bons de commande dans le CRM SUSI |
| `tlmcremiseequipe/tlmc-remise/` | **tlmc-remise** | génération des fichiers de remise bancaire TLMC (CFONB) |

> **Origine de `susi-bdc` dans ce dépôt.** Le code d'origine vit sur le Mac de Djibril
> (`/Users/djisse/susi-bdc`) et n'a pas été rapatrié. Ce qui est ici est une **réimplémentation
> d'après la passation du 03/09/2026** (`Passationsusibdc.pdf`) et le runbook
> (`Runbooksusibdc.html`), adaptée à Windows. Les statuts « testé » de la passation valent pour le
> Mac ; sur cette machine, seul ce qui est listé dans [État vérifié](#état-vérifié-sur-cette-machine)
> a été prouvé. `tlmc-remise`, lui, est le code d'origine.

**Absent et non substituable : `manuel-commandes-rapides.md`.** C'est la référence fonctionnelle du
métier (dont les cas particuliers p. 16). Le runbook impose de le lire avant tout. À récupérer depuis
le Mac — les règles reproduites ici et dans les agents sont de seconde main.

## Commandes

### susi-bdc (racine)

```sh
python -m venv .venv && .venv/Scripts/pip install -r app/requirements.txt

app\start.ps1                                   # Windows  → http://0.0.0.0:8760
app/start.sh                                    # macOS/Linux
app\tunnel.ps1                                  # tunnel Cloudflare (URL éphémère)

python -m unittest discover -s tests                       # 45 tests
python -m unittest discover -s tests -p "test_score.py" -v  # un module
```

### tlmc-remise

```sh
cd tlmcremiseequipe/tlmc-remise
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt
.venv/Scripts/python -m uvicorn app:app --port 8742
python -m unittest discover -s tests                       # 38 tests

.venv/Scripts/python agent_ftp.py lister
.venv/Scripts/python agent_ftp.py pipeline --dossier "/POUR_OUTSOURCIA/BDC/09 SEPTEMBRE 2026" --parallele 8
.venv/Scripts/python agent_ftp.py finaliser --date 2026-09-03
```

**`tests/` n'a d'`__init__.py` dans aucun des deux projets** : `python -m unittest tests.test_cmc7`
échoue en `ModuleNotFoundError`. Toujours passer par `discover -s tests`, avec `-p` pour cibler un
module.

Sur un lien réseau lent, ajouter `--timeout 180 --retries 15` à pip : un `pip install` a calé
indéfiniment sur `files.pythonhosted.org` avec les valeurs par défaut. Ne pas enchaîner un
`pip install --upgrade pip --quiet` avant l'installation : en cas de timeout, il bloque tout sans
rien afficher.

## Le pipeline susi-bdc

| Phase | Qui | Sortie |
|---|---|---|
| 1. Accueil | plateforme | `meta.json`, `pages/p-NNN.jpg` (150 dpi) |
| 2. Extraction | `bdc-lecteur` ×10 | `extraits/cmd_NN.json` |
| 3. Consolidation | orchestrateur | `lot.json`, `recap.md` |
| 4. Revue | **un humain** | `validation.json` |
| 5. Saisie | `susi-saisisseur` | `saisie.log.json` |
| 6. Vérification | `susi-verificateur` | `verification.json`, `review.md` |

**Pas de base de données.** `lots/<LOT>/` est la seule source de vérité et la phase affichée est
recalculée depuis les fichiers par `etat_lot()` dans [app/app.py](app/app.py). Supprimer un fichier
fait reculer le pipeline. `validation.json` est un **verrou** : `lancer_job()` refuse la saisie sans
lui, et la vérification sans `saisie.log.json`.

La spec détaillée est [.claude/skills/susi-saisie-bdc/SKILL.md](.claude/skills/susi-saisie-bdc/SKILL.md)
— les prompts des jobs y renvoient, et **un écart entre l'app et la skill est un bug de l'app**.

Les agents et la skill sont en **portée projet** (`.claude/`), pas dans `~/.claude/` comme sur le
Mac : le job runner lance `claude -p` avec le dépôt pour `cwd`, ils y sont donc découverts, et le
projet reste autonome.

## Permissions des jobs headless — le piège qui coûte le plus cher

`claude -p` lancé par le job runner **n'est pas interactif** : toute demande d'approbation est
refusée, et **le job se termine malgré tout en `code 0`**. Un job « réussi » qui n'a rien écrit :
c'est le symptôme le plus trompeur de tout le pipeline. Deux capacités disparaissent :

- `pdftoppm` refusé → `bdc-lecteur` ne peut plus **zoomer à 300 dpi** sur une zone douteuse, ce qui
  est précisément sa raison d'être ;
- `Write` refusé → aucun `extraits/cmd_NN.json`, aucun `recap.md`.

**Un `.claude/settings.json` ne suffit pas**, et le harness dit précisément pourquoi — le message
apparaît en tête du log de chaque job :

```
Ignoring 10 permissions.allow entries from .claude/settings.json:
this workspace has not been trusted.
```

La cause n'est pas le fichier mais la **confiance de l'espace de travail** : tant que
`F:/WORK/susi-bdc` n'est pas approuvé, ses règles `permissions.allow` sont ignorées en bloc. Deux
remèdes, au choix de l'utilisateur (c'est une décision de sécurité, pas un réglage technique) :

- ouvrir une session Claude Code interactive dans ce dossier et accepter la boîte de dialogue ;
- ou poser `projects["F:/WORK/susi-bdc"].hasTrustDialogAccepted: true` dans `C:\Users\HP\.claude.json`.

**Les jobs, eux, n'en dépendent pas.** `lancer_job()` passe les autorisations en argument via
`OUTILS_AUTORISES`, ce qui court-circuite entièrement la question de la confiance :

```
--allowedTools Read,Write,Edit,Glob,Grep,Task,TodoWrite,Bash(pdftoppm:*),Bash(pdfinfo:*),…
```

C'est pourquoi le zoom 300 dpi et l'écriture des extraits fonctionnent alors que l'avertissement
s'affiche toujours. Sur un job, ce message est **cosmétique** : ne pas le confondre avec la panne
qu'il décrivait avant l'ajout de `--allowedTools`. `.claude/settings.json` ne sert qu'aux sessions
interactives, et seulement une fois le dossier approuvé.

**Second blocage, distinct** : le harness refuse à l'agent la création d'un sous-dossier —
« may only create directories in the allowed working directories ». `mkdir extraits/` échoue donc
même avec `Write` accordé. Les dossiers de sortie sont pour cette raison créés **par la plateforme**
à l'accueil du lot (`upload()`) et au démarrage du job (`lancer_job()`), jamais par l'agent.

Corollaire de diagnostic : un job à `code 0` avec `extraits/` vide ne se diagnostique pas dans le
rendu de l'UI, mais dans le **log brut**. Les refus arrivent en `type: "user"` / `tool_result` /
`is_error: true` avec « This command requires approval » ou « requested permissions to write to » —
que `_lignes_lisibles()` réduit au laconique `! erreur d'outil`. C'est une faiblesse connue du
rendu : élargir ce cas serait un bon premier chantier.

## Le score de confiance

Deux scores, même échelle : **≥ 90** OK · **70-89** à vérifier · **< 70** revue obligatoire.

`score_extraction()` (pré-saisie) partant de 100 : arithmétique lignes + frais ≠ total −40, chèque ≠
total −40, refco illisible −25, confiance d'article basse/moyenne −20/−10, anomalies −5 chacune
(plafond −20). Les retenues **se cumulent** et le résultat est borné à [0, 100].

`susi-verificateur` (post-saisie) : écart montant −40, article faux −25, mauvais client −30, commande
absente ou en double = 0. Cet agent **retourne toujours au scan** quand SUSI et l'extraction
divergent — comparer SUSI à l'extraction laisserait une erreur de lecture se confirmer elle-même.

## Auth (schéma commun aux deux projets)

HTTP Basic, comptes en variable d'environnement, format `nom:mdp:role`.

- susi-bdc : `SUSI_BDC_UTILISATEURS` dans `app/.env`, rôles `admin|viewer`. Un `viewer` ne peut faire
  aucun non-`GET` (403). Sans fichier, un admin est généré et affiché une seule fois en console.
- tlmc-remise : `TLMC_UTILISATEURS` dans `.env`, rôles `admin|editeur|viewer`, appliqués **par
  méthode HTTP et par chemin** (`editeur` restreint aux `CHEMINS_CORRECTION`).

⚠️ **`TLMC_UTILISATEURS` dans `.env` casse `tests/test_auth.py`.** Ce test s'appuie sur le repli de
rétrocompatibilité `TLMC_UTILISATEUR` + `TLMC_MOT_DE_PASSE`, qui n'est emprunté **que si
`TLMC_UTILISATEURS` est vide**. `charger_env()` utilise `setdefault`, donc un `.env` qui définit la
variable désactive le repli et le test échoue en `401 != 200`. Ne mettre que le couple
rétrocompatible dans `.env`.

## Architecture de tlmc-remise

Découpage clé : **`tlmc/` = logique pure ; `app.py` et `remise.py` = deux frontaux du même pipeline**.

- `tlmc/pipeline.py` — `traiter_dossier()`, le cœur partagé. `ThreadPoolExecutor` (`TLMC_WORKERS`,
  défaut 8), fenêtre bornée à `workers+4` pages en vol, plus un sémaphore **global**
  `TLMC_CONCURRENCE_GLOBALE` qui plafonne les appels vision tous lots confondus. Les pages sont
  soumises en parallèle mais **consommées dans l'ordre**.
- `tlmc/ocr.py` — Tesseract sur la bande CMC7 croppée si `cmc7.traineddata` est présent, sinon
  **fallback vision via le SDK `anthropic`** (`model="claude-opus-5"`). Gère l'orientation, la
  rotation 180°, le zoom. C'est lui qui classe une page « pas un chèque ».
- `tlmc/cmc7.py` — zones `7/12/12` (`ZONES_ATTENDUES`) et surtout la **clé RLMC** : le nombre entre
  parenthèses valide mathématiquement la ligne, répare les `?` (`resoudre_par_cle()`) et détecte les
  coquilles **avant** tout autre contrôle.
- `tlmc/writer.py` — format bancaire piloté par `spec/layout.json` (320 caractères, terminaison **CR
  seul**, enregistrements `03`/`04`/`08`). Sans ce fichier, pas de `.tlmc` (`SpecManquante`).
- `tlmc/lots.py` — `sessions/<id>/session.json` en **écriture atomique** (`os.replace`), pattern
  repris dans `susi-bdc` pour `extraits/cmd_NN.json`. `isole: true` retire un chèque sans le
  supprimer.

**Différence d'authentification à ne pas confondre** : `tlmc-remise` appelle le SDK `anthropic` et
exige donc `ANTHROPIC_API_KEY` (non définie ici → la lecture vision ne peut pas tourner).
`susi-bdc` passe par le **CLI `claude -p`**, donc par l'auth OAuth du CLI : il n'a besoin d'aucune
clé API.

## Accès SFTP

`sftp://afm_bdc@90.83.66.173:9822` — identifiants dans les variables `SFTP_*` de
`tlmcremiseequipe/tlmc-remise/.env`. Session WinSCP `AFM_BDC` enregistrée sur cette machine.

```
/POUR_OUTSOURCIA/PAIEMENTS /<MM MOIS AAAA>/          → chèques      → tlmc-remise
/POUR_OUTSOURCIA/BDC/<MM MOIS AAAA>/<JJMM>/          → bons+chèques → susi-bdc
```

Deux pièges vérifiés :

1. Le dossier s'appelle littéralement **`PAIEMENTS ` avec une espace finale**. Sans elle, le serveur
   répond `No such file` (code 2).
2. Le `SKILL.md` de tlmc-remise affirme que ce SFTP « ne répond que depuis le Mac de Djibril sous
   VPN ». **C'est faux** : la connexion aboutit depuis ce PC Windows sans VPN. Le premier essai avait
   échoué par timeout transitoire — **retenter avant de conclure**, avec `-timeout=90`. Ce qui exige
   réellement le VPN, c'est **SUSI** (`http://10.210.0.20/AFM/APP/SUSI/`), injoignable sans lui.

### Grammaire des noms de lot

`JJMMAAAA<PAYS> ATLAS [III] <FID|REC|RECRUT> [<paiement>] <n><unité>[<compteur>].pdf`

Le `III` est optionnel ; le type s'écrit `FID`, `REC` ou `RECRUT` ; le paiement peut être absent
(chèque joint), `OA`, `CB`, `C3M` ou `SANS PAIEMENTS` ; le compteur s'écrit `1` **ou** `001` ; et la
coquille `8DC` pour `8BDC` existe en production. Tout cela est couvert par `RX_NOM` et ses tests.

Côté tlmc-remise, `FILTRE_CHEQUES` ne retient que les noms contenant `<n>CH` et ignore `…1BDC.pdf`
(bons seuls) et `…7CB.pdf` (coupons carte).

## Poppler sur Windows

**`choco install poppler` ne livre que les sources**, sans aucun `.exe`. Utiliser winget :

```sh
winget install --id oschwartz10612.Poppler
```

Installé en 25.07.0, `bin` ajouté au PATH utilisateur (un shell déjà ouvert ne le voit pas).
`app/app.py` le retrouve seul via `_trouver_poppler()`, qui balaie le PATH puis `POPPLER_BIN` puis
les paquets winget. Les avertissements `Syntax Error: No display font for 'Symbol'` sont bénins.

### Le zéro-padding, mesuré

`pdftoppm` cale la largeur du compteur sur le nombre de pages :

| Pages | Sortie |
|---|---|
| 4 (mini-lot 2CH9) | `p-1.jpg` … `p-4.jpg` |
| 12 | `p-01.jpg` … `p-12.jpg` |
| 100 (lot 50CH) | `p-001.jpg` … `p-100.jpg` |

Un lot 50CH tombe naturellement sur `p-001.jpg` : **le bug ne se voit pas sur les gros lots**. C'est
le mini-lot de 4 pages — celui par lequel le runbook fait commencer — qui le déclenche. D'où le
renommage inconditionnel en `p-NNN.jpg` dans `rendre_pages()`.

## Pièges du job runner

- `stdin=subprocess.DEVNULL` est **obligatoire** : sinon `claude -p` hérite du stdin d'uvicorn et se
  fige indéfiniment (constaté : 3 h à 0 % CPU, log vide).
- **Épurer l'environnement** (`RX_ENV_A_PURGER` : `CLAUDE|ANTHROPIC|BAGGAGE|AI_AGENT|SENTRY`) : les
  variables héritées d'une session Claude court-circuitent l'auth Trousseau → 401 « OAuth access
  token is invalid » même connecté.
- `--mcp-config` exige `{"mcpServers": {}}`, **pas** `{}`. L'extraction ajoute
  `--strict-mcp-config` ; la saisie garde la config par défaut, il lui faut `claude-in-chrome`.
- `--output-format stream-json --verbose` + parsing ligne à ligne : en mode `text`, rien ne sort
  avant la fin.
- **Le verrou de jobs se répare tout seul.** Il vit en mémoire, donc un processus `claude -p` tué
  (veille de la machine, arrêt du serveur) le laissait pris pour toujours — et comme un verrou pris
  refuse tout nouveau job, la « réparation en relançant le job » que préconisait le runbook était
  **impossible**. Désormais : `lancer_job()` vérifie `_PROC_ACTIF.poll()` avant de refuser et libère
  un verrou dont le processus a disparu ; `reconcilier_statuts_au_demarrage()` repasse tout
  `en_cours` trouvé au démarrage en `interrompu` (aucun job ne survit à un redémarrage) ;
  `/sante` expose `verrou_orphelin` et `POST /jobs/liberer` sert de soupape manuelle. Les fichiers
  déjà écrits sont toujours conservés — un job interrompu à 45/50 garde ses 45 extraits.
- **La console Windows est en cp1252.** Un `print()` contenant un caractère hors de cette page (une
  flèche unicode, par exemple) lève `UnicodeEncodeError` et **tue le démarrage d'uvicorn** —
  constaté sur le hook de démarrage. S'en tenir à l'ASCII dans tout message de console ; les
  accents passent, les symboles typographiques non.

## SUSI

Serveur **fragile** : ~80-100 requêtes puis HTTP 500, sessions qui expirent sous charge. Un seul job
à la fois, saisie strictement séquentielle, **jamais deux agents sur SUSI en parallèle**.

⚠️ **La saisie crée de vraies commandes et de vrais encaissements.** La première doit être supervisée
par Djibril, sur 2 commandes, jamais sur un lot de 50. Les raccourcis `F5`/`TAB`/`F4`/`Echap`
viennent du manuel et n'ont **jamais** été confrontés à l'UI réelle. Le runbook exige aussi macOS
pour cette phase (`claude-in-chrome` + session SUSI).

## Règles métier

- Sur un **chèque**, le montant en **lettres fait foi** sur les chiffres (art. L131-10 C. mon. fin.,
  implémenté dans `arbitrer_montant()`). Sur un **BDC**, le total se **recalcule depuis les lignes**.
- Panier moyen ~50 € : au-delà de `TLMC_MONTANT_ALERTE`, double lecture vision. Un panier moyen de
  lot > 120 € signale un montant mal lu.
- Confusions manuscrites sur les refco : `0/O`, `1/I`, `2/Z`, `5/S`, `B/8` (cas réel : `KB179` →
  `K8179`). Scans parfois à 180°.
- Doublons de scan : comparer les lignes **CMC7** et les n° clients entre commandes d'un lot.
- Cas particuliers (manuel p. 16) : deux chèques → cumul ; chèque absent → **OA** ;
  contre-remboursement → **OA** ; **espèces → ne pas saisir**.

## État vérifié sur cette machine

| Brique | Statut |
|---|---|
| tlmc-remise : installation, 38 tests, service sur 8742 | **vérifié** |
| susi-bdc : 45 tests | **vérifié** |
| susi-bdc : auth et rôles (401 / admin 200 / viewer GET 200 / viewer POST 403) | **vérifié** |
| susi-bdc : accueil d'un lot, `meta.json`, rendu, padding normalisé | **vérifié** |
| susi-bdc : job runner `claude -p`, skill découverte, log lisible en direct | **vérifié** |
| susi-bdc : phases 1 à 3 complètes (extraction, zoom 300 dpi, consolidation, scores) | **vérifié** sur lot synthétique exploitable |
| Extraction vision sur un **scan manuscrit réel** | **non prouvée** |
| Saisie SUSI, vérification post-saisie | **jamais exécutées** |
| tlmc-remise : lecture vision | **impossible ici** — `ANTHROPIC_API_KEY` absente |

Prérequis en place : Python 3.13, CLI `claude` authentifié, poppler 25.07.0, Tesseract 5.4.0,
git, `cloudflared`, WinSCP 6.5.6. Aucun VPN monté (FortiClient, OpenVPN Connect et WireGuard
installés mais inactifs), donc SUSI injoignable.
