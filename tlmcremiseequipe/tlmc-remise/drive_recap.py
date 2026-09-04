#!/usr/bin/env python3
"""Remise TLMC → Google Drive : rapprochement, téléchargement et récap.

Sous-commandes :
  manifest --debut JJ/MM --fin JJ/MM --prefixe AA|OUT [--annee AAAA] [--regenerer]
      Exporte le « Suivi saisie paiement SUSI », rapproche chaque ligne des
      onglets `<PREFIXE> JJ-MM` de la période avec les sessions du générateur
      TLMC hébergé (/api/lots + etat.json), RÉGÉNÈRE chaque TLMC avec
      --regenerer (POST /api/lot/{session}/tlmc, date de remise = date du lot,
      numéro de remise conservé), télécharge les fichiers dans
      sessions/drive_recap/<PREFIXE JJ-MM>/ et écrit
      sessions/drive_recap/manifest_<PREFIXE>.json (montant lu dans
      l'enregistrement total « 08 » du fichier TLMC lui-même).

  recap --manifest M.json --liens L.json --sortie R.xlsx
      Construit le classeur récap : une feuille par jour, colonnes
      N° lot / montant TLMC / lien Drive / commentaire, ligne TOTAL.
      L.json : {"<onglet>": {"<nom du fichier tlmc>": "<url drive>", ...}, ...}
      (produit par les sous-agents d'upload Drive ; facultatif : sans lui les
      liens restent vides).

Le montant affiché vient du fichier TLMC (enregistrement 08, positions 173-184,
centimes), jamais du sheet : c'est le montant réellement endossé.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import unicodedata
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / ".venv/lib"))

RACINE = Path(__file__).parent
DOSSIER = RACINE / "sessions" / "drive_recap"
SHEET_SUIVI = "1exk9CYqd0lYvtxAiXpKko0o2o04_8HtENGEbrFt6CS8"
TLMC_URL = os.environ.get("TLMC_URL", "https://tlmc-remise.fly.dev")


def env():
    for ligne in (RACINE / ".env").read_text().splitlines():
        if "=" in ligne and not ligne.startswith("#"):
            k, v = ligne.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def client():
    import httpx
    env()
    return httpx.Client(base_url=TLMC_URL,
                        auth=(os.environ["TLMC_UTILISATEUR"], os.environ["TLMC_MOT_DE_PASSE"]),
                        timeout=httpx.Timeout(120, connect=30))


# ---------------------------------------------------------------- rapprochement
TYPES = ("PAIEMENTS", "FID", "REC")


def cle_lot(nom: str):
    """(date, type, complexes?, index CH) — index None si lot non numéroté (alors on garde le nb de chèques)."""
    n = unicodedata.normalize("NFKD", nom.upper()).replace(".PDF", "")
    n = re.sub(r"\s+", " ", n).strip()
    m_date = re.match(r"(\d{6,8})", n)
    # l'index doit suivre CH immédiatement : « 18CH 1 » est un lot de 18 chèques
    # (le « 1 » traînant est un artefact du sheet), pas le lot n° 1
    m_ch = re.search(r"(\d+)\s*CH0*(\d*)", n)
    if not m_date or not m_ch:
        return None
    typ = next((t for t in TYPES if t in n), "?")
    complexe = "COMPLEXE" in n
    date = m_date.group(1)
    if len(date) == 7:  # « 6082026 » → 06082026
        date = "0" + date
    idx = m_ch.group(2)
    return (date, typ, complexe, int(idx) if idx else None, None if idx else int(m_ch.group(1)))


def apparier(nom_ligne: str, sessions: dict):
    """sessions : cle_lot(source) -> [lots api]. Retourne le lot api ou None."""
    k = cle_lot(nom_ligne)
    if k is None:
        return None
    if k in sessions:
        return sessions[k][0]
    if k[3] is not None:  # numéroté : tolère un nb de chèques différent (déjà ignoré) — rien d'autre à tenter
        return None
    # non numéroté : tolère un écart sur le nb de chèques s'il n'y a qu'un candidat du même jour/type
    cands = [l for kk, ls in sessions.items() for l in ls
             if kk[0] == k[0] and kk[1] == k[1] and kk[2] == k[2] and kk[3] is None]
    return cands[0] if len(cands) == 1 else None


def montant_tlmc(chemin: Path):
    """Montant (euros) + nb de chèques lus dans l'enregistrement total « 08 »."""
    texte = chemin.read_text(encoding="ascii", errors="replace")
    for ligne in re.split(r"[\r\n]+", texte):
        if ligne.startswith("08") and len(ligne) >= 190:
            return int(ligne[172:184]) / 100, int(ligne[185:190])
    raise ValueError(f"enregistrement 08 introuvable dans {chemin.name}")


