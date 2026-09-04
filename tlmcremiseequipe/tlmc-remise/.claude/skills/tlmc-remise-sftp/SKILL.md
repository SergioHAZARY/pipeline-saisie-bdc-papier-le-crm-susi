---
name: tlmc-remise-sftp
description: Remise de chèques TLMC Atlas For Men de bout en bout — récupère les lots PDF de chèques sur le SFTP AFM, les fait lire par le générateur TLMC hébergé (tlmc-remise.fly.dev), lance des sous-agents « tlmc-correcteur » (vision) pour corriger chaque chèque douteux sans en isoler, génère les fichiers TLMC et livre un Google Sheet récapitulatif (nom du fichier / montant endossé / lien TLMC / commentaire). Déclencher sur « remise TLMC », « lots du SFTP », « chèques AFM », « génère les TLMC », « /tlmc-remise-sftp ».
---

# Remise TLMC depuis le SFTP AFM

Projet : `.` (python = `.venv/bin/python`). Outil hébergé : https://tlmc-remise.fly.dev
(auth dans `.env`). Orchestrateur : `agent_ftp.py` (voir `--help`). Prompt des sous-agents :
`sessions/agent_ftp/PROMPT_SOUS_AGENT_CORRECTION.md`. Définition du sous-agent : `~/.claude/agents/tlmc-correcteur.md`.

## Pré-requis à vérifier avant de démarrer
1. **VPN** : le SFTP `90.83.66.173:9822` ne répond que depuis le Mac de Djibril sous VPN (jamais depuis Fly).
   Test : `.venv/bin/python -c "import socket;s=socket.socket();s.settimeout(15);print(s.connect_ex(('90.83.66.173',9822)))"` → `0` attendu. Sinon demander à Djibril d'activer le VPN et attendre.
2. **Périmètre — DEUX flux, tous les jours du mois** : `/POUR_OUTSOURCIA/PAIEMENTS /<MM MOIS AAAA>` (lots « PAIEMENTS ») **et** `/POUR_OUTSOURCIA/BDC/<MM MOIS AAAA>` (lots « FID » : 50 chèques + 50 bons de commande). Ce ne sont pas des doublons. Dans chaque dossier, seuls les fichiers dont le nom contient `<n>CH` (ex. `50CH4`, `19CH`, `50CH12 BDC`) sont des lots de chèques ; `…1BDC.pdf` = bons seuls sans chèque et `…7CB.pdf` = coupons carte bancaire → ignorés automatiquement (`FILTRE_CHEQUES`). Demander le mois si non précisé ; par défaut le mois courant. Rien ne doit être oublié : à la fin, comparer `agent_ftp.py lister` filtré avec `etat.json`.
3. Les lots déjà déposés dans l'outil (même nom de fichier) sont détectés et **non relus** : ils apparaissent dans le rapport avec leur TLMC existant.

## Étape 1 — lecture (tout se passe sur Fly, machine performance-8x)
```sh
cd . && cd . && .venv/bin/python -u agent_ftp.py pipeline --dossier "/POUR_OUTSOURCIA/BDC/09 SEPTEMBRE 2026" --dossier "/POUR_OUTSOURCIA/PAIEMENTS /09 SEPTEMBRE 2026" --parallele 8
```
Lance-le avec `run_in_background` : le sync SFTP (4 connexions, ≈ 1 Mo/s) tourne en continu pendant que les lots arrivés sont lus (8 de front, ≈ 3,8 pages/s). Surveille `PIPELINE_TERMINE` dans le log. Les sessions BDC sont préfixées `ftp-bdc-`, n° de remise `JJ(MM+50)NN` pour FID, `JJ(MM+60)NN` pour REC, lots partiels « NNCH » → NN+50 (5CH → 55), ex. 035803 / 036803 / 055855 (PAIEMENTS : `JJMMNN`).
À la fin il écrit `sessions/agent_ftp/a_corriger_par_lot.json` et affiche par lot : n à corriger, rejets, total (⚠ si panier moyen > 120 € = montant mal lu).
Si l'outil sature (latence API > 30 s), relancer avec `--parallele 4`. Secours si Fly est indisponible : `TLMC_URL=http://localhost:8743` (app locale : `TLMC_CONCURRENCE_GLOBALE=32 .venv/bin/python -m uvicorn app:app --port 8743`) puis `agent_ftp.py pousser`.

