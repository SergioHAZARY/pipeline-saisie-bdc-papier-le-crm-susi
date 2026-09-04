# tlmc-remise

Génère un fichier de remise **TLMC** (Télétransmission des Lignes Magnétiques de
Chèques, norme CFONB) à partir de chèques scannés (PDF ou images).

## Pipeline

1. **Scans** (`scans/`, PDF multi-pages ou JPG/PNG) → images via PyMuPDF
2. **OCR CMC7** : Tesseract avec un modèle `cmc7.traineddata` s'il est installé
   (chercher « cmc7 traineddata » sur GitHub — la communauté brésilienne en
   maintient pour les boletos), sinon **fallback vision Claude** (sortie
   structurée : ligne CMC7 + montant + niveau de confiance)
3. **Validation** : zones CMC7 (7 / 12 / 12 chiffres), montant → centimes
4. **Sorties** :
   - `sortie/controle.csv` — récap à vérifier humainement avant envoi banque
   - `rejets/rejets.csv` + images croppées — chèques illisibles, à saisir à la main
   - `sortie/remise_YYYYMMDD.tlmc` — **uniquement si `spec/layout.json` existe**

## Installation

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

Fallback vision : clé API dans `ANTHROPIC_API_KEY` (ou profil `ant auth login`).

## Usage

### UI web (recommandé)

```sh
.venv/bin/python -m uvicorn app:app --port 8742
```

Puis ouvrir http://localhost:8742 :
- **Scans locaux** : sélection de fichiers ou d'un dossier entier
- **Google Drive** : coller le lien d'un fichier ou dossier partagé
  (« toute personne disposant du lien » — via gdown, pas d'OAuth)

Chaque traitement crée une session dans `sessions/<horodatage>/` avec les
scans, rejets et sorties (controle.csv, rejets.csv, .tlmc téléchargeables).

### CLI

```sh
.venv/bin/python remise.py --input ./scans/
```

## Format TLMC (BRED, 320 caractères)

`spec/layout.json` décrit le format déduit d'un fichier de remise réel
(REMISE OUTSOURCIA / BRED) : enregistrements de **320 caractères** terminés
par **CR seul** — `03` en-tête, `04` par chèque, `08` total. Les paramètres
remettant (libellé banque, identifiant, RIB crédité, n° de remise) sont dans
`layout.json > parametres` et surchargeables à la génération dans l'UI.

Chaque lot traité est persisté dans `sessions/<id>/session.json` : l'UI
permet de relire le résultat, corriger les zones CMC7 et les montants,
isoler un chèque (retiré de la remise), puis générer le fichier TLMC.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Limites connues

- Le crop de la bande CMC7 suppose un scan à peu près droit (deskew léger
  seulement) — à caler sur les scans réels.
- Tesseract ne lit pas le montant (souvent manuscrit) : montant via le
  fallback vision ou saisie manuelle (rejets).
- Structure CMC7 supposée 7/12/12 chiffres — à confirmer avec la spec banque
  (`ZONES_ATTENDUES` dans `tlmc/cmc7.py`).

## Agent SFTP → TLMC (`agent_ftp.py`)

Pilote le générateur hébergé (API JSON `/api/...`, compte admin) depuis les
scans déposés sur le SFTP Atlas For Men (identifiants `SFTP_*` dans `.env`).

```sh
.venv/bin/python agent_ftp.py lister                  # arborescence SFTP
.venv/bin/python agent_ftp.py sync                    # télécharge les nouveaux PDF
.venv/bin/python agent_ftp.py traiter --tout          # dépôt + lecture + autocorrection
.venv/bin/python agent_ftp.py a_corriger              # chèques restants (JSON + image_url)
.venv/bin/python agent_ftp.py image SESSION IMG --zoom-bande
.venv/bin/python agent_ftp.py corriger SESSION CID --z2 062016706908 --montant 44.98
.venv/bin/python agent_ftp.py generer --date 2026-08-23   # n° de remise séquentiels
.venv/bin/python agent_ftp.py rapport                 # sessions/agent_ftp/rapport.csv
```

État dans `sessions/agent_ftp/etat.json` (fichiers connus, sessions, n° de
remise). `--local-dir` sur `sync` permet de tester sans SFTP ; `TLMC_URL`
pointe vers un serveur local si besoin.

API JSON : `GET /api/lots`, `POST /api/traiter/upload`, `GET /api/lot/{nom}`,
`GET /api/lot/{nom}/a_corriger`, `POST /api/lot/{nom}/autocorriger`,
`POST /api/lot/{nom}/cheque/{cid}` (JSON z1/z2/z3/montant_eur/isole/commentaire),
`POST /api/lot/{nom}/tlmc` (JSON numero_remise/date_remise).

### Industrialisation (23/08/2026)

- Machine Fly `performance-8x` / 16 Go (`fly.toml`, appliqué au `fly deploy`), `TLMC_WORKERS=12`,
  `TLMC_CONCURRENCE_GLOBALE=64`, arrêt automatique hors usage (`min_machines_running = 0`).
- Chaîne complète : `agent_ftp.py pipeline --dossier "/POUR_OUTSOURCIA/PAIEMENTS /MM MOIS AAAA"`
  (sync SFTP sous VPN → lecture sur Fly, 8 lots de front → `sessions/agent_ftp/a_corriger_par_lot.json`)
  → sous-agents Claude `tlmc-correcteur` (`~/.claude/agents/`) → `agent_ftp.py finaliser --date AAAA-MM-JJ`
  → Google Sheet. Orchestration décrite dans la skill Claude Code `/tlmc-remise-sftp`
  (`~/.claude/skills/tlmc-remise-sftp/SKILL.md`).