def onglets_periode(noms, prefixe, debut, fin):
    sel = []
    for n in noms:
        m = re.fullmatch(rf"{prefixe} (\d{{2}})-(\d{{2}})", n.strip())
        if not m:
            continue
        d = dt.date(debut.year, int(m.group(2)), int(m.group(1)))
        if debut <= d <= fin:
            sel.append((d, n))
    return [n for _, n in sorted(sel)]


def date_du_lot(nom: str, onglet: str, annee: int):
    """Date de remise = date portée par le nom du lot ; secours : date de l'onglet."""
    k = cle_lot(nom)
    if k:
        d = k[0]
        try:
            return dt.date(int(d[4:8]), int(d[2:4]), int(d[0:2]))
        except ValueError:
            pass
    m = re.search(r"(\d{2})-(\d{2})$", onglet.strip())
    return dt.date(annee, int(m.group(2)), int(m.group(1)))


NUMERO_DEFAUT = "005278"  # numero_remise du layout : jamais un vrai numéro de lot


def numeros_historiques():
    """nom du PDF -> numéro de remise d'origine, d'après les rapports agent_ftp.
    Certaines régénérations passées ont écrasé le numéro de sessions avec le
    défaut du layout (005278) ; les rapports CSV gardent le bon numéro."""
    mapping = {}
    for nom in ("rapport.csv", "rapport_BDC.csv", "rapport_PAIEMENTS.csv"):
        f = RACINE / "sessions" / "agent_ftp" / nom
        if not f.exists():
            continue
        for ligne in f.read_text(encoding="utf-8").splitlines():
            m = re.search(r"([^,/]+\.pdf),[0-9.]*,.*tlmc_(\d+)\.txt", ligne)
            if m and m.group(2) != NUMERO_DEFAUT:
                mapping.setdefault(m.group(1).strip(), m.group(2))
    return mapping


def numero_de_remise(lot: dict, historiques: dict | None = None):
    """Numéro de remise du lot ; le défaut du layout est remplacé par le
    numéro historique du rapport (sinon collision de noms de fichiers)."""
    n = str(lot.get("numero_remise") or "").strip()
    if not n:
        fich = (lot.get("dernier_tlmc") or {}).get("fichier", "")
        m = re.search(r"tlmc_(\d+)\.txt", fich)
        n = m.group(1) if m else ""
    if n == NUMERO_DEFAUT and historiques:
        src = (lot.get("source") or "").strip()
        n = historiques.get(src, n)
    return n or None


def regenerer_et_telecharger(cl, sess, numero, date_remise, rep: Path, regenerer=True, fichier_existant=None):
    """POST /api/lot/{sess}/tlmc puis téléchargement du fichier frais.
    Sans `regenerer`, télécharge simplement `fichier_existant`."""
    if regenerer:
        r = cl.post(f"/api/lot/{sess}/tlmc",
                    json={"date_remise": date_remise.isoformat(), "numero_remise": numero})
        if r.status_code != 200:
            try:
                msg = r.json().get("erreur", r.text)
            except Exception:
                msg = r.text
            return {"erreur": f"régénération refusée : {msg[:200]}"}
        fich = r.json()["fichier"]
    else:
        fich = fichier_existant
    local = rep / fich
    r2 = cl.get(f"/telecharger/{sess}/{fich}")
    r2.raise_for_status()
    local.write_bytes(r2.content)
    m, nb = montant_tlmc(local)
    return {"fichier": fich, "chemin": str(local), "montant": m, "nb": nb}


