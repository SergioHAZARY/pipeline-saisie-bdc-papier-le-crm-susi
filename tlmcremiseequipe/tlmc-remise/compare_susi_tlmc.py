#!/usr/bin/env python3
"""Rapprochement d'un lot : chèques TLMC (plateforme fly) vs paiements SUSI (extracts .xls).

Usage :
  python3 compare_lot.py --session ftp-24082026FR-ATLAS-III-PAIEMENTS-50CH007 \
      --susi /Users/djisse/susi-rapports/out/AA_24-08/LotPayement_P278769.xls [autre.xls ...] \
      [--out verdict.json]

Sortie (JSON sur stdout et/ou --out) :
  tlmc  : {n, total, cheques:[{montant, titulaire, banque, page, image_url}]}
  susi  : {n, total, par_lot:{P...: total}, paiements:[{montant, nom, lot, annulation}]}
  extra_tlmc : chèques endossés sans paiement SUSI au même montant (candidats « à saisir »)
  extra_susi : paiements SUSI sans chèque TLMC au même montant (candidats « à corriger/retirer »)
  paires_suspectes : rapprochement par nom entre extra_tlmc et extra_susi (montant mal saisi)
"""
import argparse, base64, json, os, re, sys, unicodedata, urllib.request

FLY = os.environ.get("TLMC_URL", "https://tlmc-remise.fly.dev")
AUTH = os.environ.get("TLMC_AUTH", "cdpafm1:F4GxbXczikA")


def http_json(url):
    req = urllib.request.Request(url)
    req.add_header("Authorization", "Basic " + base64.b64encode(AUTH.encode()).decode())
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def lire_tlmc(session):
    d = http_json(f"{FLY}/api/lot/{urllib.request.quote(session)}")
    cheques = []
    for c in d.get("cheques", []):
        if c.get("isole"):
            continue
        try:
            m = round(float(str(c.get("montant_eur", "")).replace(",", ".")), 2)
        except ValueError:
            continue
        cheques.append({"montant": m, "titulaire": c.get("titulaire", ""),
                        "banque": c.get("banque", ""), "page": c.get("page"),
                        "image_url": FLY + c.get("image_url", "")})
    dt = d.get("dernier_tlmc") or {}
    return {"n": len(cheques), "total": round(sum(c["montant"] for c in cheques), 2),
            "cheques": cheques, "tlmc_fichier": dt.get("fichier"),
            "numero_remise": dt.get("numero_remise"),
            "total_tlmc_officiel": (dt.get("total_centimes") or 0) / 100,
            "rejets": len(d.get("rejets") or []), "isoles": sum(1 for c in d.get("cheques", []) if c.get("isole"))}


def lire_susi(chemins):
    import xlrd
    paiements, par_lot = [], {}
    for chemin in chemins:
        wb = xlrd.open_workbook(chemin)
        sh = wb.sheet_by_index(0)
        icol = None
        for i in range(sh.nrows):
            vals = [str(c.value).strip() for c in sh.row(i)]
            if "Nom client" in vals:
                icol = {"lot": vals.index("Lot"), "nom": vals.index("Nom client"),
                        "montant": vals.index("Montant paiement"),
                        "annul": vals.index("Montant annulation"),
                        "net": vals.index("Montant Net")}
                continue
            if icol is None:
                continue
            lot = vals[icol["lot"]] if icol["lot"] < len(vals) else ""
            if not lot.startswith("P"):
                continue
            def num(j):
                try:
                    return round(float(vals[j].replace(",", "")), 2)
                except (ValueError, IndexError):
                    return 0.0
            net, annul = num(icol["net"]), num(icol["annul"])
            paiements.append({"montant": net, "nom": vals[icol["nom"]], "lot": lot,
                              "annulation": annul, "fichier": os.path.basename(chemin)})
            par_lot[lot] = round(par_lot.get(lot, 0) + net, 2)
    actifs = [p for p in paiements if p["montant"] != 0]
    return {"n": len(actifs), "total": round(sum(p["montant"] for p in actifs), 2),
            "par_lot": par_lot, "paiements": paiements}


def simplifie(nom):
    s = unicodedata.normalize("NFD", str(nom).upper())
    s = "".join(ch for ch in s if ch.isalpha() or ch == " ")
    mots = [m for m in s.split() if m not in {"M", "MME", "MLLE", "MR", "MADAME", "MONSIEUR"} and len(m) > 2]
    return set(mots)


def comparer(tlmc, susi):
    from collections import Counter
    ct = Counter(c["montant"] for c in tlmc["cheques"])
    cs = Counter(p["montant"] for p in susi["paiements"] if p["montant"] != 0)
    seulement_t = ct - cs
    seulement_s = cs - ct
    extra_t, extra_s = [], []
    vus = Counter()
    for c in tlmc["cheques"]:
        if vus[c["montant"]] < seulement_t.get(c["montant"], 0):
            vus[c["montant"]] += 1
            extra_t.append(c)
    vus = Counter()
    for p in susi["paiements"]:
        if p["montant"] != 0 and vus[p["montant"]] < seulement_s.get(p["montant"], 0):
            vus[p["montant"]] += 1
            extra_s.append(p)
    paires = []
    for t in extra_t:
        nt = simplifie(t["titulaire"])
        for s in extra_s:
            if nt & simplifie(s["nom"]):
                paires.append({"tlmc": t, "susi": s,
                               "hypothese": f"montant mal saisi dans SUSI : {s['nom']} saisi {s['montant']:.2f} au lieu de {t['montant']:.2f} (p{t['page']})"})
    return extra_t, extra_s, paires


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", required=True)
    ap.add_argument("--susi", nargs="+", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    tlmc = lire_tlmc(a.session)
    susi = lire_susi(a.susi)
    extra_t, extra_s, paires = comparer(tlmc, susi)
    res = {"session": a.session, "tlmc": {k: v for k, v in tlmc.items() if k != "cheques"},
           "susi": {k: v for k, v in susi.items() if k != "paiements"},
           "ecart_total": round(tlmc["total"] - susi["total"], 2),
           "extra_tlmc": extra_t, "extra_susi": extra_s, "paires_suspectes": paires,
           "coherent": not extra_t and not extra_s}
    txt = json.dumps(res, ensure_ascii=False, indent=1)
    print(txt)
    if a.out:
        open(a.out, "w").write(txt)


if __name__ == "__main__":
    main()
