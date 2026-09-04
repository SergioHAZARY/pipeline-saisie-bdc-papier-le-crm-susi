# Remise TLMC — mode d'emploi équipe

Ce dossier est autonome : il contient l'outil, la skill Claude Code (`.claude/skills/tlmc-remise-sftp`)
et les sous-agents (`.claude/agents/`). Ouvrir Claude Code **dans ce dossier** suffit pour que la skill
et les agents soient disponibles.

## Prérequis (une fois par personne)

1. **Récupérer le dossier** `tlmc-remise` complet (avec `.env` — transmis de façon sécurisée, jamais par mail),
   idéalement dans `~/tlmc-remise`. Le dossier `sessions/` n'est pas nécessaire, SAUF
   `sessions/agent_ftp/PROMPT_SOUS_AGENT_CORRECTION.md` (prompt des sous-agents correcteurs).
2. **Python** : `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` (dans le dossier).
3. **VPN AFM** actif sur la machine (le SFTP `90.83.66.173:9822` ne répond que sous VPN).
   Test : `.venv/bin/python -c "import socket;s=socket.socket();s.settimeout(15);print(s.connect_ex(('90.83.66.173',9822)))"` → doit afficher `0`.
4. **Claude Code** connecté au compte Team, lancé depuis le dossier : `cd ~/tlmc-remise && claude`.
5. Pour écrire dans le « Suivi saisie paiement SUSI » : avoir un **accès en édition** au Google Sheet
   (ID `1exk9CYqd0lYvtxAiXpKko0o2o04_8HtENGEbrFt6CS8`) et un connecteur Google Sheets (Zapier ou Drive)
   branché sur ce compte. Sinon, la session livre un xlsx patché + les instructions, et Djibril applique.

## Lancer un traitement (prompt à copier-coller)

> /tlmc-remise-sftp Traite les journées du **JJ et JJ août 2026** du flux **OUT** :
> dossiers SFTP `/POUR_OUTSOURCIA/BDC/OUT/08 AOUT 2026/JJMM` et `/POUR_OUTSOURCIA/PAIEMENTS /OUT/08 AOUT 2026/JJMM`.
> Ensuite remplis la colonne F (Montant endossé) des onglets « OUT JJ-MM » du Suivi saisie paiement SUSI,
> fais les premières corrections et pose en commentaire (colonne I) les instructions « ACTION SUSI » pour
> corriger la cohérence. Ne touche JAMAIS aux colonnes E, G (Cohérence) et H.

Pour le flux AA (équipe Mada), remplacer les dossiers par `/POUR_OUTSOURCIA/BDC/08 AOUT 2026/JJMM` et
`/POUR_OUTSOURCIA/PAIEMENTS /08 AOUT 2026/JJMM`, et les onglets par « AA JJ-MM ».

## Règles à ne pas transgresser (la skill les connaît, mais en cas de doute)

- **Une seule personne par journée de lots** (sinon doubles remises). Se coordonner avant de lancer.
- Numéros de remise : AA → `JJMMNN` (PAIEMENTS) / `JJ(MM+50)NN` (FID) / `JJ(MM+60)NN` (REC) ;
  **OUT → `JJ(MM+70)NN` (PAIEMENTS) / `JJ(MM+80)NN` (FID) / `JJ(MM+90)NN` (REC)** — jamais les mêmes
  numéros que le flux AA du même jour.
- Jamais isoler un chèque lisible ; les lettres font foi sur le montant ; chèque non signé ou illisible
  → signalé en commentaire, décision humaine.
- Colonne G du suivi = formule Cohérence : ne JAMAIS y écrire. Commentaires en colonne I uniquement.
- L'outil hébergé (https://tlmc-remise.fly.dev, auth dans `.env`) est **partagé** : les lots déjà déposés
  sont détectés par nom de fichier et ne sont pas relus.

## Si ça bloque

- Lots qui ressortent tous en « rejets » avec un motif « credit balance is too low » : les crédits API
  Anthropic du serveur sont épuisés → prévenir Djibril (rechargement console.anthropic.com, ce ne sont
  pas les crédits Claude du plan Team).
- SFTP injoignable : vérifier le VPN.
- Le récap final attendu : Google Sheet (lot / montant endossé / n° remise / liens TLMC) + colonne F
  remplie + lignes rouges commentées « ACTION SUSI » pour chaque écart.