def cmd_manifest(a):
    import openpyxl
    from concurrent.futures import ThreadPoolExecutor
    debut = dt.datetime.strptime(f"{a.debut}/{a.annee}", "%d/%m/%Y").date()
    fin = dt.datetime.strptime(f"{a.fin}/{a.annee}", "%d/%m/%Y").date()
    prefixe = a.prefixe.upper()

    chemin_xlsx = Path(tempfile.gettempdir()) / f"suivi_{SHEET_SUIVI}.xlsx"
    tmp = chemin_xlsx.with_suffix(f".part{os.getpid()}")
    urllib.request.urlretrieve(
        f"https://docs.google.com/spreadsheets/d/{SHEET_SUIVI}/export?format=xlsx", tmp)
    os.replace(tmp, chemin_xlsx)
    wb = openpyxl.load_workbook(chemin_xlsx, read_only=True)

    cl = client()
    lots = {l["session"]: l for l in cl.get("/api/lots").json() if l.get("session")}
    # complète avec etat.json : sessions générées mais absentes de /api/lots
    # (certaines sessions relancées portent un `source` qui n'est pas le nom du
    #  PDF — etat.json fait foi pour le rapprochement nom de lot → session)
    etat = RACINE / "sessions" / "agent_ftp" / "etat.json"
    if etat.exists():
        for nom_pdf, f in json.loads(etat.read_text(encoding="utf-8"))["fichiers"].items():
            s = f.get("session")
            if s and f.get("tlmc") and cle_lot((lots.get(s) or {}).get("source") or "") is None:
                lots[f"{s}#etat"] = {"session": s, "source": nom_pdf,
                                     "dernier_tlmc": {"fichier": f["tlmc"]["fichier"]},
                                     "numero_remise": f.get("numero_remise")}
    sessions: dict = {}
    for l in lots.values():
        k = cle_lot(l.get("source") or "")
        if k:
            sessions.setdefault(k, []).append(l)

    # phase A — rapprochement ligne par ligne
    DOSSIER.mkdir(parents=True, exist_ok=True)
    historiques = numeros_historiques()
    manifest = {"prefixe": prefixe, "debut": str(debut), "fin": str(fin),
                "regenere": bool(a.regenerer),
                "genere_le": dt.datetime.now().isoformat(timespec="seconds"), "onglets": {}}
    a_traiter = {}  # session -> (numero, date_remise, rep)
    for onglet in onglets_periode(wb.sheetnames, prefixe, debut, fin):
        entrees = []
        for r in wb[onglet].iter_rows(values_only=True):
            nom = str(r[0]).strip() if r and r[0] else ""
            if not re.search(r"\d\s*CH", nom.upper()):
                continue
            brut_f = r[5] if len(r) > 5 else None
            try:
                suivi = round(float(str(brut_f).replace(" ", "").replace(" ", "")
                                    .replace(",", ".")), 2) if brut_f not in (None, "") else None
            except (TypeError, ValueError):
                suivi = None
            e = {"nom_lot": nom, "session": None, "fichier_tlmc": None, "chemin": None,
                 "montant_tlmc": None, "nb_cheques": None, "montant_suivi": suivi,
                 "statut": "tlmc_manquant", "commentaire": ""}
            lot = apparier(nom, sessions)
            if lot and lot.get("dernier_tlmc"):
                e["session"] = lot["session"]
                e["statut"] = "attente"
                src = (lot.get("source") or "").replace(".pdf", "")
                if cle_lot(src) and src.upper() != re.sub(r"\s+", " ", nom.upper()):
                    e["commentaire"] = f"apparié au lot « {src} »"
                numero = numero_de_remise(lot, historiques)
                if numero is None:
                    e.update(statut="sans_tlmc", commentaire="numéro de remise introuvable — non régénéré")
                else:
                    rep = DOSSIER / onglet.replace(" ", "_")
                    rep.mkdir(exist_ok=True)
                    date_r = (dt.datetime.strptime(a.date_remise, "%d/%m/%Y").date()
                              if getattr(a, "date_remise", None) else date_du_lot(nom, onglet, a.annee))
                    a_traiter.setdefault(lot["session"],
                                         (numero, date_r, rep,
                                          lot["dernier_tlmc"]["fichier"]))
            elif lot:
                e.update(session=lot["session"], statut="sans_tlmc",
                         commentaire="lot lu mais TLMC non généré (chèques à corriger ?)")
            else:
                e["commentaire"] = "aucune session dans le générateur TLMC (scan non lu)"
            entrees.append(e)
        manifest["onglets"][onglet] = entrees

    # phase B — régénération + téléchargement en parallèle
    print(f"{len(a_traiter)} lot(s) à {'régénérer' if a.regenerer else 'télécharger'}…")
    resultats = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(regenerer_et_telecharger, cl, s, n, d, rep, a.regenerer, fich): s
                for s, (n, d, rep, fich) in a_traiter.items()}
        for fut, s in futs.items():
            try:
                resultats[s] = fut.result()
            except Exception as exc:
                resultats[s] = {"erreur": f"{type(exc).__name__} : {exc}"}

    # phase C — reporter les résultats sur les lignes
    for onglet, entrees in manifest["onglets"].items():
        deja = {}
        for e in entrees:
            if e["statut"] != "attente":
                continue
            res = resultats.get(e["session"], {"erreur": "session non traitée"})
            if "erreur" in res:
                e.update(statut="sans_tlmc",
                         commentaire=(e["commentaire"] + " | " if e["commentaire"] else "") + res["erreur"])
                continue
            e.update(fichier_tlmc=res["fichier"], chemin=res["chemin"],
                     montant_tlmc=res["montant"], nb_cheques=res["nb"], statut="ok")
            if res["fichier"] in deja:
                e["statut"] = "doublon"
                e["commentaire"] = (e["commentaire"] + " | " if e["commentaire"] else "") + \
                    f"même TLMC que la ligne « {deja[res['fichier']]} »"
            deja.setdefault(res["fichier"], e["nom_lot"])
        # écart vs suivi : la colonne F d'un lot éclaté est répartie sur ses
        # lignes → comparer le TLMC à la SOMME des F des lignes du même fichier
        par_fichier = {}
        for e in entrees:
            if e["fichier_tlmc"]:
                par_fichier.setdefault(e["fichier_tlmc"], []).append(e)
        for fich, grp in par_fichier.items():
            fs = [x["montant_suivi"] for x in grp if x["montant_suivi"] is not None]
            if not fs:
                continue
            principal = next(x for x in grp if x["statut"] == "ok")
            # F d'un lot éclaté : tantôt réparti sur les lignes (somme), tantôt
            # déjà totalisé sur la ligne principale (la ligne éclatée est alors
            # redondante) — pas d'écart si l'une des deux lectures colle
            candidats = {round(sum(fs), 2), round(principal["montant_suivi"] or 0, 2)}
            if any(abs(principal["montant_tlmc"] - f) <= 0.005 for f in candidats):
                continue
            total_f = round(sum(fs), 2)
            d = principal["montant_tlmc"] - total_f
            principal["commentaire"] = (principal["commentaire"] + " | " if principal["commentaire"] else "") + \
                f"ÉCART vs suivi SUSI (col. F = {total_f:.2f} €) : {d:+.2f} €"
        ok = sum(1 for e in entrees if e["statut"] in ("ok", "doublon"))
        print(f"{onglet:12} {len(entrees):3} lignes | {ok:3} TLMC | "
              f"{sum(e['montant_tlmc'] or 0 for e in entrees if e['statut'] == 'ok'):10.2f} €")

    # nom par période : deux sessions travaillant sur des périodes différentes
    # ne s'écrasent plus mutuellement
    sortie = DOSSIER / f"manifest_{prefixe}_{debut:%d-%m}_{fin:%d-%m}.json"
    sortie.write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nmanifest : {sortie}")


