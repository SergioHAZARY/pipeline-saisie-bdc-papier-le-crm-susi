#!/usr/bin/env python3
"""CLI de génération d'une remise TLMC à partir de chèques scannés.

Usage :
  python3 remise.py --input ./scans/ --output sortie/remise_YYYYMMDD.tlmc

Version web : python3 app.py (upload local ou lien Google Drive).
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from tlmc import charger_env
from tlmc.pipeline import ecrire_csv_controle, ecrire_csv_rejets, traiter_dossier

RACINE = Path(__file__).parent


def main() -> int:
    charger_env()
    parser = argparse.ArgumentParser(description="Génère une remise TLMC depuis des scans de chèques.")
    parser.add_argument("--input", type=Path, default=RACINE / "scans", help="dossier des scans (PDF/JPG/PNG)")
    parser.add_argument("--output", type=Path, default=None, help="fichier .tlmc de sortie")
    parser.add_argument("--spec", type=Path, default=RACINE / "spec", help="dossier contenant layout.json")
    parser.add_argument("--rejets", type=Path, default=RACINE / "rejets", help="dossier des images rejetées")
    args = parser.parse_args()

    sortie_tlmc = args.output or RACINE / "sortie" / f"remise_{date.today():%Y%m%d}.tlmc"

    if not args.input.exists() or not any(args.input.iterdir()):
        print(f"Aucun scan trouvé dans {args.input}", file=sys.stderr)
        return 1

    resultat = traiter_dossier(args.input, args.rejets, sortie_tlmc, args.spec)

    controle = ecrire_csv_controle(resultat, sortie_tlmc.parent / "controle.csv")
    ecrire_csv_rejets(resultat, args.rejets / "rejets.csv")

    print(f"\n{len(resultat.retenus)} chèque(s) retenu(s), {len(resultat.rejets)} rejet(s) "
          f"— total {resultat.total_centimes / 100:.2f} €")
    if controle:
        print(f"Contrôle : {controle}")
    if not resultat.retenus:
        return 1
    if resultat.erreur_spec:
        print(f"\n⚠️  {resultat.erreur_spec}", file=sys.stderr)
        return 2
    print(f"Fichier TLMC : {resultat.chemin_tlmc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
