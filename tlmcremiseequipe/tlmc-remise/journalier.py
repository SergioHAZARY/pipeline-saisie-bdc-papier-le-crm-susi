"""Fusionne tous les TLMC d'un jour (manifest drive_recap) en UNE remise.

Convention validée le 25/08/2026 (dossier sessions/drive_recap/JOURNALIERS/) :
- numéro de remise = JJMM00, date de remise = date du jour des lots ;
- enregistrements 320 caractères séparés par \r SEUL, pas de \r final ;
- en-tête 03 / total 08 repris du premier lot, patchés : date [10:18],
  numéro en [67:74] ('0'+JJMM00) et [128:134] ;
- détails 04 concaténés, séquence [4:10] renumérotée 2..n+1, référence n24
  [93:117] = 300000+i cadrée droite sur des zéros ;
- total 08 : séquence n+2, montant total [172:184], nb chèques [184:190].

Usage : .venv/bin/python journalier.py --manifest sessions/drive_recap/manifest_AA_24-07_24-07.json
"""
import argparse
import datetime as dt
import json
from pathlib import Path

DOSSIER = Path(__file__).parent / "sessions" / "drive_recap" / "JOURNALIERS"


def fusionner(manifest_path: Path, date_remise: dt.date | None = None) -> list[dict]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resultats = []
    for onglet, lignes in manifest["onglets"].items():
        # onglet « AA 24-07 » → numéro JJMM00 ; date de remise = date du jour
        # des lots, sauf date forcée (--date)
        jj, mm = onglet.split()[1].split("-")
        annee = dt.datetime.strptime(manifest["debut"], "%Y-%m-%d").year
        jour = date_remise or dt.date(annee, int(mm), int(jj))
        numero = f"{jj}{mm}00"

        # dédoublonne (les lignes éclatées du suivi pointent le même TLMC)
        chemins, vus = [], set()
        for l in lignes:
            c = l.get("chemin")
            if c and c not in vus:
                vus.add(c)
                chemins.append(Path(c))
        if not chemins:
            continue

        details, total_lots = [], 0
        header = tail = None
        for ch in sorted(chemins, key=lambda p: p.name):
            recs = ch.read_bytes().split(b"\r")
            assert all(len(r) == 320 for r in recs), f"record ≠ 320c : {ch.name}"
            assert recs[0][:4] == b"03CH" and recs[-1][:4] == b"08CH", ch.name
            if header is None:
                header, tail = bytearray(recs[0]), bytearray(recs[-1])
            details.extend(recs[1:-1])
            total_lots += int(recs[-1][172:184])

        n = len(details)
        date_b = jour.strftime("%Y%m%d").encode()
        for zone in (header, tail):
            zone[10:18] = date_b
            zone[67:74] = b"0" + numero.encode()
            zone[128:134] = numero.encode()
        header[4:10] = b"000001"
        tail[4:10] = f"{n + 2:06d}".encode()
        tail[172:184] = f"{total_lots:012d}".encode()
        tail[184:190] = f"{n:06d}".encode()

        corps = []
        for i, d in enumerate(details, start=1):
            d = bytearray(d)
            assert d[:4] == b"04CH", "détail inattendu"
            d[4:10] = f"{i + 1:06d}".encode()
            d[93:117] = f"{300000 + i:024d}".encode()
            corps.append(bytes(d))

        contenu = b"\r".join([bytes(header), *corps, bytes(tail)])
        # contrôles : total = somme des totaux de lots, comptes cohérents
        assert int(bytes(tail)[172:184]) == total_lots
        assert len(contenu.split(b"\r")) == n + 2
        assert not contenu.endswith(b"\r")

        DOSSIER.mkdir(parents=True, exist_ok=True)
        nom = f"REMISE OUTSOURCIA OUT DU {jour:%d.%m.%Y} tlmc_{numero}.txt"
        (DOSSIER / nom).write_bytes(contenu)
        resultats.append({"onglet": onglet, "fichier": nom, "nb_lots": len(chemins),
                          "nb_cheques": n, "montant": total_lots / 100})
        print(f"{onglet} : {len(chemins)} lots, {n} chèques, "
              f"{total_lots / 100:.2f} € → {nom}")
    return resultats


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--manifest", required=True, nargs="+")
    p.add_argument("--date", help="JJ/MM/AAAA — force la date de remise (défaut : date du jour des lots)")
    a = p.parse_args()
    d = dt.datetime.strptime(a.date, "%d/%m/%Y").date() if a.date else None
    for m in a.manifest:
        fusionner(Path(m), d)