## Étape 2 — correction par sous-agents (objectif : 0 chèque isolé)
Pour chaque lot de `a_corriger_par_lot.json` ayant `a_corriger > 0` ou `rejets > 0`, lance un sous-agent
`tlmc-correcteur` (Agent tool, `subagent_type: "tlmc-correcteur"`, `run_in_background: true`), **2 à 3 lots par
sous-agent, jusqu'à 8-10 sous-agents en parallèle**. Prompt minimal :

> Sessions à traiter : `S1`, `S2`, `S3` (n à corriger / rejets / total indiqués). Signale dans le prompt les lots au total suspect (« un montant est probablement aberrant »).

Chaque sous-agent rend un JSON (une ligne par chèque) et les totaux. Budget observé : ~110-150 k tokens et 6-12 min par sous-agent de 2-3 lots.
Relance un sous-agent si un lot revient avec des chèques encore listés par `a_corriger` dont le problème n'est pas
une simple trace « corrigé manuellement ».

## Étape 3 — génération + rapport
```sh
.venv/bin/python agent_ftp.py finaliser --date AAAA-MM-JJ     # date de remise = jour du dépôt
```
Refuse les lots où il reste des chèques à corriger (les liste) ; `--force` pour passer outre. Produit
`sessions/agent_ftp/rapport.csv` (colonnes : nom du fichier, montant endossé (€), lien vers le fichier TLMC, lien vers le lot (tlmc-remise), commentaire).

## Étape 4 — Google Sheets (un par flux)
`finaliser` écrit `rapport_BDC.csv` et `rapport_PAIEMENTS.csv` (+ `rapport.csv` global). Trier chaque CSV par date de lot,
puis créer **deux** feuilles avec le connecteur Drive : `mcp__…__create_file` (`contentMimeType: text/csv`, `textContent` = CSV),
titres « TLMC – BDC (FID) <mois> » et « TLMC – PAIEMENTS <mois> ». Les fichiers appartiennent à Djibril ; donner les deux `viewUrl`.

## Règles métier (ne pas transiger)
- **Jamais isoler** un chèque lisible : corriger zones CMC7 (7/12/12), clé RLMC (`97 - (100×int(z1+z2+z3)) % 97`), montant.
- Montant : les **lettres font foi** en cas de désaccord avec les chiffres → le dire dans le commentaire du lot (« à confirmer »).
- Chèque physiquement illisible (scan masqué, coupé) : reste en rejet, commentaire « à rescanner » dans le sheet.
- Les lots de l'équipe non relus, les montants > 500 €, les totaux de lot aberrants et les clés masquées sont **signalés** dans la colonne commentaire, pas corrigés en silence.
- Numérotation des remises : `JJMM + NN` (automatique depuis le nom de fichier).

## Récap final attendu pour Djibril
Lien du Google Sheet ; nombre de lots / chèques / montant total ; chèques non remis ; points « à confirmer » ;
anomalies de l'outil rencontrées (et, si corrigées, déployées : `fly deploy --remote-only --ha=false -a tlmc-remise`).
Mettre à jour la mémoire projet `tlmc-remise-project.md`.

## Étape 5 — colonne F du « Suivi saisie paiement SUSI » (agent `tlmc-suivi-susi`)
Une fois le Google Sheet récap livré, lancer l'agent `tlmc-suivi-susi` (Agent tool, `subagent_type: "tlmc-suivi-susi"`)
avec le chemin du `rapport_sheet.csv` et l'ID du sheet de suivi (`1exk9CYqd0lYvtxAiXpKko0o2o04_8HtENGEbrFt6CS8` par défaut).
Il remplit F (montant endossé) sur les onglets `AA JJ-MM` puis `OUT JJ-MM`, met en rouge les lignes où F ≠ E (saisie SUSI ≠ chèques)
via `remplir_suivi.py` (en place si OAuth gspread configuré, sinon xlsx rempli + sheet « Patch colonne F »).