# ------------------------------------------------------------------------ recap
def cmd_recap(a):
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill

    manifest = json.loads(Path(a.manifest).read_text(encoding="utf-8"))
    liens = json.loads(Path(a.liens).read_text(encoding="utf-8")) if a.liens else {}

    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    gras = Font(bold=True)
    entete = PatternFill("solid", fgColor="1F4E79")
    rouge = PatternFill("solid", fgColor="FFC7CE")
    for onglet, entrees in manifest["onglets"].items():
        if not entrees:  # journée sans lot de chèques
            continue
        ws = wb.create_sheet(onglet)
        ws.append(["N° lot", "Montant TLMC (€)", "Nb chèques", "Fichier TLMC", "Lien Google Drive", "Commentaire"])
        for c in ws[1]:
            c.font = Font(bold=True, color="FFFFFF")
            c.fill = entete
        # montant affiché ligne à ligne : reflète la répartition du suivi SUSI
        # (colonne F) quand un lot est éclaté sur plusieurs lignes, pour que
        # « Montant TLMC » = « Montant endossé » sur CHAQUE ligne du sheet
        montant_affiche = {}
        groupes = {}
        for e in entrees:
            if e["fichier_tlmc"]:
                groupes.setdefault(e["fichier_tlmc"], []).append(e)
        for fich, grp in groupes.items():
            principal = next(x for x in grp if x["statut"] == "ok")
            total = principal["montant_tlmc"]
            fs = [x["montant_suivi"] for x in grp if x.get("montant_suivi") is not None]
            if len(grp) > 1 and fs and abs(round(sum(fs), 2) - total) <= 0.005:
                # F réparti sur les lignes → afficher la part de chaque ligne
                for x in grp:
                    montant_affiche[id(x)] = x.get("montant_suivi")
            else:
                montant_affiche[id(principal)] = total
                for x in grp:
                    if x is not principal and x.get("montant_suivi") is not None:
                        # ligne éclatée redondante : F déjà inclus dans le total
                        # de la ligne principale — affiché pour coller au suivi,
                        # mais NON compté dans le TOTAL du jour
                        montant_affiche[id(x)] = x["montant_suivi"]
                        x["commentaire"] = ((x["commentaire"] + " | ") if x["commentaire"] else "") + \
                            f"déjà compris dans la ligne principale (le suivi compte ce montant deux fois)"
        for e in entrees:
            url = liens.get(onglet, {}).get(e["fichier_tlmc"] or "", "")
            ws.append([e["nom_lot"],
                       montant_affiche.get(id(e)),
                       e["nb_cheques"] if e["statut"] != "doublon" else None,
                       e["fichier_tlmc"] or "",
                       url,
                       e["commentaire"] or ("" if e["statut"] in ("ok", "doublon") else "TLMC MANQUANT")])
            lig = ws.max_row
            if url:
                ws.cell(lig, 5).hyperlink = url
                ws.cell(lig, 5).font = Font(color="0563C1", underline="single")
                ws.cell(lig, 5).value = e["fichier_tlmc"]
            if e["statut"] not in ("ok", "doublon"):
                for c in ws[lig]:
                    c.fill = rouge
        total = sum(e["montant_tlmc"] or 0 for e in entrees if e["statut"] == "ok")
        nb = sum(e["nb_cheques"] or 0 for e in entrees if e["statut"] == "ok")
        ws.append([])
        ws.append(["TOTAL", round(total, 2), nb, "", "", f"{sum(1 for e in entrees if e['statut'] == 'ok')} fichiers TLMC"])
        for c in ws[ws.max_row]:
            c.font = gras
        for col, larg in zip("ABCDEF", (45, 16, 11, 52, 52, 60)):
            ws.column_dimensions[col].width = larg
        ws.cell(2, 2).number_format = "# ##0.00"
        for lig in range(2, ws.max_row + 1):
            ws.cell(lig, 2).number_format = "# ##0.00"
        ws.freeze_panes = "A2"
        ws.cell(1, 1).alignment = Alignment(horizontal="left")
    wb.save(a.sortie)
    print(f"récap : {a.sortie} ({len(manifest['onglets'])} feuilles)")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("manifest")
    m.add_argument("--debut", required=True, help="JJ/MM")
    m.add_argument("--fin", required=True, help="JJ/MM")
    m.add_argument("--prefixe", required=True, choices=["AA", "OUT", "aa", "out", "Out"])
    m.add_argument("--annee", type=int, default=dt.date.today().year)
    m.add_argument("--regenerer", action="store_true",
                   help="régénère chaque TLMC (date de remise = date du lot) au lieu de reprendre l'existant")
    m.add_argument("--date-remise",
                   help="JJ/MM/AAAA — force cette date de remise à la régénération (défaut : date du lot)")
    m.set_defaults(fn=cmd_manifest)
    r = sub.add_parser("recap")
    r.add_argument("--manifest", required=True)
    r.add_argument("--liens")
    r.add_argument("--sortie", required=True)
    r.set_defaults(fn=cmd_recap)
    a = p.parse_args()
    a.fn(a)
