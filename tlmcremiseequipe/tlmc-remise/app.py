#!/usr/bin/env python3
"""UI web de remise TLMC.

- Dépôt de scans (local ou Google Drive) → lecture CMC7 + montants
- Chaque lot est persisté (sessions/<id>/session.json) : résultat consultable,
  valeurs éditables chèque par chèque, chèques isolables (retirés de la remise)
- Génération du fichier TLMC (format BRED 320 car., cf. spec/layout.json)

Lancement : .venv/bin/python -m uvicorn app:app --port 8742
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import date, datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from tlmc import charger_env
from tlmc.drive import DriveError, telecharger_drive
from tlmc.lots import (charger_lot, generer_tlmc, lister_lots,
                       lot_depuis_resultat, sauver_lot, stats_lot, valider_cheque)
from tlmc.pipeline import traiter_dossier

RACINE = Path(__file__).parent
SESSIONS = RACINE / "sessions"
SPEC = RACINE / "spec"

charger_env()
app = FastAPI(title="Remise TLMC")

# identifiant du process serveur : un traitement estampillé par un autre boot
# est forcément mort (le thread ne survit pas à un redémarrage)
import uuid
BOOT_ID = uuid.uuid4().hex

# ---------------------------------------------------------------------------
# Authentification + rôles (HTTP Basic) — les lots contiennent des données
# bancaires réelles. Comptes dans TLMC_UTILISATEURS :
#   "nom:motdepasse:role,nom2:motdepasse2:role2" avec role ∈ admin|editeur|viewer
#   - viewer  : consultation et téléchargements uniquement
#   - editeur : + dépôt/édition/isolation/génération TLMC (pas les corrections)
#   - admin   : tout (correction automatique, rejets, réintégrations…)
# Rétrocompatibilité : TLMC_UTILISATEUR/TLMC_MOT_DE_PASSE = un admin unique.
# ---------------------------------------------------------------------------
import base64
import os
import secrets as _secrets

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response as _Response

# chemins d'action réservés à l'admin (les « corrections »)
CHEMINS_CORRECTION = ("/autocorriger", "/rejet/", "/isole/")


def _charger_utilisateurs() -> dict:
    utilisateurs = {}
    for entree in os.environ.get("TLMC_UTILISATEURS", "").split(","):
        morceaux = entree.strip().split(":")
        if len(morceaux) == 3 and morceaux[2] in ("admin", "editeur", "viewer"):
            utilisateurs[morceaux[0]] = {"mdp": morceaux[1], "role": morceaux[2]}
    if not utilisateurs and os.environ.get("TLMC_MOT_DE_PASSE"):
        utilisateurs[os.environ.get("TLMC_UTILISATEUR", "atlas")] = {
            "mdp": os.environ["TLMC_MOT_DE_PASSE"], "role": "admin"}
    return utilisateurs


class AuthBasique(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        utilisateurs = _charger_utilisateurs()
        if utilisateurs:
            entete = request.headers.get("authorization", "")
            nom = mdp = ""
            if entete.startswith("Basic "):
                try:
                    nom, _, mdp = base64.b64decode(entete[6:]).decode(
                        "utf-8", "replace").partition(":")
                except Exception:
                    pass
            compte = utilisateurs.get(nom)
            if not compte or not _secrets.compare_digest(mdp, compte["mdp"]):
                return _Response(status_code=401,
                                 headers={"WWW-Authenticate": 'Basic realm="Remise TLMC"'})
            role = compte["role"]
            if request.method != "GET" and role == "viewer":
                return _Response(
                    "Compte en lecture seule : action non autorisée.",
                    status_code=403, media_type="text/plain; charset=utf-8")
            if request.method != "GET" and role == "editeur"                     and any(c in request.url.path for c in CHEMINS_CORRECTION):
                return _Response(
                    "Les corrections (automatiques, rejets, réintégrations) sont "
                    "réservées à l'administrateur.",
                    status_code=403, media_type="text/plain; charset=utf-8")
        return await call_next(request)


app.add_middleware(AuthBasique)

STYLE = """<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, sans-serif; max-width: 1120px; margin: 1.5rem auto; padding: 0 1rem; }
  h1 { font-size: 1.3rem; } h1 small { font-weight: normal; opacity: .6; font-size: .85rem; }
  fieldset { border: 1px solid #8884; border-radius: 8px; margin-bottom: 1rem; padding: .8rem 1rem; }
  legend { font-weight: 600; padding: 0 .4rem; }
  input[type=text] { padding: .35rem; border-radius: 5px; border: 1px solid #8886; }
  button { padding: .45rem 1rem; border-radius: 6px; border: none; background: #2563eb; color: #fff; cursor: pointer; }
  button:hover { background: #1d4ed8; }
  button.secondaire { background: #6b7280; }
  table { border-collapse: collapse; width: 100%; margin-top: .6rem; font-size: .82rem; }
  th, td { border: 1px solid #8884; padding: .25rem .4rem; text-align: left; vertical-align: top; }
  td input[type=text] { width: 100%; box-sizing: border-box; font-family: ui-monospace, monospace; font-size: .8rem; padding: .2rem; }
  td.montant input { width: 5.5rem; text-align: right; }
  .ok { color: #16a34a; } .rejet { color: #dc2626; } .warn { color: #d97706; }
  .isole { opacity: .45; text-decoration: line-through; }
  tr.isole td input { text-decoration: line-through; }
  .vignette { max-height: 46px; border-radius: 3px; cursor: zoom-in; }
  .lots a { text-decoration: none; }
  .spinner { display: inline-block; width: 1em; height: 1em; border: 3px solid #2563eb44;
             border-top-color: #2563eb; border-radius: 50%; animation: tourne 0.8s linear infinite;
             vertical-align: -0.15em; }
  @keyframes tourne { to { transform: rotate(360deg); } }
  .barre { height: 12px; background: #8883; border-radius: 99px; overflow: hidden; margin: .6rem 0; }
  .barre-int { height: 100%; background: linear-gradient(90deg, #2563eb, #16a34a);
               transition: width .5s; min-width: 4px; }
  .journal { font-family: ui-monospace, monospace; font-size: .8rem; opacity: .8;
             max-height: 10rem; overflow-y: auto; }
  .progression { border: 1px solid #8884; border-radius: 8px; padding: 1rem 1.2rem; }
  .pastille { display: inline-block; padding: .05rem .5rem; border-radius: 99px; font-size: .75rem; background: #8882; }
</style>"""


def page(html: str) -> str:
    return (f"<!doctype html><html lang='fr'><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>Remise TLMC</title>{STYLE}</head><body>{html}</body></html>")


# ---------------------------------------------------------------------------
# Progression des traitements en arrière-plan
# ---------------------------------------------------------------------------

def lire_progression(session_dir: Path) -> dict | None:
    f = session_dir / "progress.json"
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def ecrire_progression(session_dir: Path, prog: dict) -> None:
    import os
    prog["boot"] = BOOT_ID
    prog["maj"] = datetime.now().strftime("%H:%M:%S")
    tmp = session_dir / "progress.json.tmp"
    tmp.write_text(json.dumps(prog, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, session_dir / "progress.json")


def compter_pages(dossier_scans: Path) -> int:
    total = 0
    from tlmc.pipeline import collecter_fichiers
    for f in collecter_fichiers(dossier_scans):
        if f.suffix.lower() == ".pdf":
            try:
                import pymupdf
                with pymupdf.open(f) as doc:
                    total += len(doc)
            except Exception:
                total += 1
        else:
            total += 1
    return total


def _worker_traitement(session_dir: Path, source: str, lien_drive: str | None = None):
    prog = {"etat": "demarrage", "pages": 0, "total_pages": 0,
            "ok": 0, "rejets": 0, "ignores": 0, "journal": [], "source": source}
    try:
        if lien_drive:
            prog["etat"] = "telechargement"
            ecrire_progression(session_dir, prog)
            telecharger_drive(lien_drive, session_dir / "scans")

        prog["total_pages"] = compter_pages(session_dir / "scans")
        prog["etat"] = "lecture"
        ecrire_progression(session_dir, prog)

        def attendre_si_pause():
            while (session_dir / "pause.flag").exists():
                if prog.get("etat") != "pause":
                    prog["etat"] = "pause"
                    ecrire_progression(session_dir, prog)
                time.sleep(2)
            if prog.get("etat") == "pause":
                prog["etat"] = "lecture"
                ecrire_progression(session_dir, prog)

        def cb(evt):
            prog["pages"] += 1
            cle = {"ok": "ok", "rejet": "rejets", "ignore": "ignores"}[evt["statut"]]
            prog[cle] += 1
            if evt["statut"] != "ignore":
                libelle = {"ok": f"✓ p{evt['page']} — {evt.get('montant', '')} €",
                           "rejet": f"✗ p{evt['page']} — {evt.get('motif', '')[:80]}"}[evt["statut"]]
                prog["journal"] = (prog["journal"] + [libelle])[-8:]
            ecrire_progression(session_dir, prog)

        resultat = traiter_dossier(
            dossier_scans=session_dir / "scans",
            dossier_rejets=session_dir / "rejets",
            sortie_tlmc=session_dir / "sortie" / "remise.tlmc",
            dossier_spec=SPEC / "_aucune",
            dossier_cheques=session_dir / "cheques",
            sur_progression=cb,
            attendre_si_pause=attendre_si_pause,
            options=json.loads((session_dir / "options.json").read_text())
                    if (session_dir / "options.json").exists() else None,
        )
        lot_depuis_resultat(resultat, session_dir, source)
        prog["etat"] = "termine"
        ecrire_progression(session_dir, prog)
    except Exception as exc:
        prog["etat"] = "erreur"
        prog["message"] = str(exc)
        ecrire_progression(session_dir, prog)


def demarrer_traitement(session_dir: Path, source: str, lien_drive: str | None = None,
                        options: dict | None = None):
    import os
    if options is not None:
        tmp = session_dir / "options.json.tmp"
        tmp.write_text(json.dumps(options), encoding="utf-8")
        os.replace(tmp, session_dir / "options.json")
    ecrire_progression(session_dir, {"etat": "demarrage", "pages": 0, "total_pages": 0,
                                     "ok": 0, "rejets": 0, "ignores": 0,
                                     "journal": [], "source": source})
    threading.Thread(target=_worker_traitement,
                     args=(session_dir, source, lien_drive), daemon=True).start()


MOTIF_IMPRIME = None  # compilé au premier usage


def _autocorriger_worker(session_dir: Path):
    """Repasse sur les chèques signalés : corrections déterministes (n° imprimé)
    puis relecture zoomée de la bande CMC7. Ce qui reste est marqué pour
    correction manuelle."""
    import re
    from tlmc.ocr import cle_coherente, lire_bande_claude, tourner_180, zones_conformes

    def zones_ok(c):
        z1, z2, z3 = str(c.get("z1", "")), str(c.get("z2", "")), str(c.get("z3", ""))
        return (len(z1), len(z2), len(z3)) == (7, 12, 12) and (z1 + z2 + z3).isdigit()

    lot = charger_lot(session_dir)
    motif_imprime = re.compile(r"n° imprimé ([0-9]+) ≠ CMC7 ([0-9]+)")
    actionnable = re.compile(r"n° imprimé|zone [123]|lecture douteuse|CMC7|clé de contrôle \(\d+\) INVALIDE")
    cibles = [c for c in lot["cheques"] if not c.get("isole")
              and (not zones_ok(c) or any(actionnable.search(a) for a in c.get("avertissements", [])))]
    prog = {"etat": "autocorrection", "pages": 0, "total_pages": len(cibles),
            "ok": 0, "rejets": 0, "ignores": 0, "journal": [],
            "source": "correction automatique"}
    ecrire_progression(session_dir, prog)

    for c in cibles:
        corrections = []
        try:
            # 1) relecture zoomée de la bande si les zones sont anormales
            if not zones_ok(c) and c.get("image")                     and (session_dir / "cheques" / c["image"]).exists():
                releve = lire_bande_claude((session_dir / "cheques" / c["image"]).read_bytes())
                if releve and zones_conformes(releve):
                    nz1, nz2, nz3 = releve.split()
                    for champ, nv in (("z1", nz1), ("z2", nz2), ("z3", nz3)):
                        if str(c.get(champ, "")) != nv:
                            corrections.append(f"{champ} {c.get(champ, '')}→{nv} (bande zoomée)")
                            c[champ] = nv
            # 2) n° imprimé : la fin du numéro imprimé EST le n° CMC7
            for a in list(c.get("avertissements", [])):
                m = motif_imprime.search(a)
                if m and len(m.group(1)) >= 7:
                    vrai = m.group(1)[-7:]
                    if str(c.get("z1", "")) != vrai:
                        # un n° « imprimé » lu sur un chèque qui n'en a pas (BNP…) est une
                        # hallucination : on n'applique que si la clé RLMC confirme
                        if cle and cle_coherente(f"{vrai} {c.get('z2', '')} {c.get('z3', '')}", cle) is not True:
                            continue
                        corrections.append(f"z1 {c.get('z1', '')}→{vrai} (n° imprimé)")
                        c["z1"] = vrai
        except Exception as exc:
            corrections = []
            c.setdefault("avertissements", []).append(f"autocorrection en échec : {exc}")

        # drapeau « clé INVALIDE » périmé : la ligne actuelle valide la clé
        if cle and cle_coherente(f"{c.get('z1', '')} {c.get('z2', '')} {c.get('z3', '')}", cle) is True \
                and any("INVALIDE" in a for a in c.get("avertissements", [])):
            c["avertissements"] = [a for a in c.get("avertissements", []) if "INVALIDE" not in a]
            c["avertissements"].append(f"clé de contrôle ({cle}) ✓")
            if not corrections:
                corrections.append("drapeau clé invalide levé (ligne conforme à la clé)")
        if corrections:
            # retire les avertissements consommés, ajoute la trace de correction
            c["avertissements"] = [a for a in c.get("avertissements", [])
                                   if not actionnable.search(a)]
            c["avertissements"].append("corrigé auto : " + " ; ".join(corrections))
            prog["ok"] += 1
            prog["journal"] = (prog["journal"] + [f"✓ p{c.get('page', '?')} — " + " ; ".join(corrections)])[-8:]
        else:
            if not zones_ok(c):
                if "correction manuelle requise (corriger ou isoler)" not in c.get("avertissements", []):
                    c.setdefault("avertissements", []).append(
                        "correction manuelle requise (corriger ou isoler)")
            prog["rejets"] += 1
            prog["journal"] = (prog["journal"] + [f"✋ p{c.get('page', '?')} — pas de correction sûre"])[-8:]
        prog["pages"] += 1
        ecrire_progression(session_dir, prog)

    sauver_lot(session_dir, lot)
    prog["etat"] = "termine"
    ecrire_progression(session_dir, prog)


def traitement_actif(session_dir: Path) -> bool:
    prog = lire_progression(session_dir)
    if not prog or prog.get("etat") in ("termine", "erreur"):
        return False
    if prog.get("boot") != BOOT_ID:
        return False  # écrit par un process précédent : thread mort
    return (time.time() - (session_dir / "progress.json").stat().st_mtime) < 600


# ---------------------------------------------------------------------------
# Accueil : dépôt + liste des lots
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def accueil(q: str = "", p: int = 1):
    lots_html = []  # liste de (clé_de_recherche, html)
    for nom, lot in lister_lots(SESSIONS):
        s = stats_lot(lot)
        tlmc = lot.get("dernier_tlmc")
        badge_tlmc = (f" <span class='pastille ok'>TLMC : {escape(tlmc['fichier'])}</span>"
                      if tlmc else "")
        avert = sum(len(c.get("avertissements", [])) for c in lot["cheques"])
        source = lot.get("source", "")
        if source.startswith(("upload", "retraitement", "relance", "Google Drive")):
            # anciens lots : retrouver les noms de fichiers depuis le contenu
            fichiers = list(dict.fromkeys(
                c.get("fichier", "") for c in [*lot["cheques"], *lot.get("rejets", [])]
                if c.get("fichier")))
            if fichiers:
                source = ", ".join(fichiers[:2]) + (
                    f" +{len(fichiers) - 2} autre(s)" if len(fichiers) > 2 else "")
        lots_html.append((f"{nom} {source}".lower(),
            f"<li><a href='/lot/{escape(nom)}'><strong>{escape(nom)}</strong></a> — "
            f"📄 {escape(source)} · {s['nb_actifs']} chèque(s) actifs"
            + (f", {s['nb_isoles']} isolé(s)" if s['nb_isoles'] else "")
            + (f", <span class='rejet'>{s['nb_rejets']} rejet(s)</span>" if s['nb_rejets'] else "")
            + (f", <span class='warn'>{avert} avertissement(s)</span>" if avert else "")
            + f" · <strong>{s['total_centimes'] / 100:.2f} €</strong>{badge_tlmc}</li>"))
    # sessions sans session.json : en cours de traitement, ou orphelines
    noms_ok = {nom for nom, _ in lister_lots(SESSIONS)}
    if SESSIONS.exists():
        for d in sorted(SESSIONS.iterdir(), reverse=True):
            if not d.is_dir() or d.name in noms_ok:
                continue
            prog = lire_progression(d)
            if prog and prog.get("etat") not in ("termine",):
                if traitement_actif(d):
                    lots_html.append((d.name.lower(),
                        f"<li><a href='/lot/{escape(d.name)}'><strong>{escape(d.name)}</strong></a> — "
                        f"<span class='warn'>⏳ traitement en cours ({prog.get('pages', 0)}/"
                        f"{prog.get('total_pages', '?')} pages)</span></li>"))
                    continue
            if (d / "scans").exists() and any((d / "scans").iterdir()):
                lots_html.append((d.name.lower(),
                    f"<li><strong>{escape(d.name)}</strong> — <span class='rejet'>non traité"
                    f"{' (interrompu)' if prog else ''}</span> "
                    f"<form method='post' action='/lot/{escape(d.name)}/relancer' style='display:inline'>"
                    f"<button type='submit' class='secondaire'>Relancer le traitement</button></form></li>"))

    from urllib.parse import quote_plus
    total_lots = len(lots_html)
    if q.strip():
        recherche = q.strip().lower()
        lots_html = [e for e in lots_html if recherche in e[0]]
    PAR_PAGE = 15
    nb_pages = max(1, -(-len(lots_html) // PAR_PAGE))
    p = min(max(1, p), nb_pages)
    visibles = lots_html[(p - 1) * PAR_PAGE : p * PAR_PAGE]

    barre_recherche = f"""
<form method="get" action="/" style="margin:.4rem 0 .8rem">
  <input type="text" name="q" size="38" value="{escape(q)}"
         placeholder="Rechercher un lot par nom de fichier…">
  <button type="submit" class="secondaire">🔍 Rechercher</button>
  {f"<a href='/' style='margin-left:.6rem'>✕ effacer ({len(lots_html)}/{total_lots} lot(s))</a>" if q.strip() else f"<span class='pastille'>{total_lots} lot(s)</span>"}
</form>"""
    nav = ""
    if nb_pages > 1:
        base = f"/?q={quote_plus(q)}&p=" if q.strip() else "/?p="
        nav = ("<p>"
               + (f"<a href='{base}{p - 1}'>← Précédent</a> " if p > 1 else "")
               + f"<span class='pastille'>page {p}/{nb_pages}</span>"
               + (f" <a href='{base}{p + 1}'>Suivant →</a>" if p < nb_pages else "")
               + "</p>")
    liste = (barre_recherche
             + ("<ul class='lots'>" + "".join(h for _, h in visibles) + "</ul>" + nav
                if visibles else "<p>Aucun lot ne correspond.</p>"))

    return page(f"""
<h1>Remise de chèques TLMC <small>CFONB · BRED 320c</small></h1>
<fieldset><legend>Nouveau lot — scans locaux</legend>
  <form method="post" action="/traiter/upload" enctype="multipart/form-data"
        onsubmit="var b=this.querySelector('button');b.innerHTML='<span class=spinner></span> Envoi du fichier… (ne pas fermer)';b.style.pointerEvents='none'">
    <p><input type="file" name="fichiers" multiple accept=".pdf,.jpg,.jpeg,.png,.tif,.tiff">
       <label> ou dossier : <input type="file" name="dossier" webkitdirectory multiple></label>
       <button type="submit">Traiter</button></p>
    <p style="font-size:.85rem">Lire aussi :
      <label><input type="checkbox" name="lire_banque" checked> banque</label>
      <label><input type="checkbox" name="lire_titulaire" checked> titulaire</label>
      <label><input type="checkbox" name="lire_numero" checked> n° de chèque imprimé (contrôle croisé avec la ligne CMC7)</label></p>
  </form>
</fieldset>
<fieldset><legend>Nouveau lot — Google Drive (partage par lien)</legend>
  <form method="post" action="/traiter/drive"
        onsubmit="var b=this.querySelector('button');b.innerHTML='<span class=spinner></span> Téléchargement…';b.style.pointerEvents='none'">
    <input type="text" name="lien" size="55" placeholder="https://drive.google.com/…" required>
    <button type="submit">Traiter</button>
    <p style="font-size:.85rem">Lire aussi :
      <label><input type="checkbox" name="lire_banque" checked> banque</label>
      <label><input type="checkbox" name="lire_titulaire" checked> titulaire</label>
      <label><input type="checkbox" name="lire_numero" checked> n° de chèque imprimé (contrôle croisé avec la ligne CMC7)</label></p>
  </form>
</fieldset>
<h2>Lots</h2>{liste}""")


# ---------------------------------------------------------------------------
# Vue lot : édition chèque par chèque
# ---------------------------------------------------------------------------

def _rendre_lot(nom: str, lot: dict, message: str = "") -> str:
    s = stats_lot(lot)
    lignes = []
    for c in lot["cheques"]:
        if c.get("isole"):
            continue  # les isolés ont leur section dédiée plus bas
        cid = c["id"]
        problemes = valider_cheque(c)
        averts = list(dict.fromkeys([*c.get("avertissements", []), *problemes]))
        classe = "isole" if c.get("isole") else ""
        img = (f"<a href='/image/{escape(nom)}/{escape(c['image'])}' target='_blank'>"
               f"<img class='vignette' src='/image/{escape(nom)}/{escape(c['image'])}'></a>"
               if c.get("image") else "")
        lignes.append(f"""
<tr class="{classe}">
  <td>{c.get('page', '')}</td>
  <td>{img}</td>
  <td><input type="text" name="banque_{cid}" value="{escape(str(c.get('banque', '')))}" size="14">
      <input type="text" name="titulaire_{cid}" value="{escape(str(c.get('titulaire', '')))}" size="16"></td>
  <td><input type="text" name="z1_{cid}" value="{escape(str(c['z1']))}" size="8"></td>
  <td><input type="text" name="z2_{cid}" value="{escape(str(c['z2']))}" size="13"></td>
  <td><input type="text" name="z3_{cid}" value="{escape(str(c['z3']))}" size="13"></td>
  <td class="montant"><input type="text" name="montant_{cid}" value="{escape(str(c['montant_eur']))}"></td>
  <td class="warn">{escape(' | '.join(averts))}</td>
  <td style="text-align:center"><input type="checkbox" name="isole_{cid}" {'checked' if c.get('isole') else ''}></td>
</tr>""")

    isoles = [c for c in lot["cheques"] if c.get("isole")]
    isoles_html = ""
    if isoles:
        blocs = []
        for c in isoles:
            cid = c["id"]
            averts = list(dict.fromkeys([*c.get("avertissements", []), *valider_cheque(c)]))
            img = (f"<a href='/image/{escape(nom)}/{escape(c['image'])}' target='_blank'>"
                   f"<img src='/image/{escape(nom)}/{escape(c['image'])}' "
                   f"style='max-width:520px;max-height:240px;border-radius:4px'></a>"
                   if c.get("image") else "")
            blocs.append(f"""
<fieldset>
<legend>p{c.get('page', '?')} — {escape(str(c.get('banque', '') or ''))} {escape(str(c.get('titulaire', '') or ''))}</legend>
{f"<p class='warn'>{escape(' | '.join(averts))}</p>" if averts else ""}
{img}
<form method="post" action="/lot/{escape(nom)}/isole/{cid}/maj">
  <p>
    <input type="text" name="z1" size="9" value="{escape(str(c.get('z1', '')))}">
    <input type="text" name="z2" size="14" value="{escape(str(c.get('z2', '')))}">
    <input type="text" name="z3" size="14" value="{escape(str(c.get('z3', '')))}">
    <input type="text" name="montant" size="8" value="{escape(str(c.get('montant_eur', '')))}">
  </p>
  <p>
    <input type="text" name="banque" size="18" value="{escape(str(c.get('banque', '') or ''))}">
    <input type="text" name="titulaire" size="22" value="{escape(str(c.get('titulaire', '') or ''))}">
    <button type="submit" name="action" value="reintegrer">✅ Corriger et réintégrer au lot</button>
    <button type="submit" name="action" value="garder" class="secondaire">💾 Enregistrer (garder isolé)</button>
  </p>
</form>
</fieldset>""")
        isoles_html = (f"<h3 class='warn'>🧰 Chèques isolés ({len(isoles)}) — corriger "
                       f"depuis l'image puis réintégrer</h3>" + "".join(blocs))

    rejets_html = ""
    if lot.get("rejets"):
        import re as _re
        blocs = []
        for idx, r in enumerate(lot["rejets"]):
            # pré-remplissage : lecture brute stockée, sinon extraite du motif
            pz1, pz2, pz3 = r.get("z1", ""), r.get("z2", ""), r.get("z3", "")
            if not (pz1 or pz2 or pz3):
                m = _re.search(r"([0-9?]{4,8}) ([0-9?]{8,16}) ([0-9?]{8,16})", r.get("motif", ""))
                if m:
                    pz1, pz2, pz3 = m.group(1), m.group(2), m.group(3)
            img_nom = Path(r.get("image", "")).name
            img_html = (f"<a href='/rejet-image/{escape(nom)}/{escape(img_nom)}' target='_blank'>"
                        f"<img src='/rejet-image/{escape(nom)}/{escape(img_nom)}' "
                        f"style='max-width:520px;max-height:240px;border-radius:4px'></a>"
                        if img_nom else "")
            blocs.append(f"""
<fieldset>
<legend>{escape(r['fichier'])} p{r['page']}</legend>
<p class='warn'>{escape(r['motif'])}</p>
{img_html}
<form method="post" action="/lot/{escape(nom)}/rejet/{idx}/integrer">
  <p style="opacity:.65;font-size:.8rem">Champs pré-remplis avec la lecture brute —
     corrige les <strong>?</strong> et les chiffres douteux en t'aidant de l'image.</p>
  <p>
    <input type="text" name="z1" placeholder="n° chèque (7)" size="9" value="{escape(pz1)}">
    <input type="text" name="z2" placeholder="zone interbancaire (12)" size="14" value="{escape(pz2)}">
    <input type="text" name="z3" placeholder="compte (12)" size="14" value="{escape(pz3)}">
    <input type="text" name="montant" placeholder="montant €" size="8" value="{escape(r.get('montant', ''))}">
  </p>
  <p>
    <input type="text" name="banque" placeholder="banque" size="18" value="{escape(r.get('banque', ''))}">
    <input type="text" name="titulaire" placeholder="titulaire" size="22" value="{escape(r.get('titulaire', ''))}">
    <button type="submit">➕ Corriger et intégrer au lot</button>
  </p>
</form>
</fieldset>""")
        rejets_html = ("<h3 class='rejet'>Rejets (hors remise) — corriger depuis l'image "
                       "puis intégrer</h3>" + "".join(blocs))

    ignorees_html = ""
    if lot.get("ignorees"):
        items = "".join(f"<li>{escape(i['fichier'])} p{i['page']} — {escape(i['motif'])}</li>"
                        for i in lot["ignorees"])
        ignorees_html = (f"<details><summary>{len(lot['ignorees'])} page(s) ignorée(s) "
                         f"(coupons, bons de commande…)</summary><ul>{items}</ul></details>")

    tlmc = lot.get("dernier_tlmc")
    tlmc_html = (f"<p class='ok'>Dernier TLMC généré : <a href='/telecharger/{escape(nom)}/"
                 f"{escape(tlmc['fichier'])}'>⬇ {escape(tlmc['fichier'])}</a> — "
                 f"{tlmc['nb_cheques']} chèques, {tlmc['total_centimes'] / 100:.2f} €, "
                 f"remise n°{escape(str(tlmc.get('numero_remise', '')))}</p>" if tlmc else "")

    return page(f"""
<p><a href="/">← Lots</a></p>
<h1>Lot {escape(nom)} <small>{escape(lot.get('source', ''))}</small></h1>
{f"<p class='ok'>{escape(message)}</p>" if message else ""}
<p><span class='pastille'>{s['nb_actifs']} actif(s)</span>
   <span class='pastille'>{s['nb_isoles']} isolé(s)</span>
   <span class='pastille'>total <strong>{s['total_centimes'] / 100:.2f} €</strong></span>
   {f"<span class='pastille rejet'>{s['nb_invalides']} montant(s) invalide(s)</span>" if s['nb_invalides'] else ""}</p>
<form method="post" action="/lot/{escape(nom)}/maj">
<table>
<tr><th>p.</th><th>chèque</th><th>banque / titulaire</th><th>CMC7 n°</th>
    <th>zone interbancaire</th><th>compte</th><th>€</th><th>avertissements</th><th>isoler</th></tr>
{''.join(lignes)}
</table>
<p><button type="submit">💾 Enregistrer les modifications</button></p>
</form>
<form method="post" action="/lot/{escape(nom)}/autocorriger" style="margin-top:.4rem;display:inline-block">
  <button type="submit">🪄 Correction automatique (chèques avec commentaires)</button>
</form>
<form method="post" action="/lot/{escape(nom)}/retraiter" style="margin-top:.4rem;display:inline-block">
  <button type="submit" class="secondaire">🔄 Retraiter le lot (relecture complète — l'état actuel est archivé)</button>
</form>
{isoles_html}
{rejets_html}
{ignorees_html}
<fieldset><legend>Générer le fichier TLMC (chèques non isolés)</legend>
<form method="post" action="/lot/{escape(nom)}/tlmc">
  <label>N° de remise : <input type="text" name="numero_remise" value="" size="8" placeholder="005278"></label>
  <label> Date (AAAA-MM-JJ) : <input type="text" name="date_remise" value="{date.today():%Y-%m-%d}" size="10"></label>
  <button type="submit">Générer</button>
</form>
{tlmc_html}
</fieldset>""")


def _rendre_progression(nom: str, prog: dict) -> str:
    etat = prog.get("etat", "?")
    total = prog.get("total_pages") or 0
    pages = prog.get("pages", 0)
    pct = int(pages / total * 100) if total else 0
    libelles = {"demarrage": "Démarrage…", "telechargement": "Téléchargement depuis Google Drive…",
                "lecture": "Lecture des chèques (CMC7 + montants)…",
                "pause": "⏸ En pause — les lectures en vol se terminent, rien de nouveau ne part.",
                "autocorrection": "🪄 Correction automatique des chèques signalés…"}
    journal = "".join(f"<li>{escape(l)}</li>" for l in reversed(prog.get("journal", [])))
    contenu = f"""
<p><a href="/">← Lots</a></p>
<h1>Lot {escape(nom)} <small>{escape(prog.get('source', ''))}</small></h1>
<div class="progression">
  <p><span class="spinner"></span> <strong>{escape(libelles.get(etat, etat))}</strong></p>
  <div class="barre"><div class="barre-int" style="width:{pct}%"></div></div>
  <p>{pages}/{total or "?"} pages —
     <span class="ok">{prog.get('ok', 0)} chèque(s) lu(s)</span> ·
     <span class="rejet">{prog.get('rejets', 0)} rejet(s)</span> ·
     {prog.get('ignores', 0)} page(s) ignorée(s) (bons de commande)</p>
  <ul class="journal">{journal}</ul>
  <p>{"<form method='post' action='/lot/" + escape(nom) + "/reprendre' style='display:inline'><button type='submit'>▶ Reprendre</button></form>" if etat == "pause" else "<form method='post' action='/lot/" + escape(nom) + "/pause' style='display:inline'><button type='submit' class='secondaire'>⏸ Mettre en pause</button></form>"}</p>
  <p style="opacity:.55;font-size:.8rem">Actualisation automatique — dernière mise à jour {escape(prog.get('maj', ''))}.
     Tu peux fermer cette page : le traitement continue sur le serveur.</p>
</div>"""
    # meta refresh toutes les 2 s tant que ça tourne
    return page(contenu).replace("<title>", "<meta http-equiv='refresh' content='2'><title>", 1)


@app.get("/lot/{nom}", response_class=HTMLResponse)
def voir_lot(nom: str, message: str = ""):
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    lot = charger_lot(session_dir)
    prog = lire_progression(session_dir)
    if lot is not None:
        if prog and prog.get("etat") == "autocorrection" and traitement_actif(session_dir):
            return HTMLResponse(_rendre_progression(nom, prog))
        return _rendre_lot(nom, lot, message)
    if prog:
        if prog.get("etat") == "erreur":
            return HTMLResponse(page(
                f"<p><a href='/'>← Lots</a></p><h1>Lot {escape(nom)}</h1>"
                f"<p class='rejet'>⚠️ Traitement en erreur : {escape(prog.get('message', ''))}</p>"
                f"<form method='post' action='/lot/{escape(nom)}/relancer'>"
                f"<button type='submit'>Relancer le traitement</button></form>"))
        if not traitement_actif(session_dir):
            return HTMLResponse(page(
                f"<p><a href='/'>← Lots</a></p><h1>Lot {escape(nom)}</h1>"
                f"<p class='rejet'>Traitement interrompu (redémarrage du serveur ?).</p>"
                f"<form method='post' action='/lot/{escape(nom)}/relancer'>"
                f"<button type='submit'>Relancer le traitement</button></form>"))
        return HTMLResponse(_rendre_progression(nom, prog))
    if (session_dir / "scans").exists():
        return HTMLResponse(page(
            f"<p><a href='/'>← Lots</a></p><h1>Lot {escape(nom)}</h1>"
            f"<p class='warn'>Scans présents mais jamais traités.</p>"
            f"<form method='post' action='/lot/{escape(nom)}/relancer'>"
            f"<button type='submit'>Lancer le traitement</button></form>"))
    return HTMLResponse(page("<p class='rejet'>Lot introuvable.</p>"), status_code=404)


@app.post("/lot/{nom}/pause")
async def pause_lot(nom: str):
    nom = Path(nom).name
    (SESSIONS / nom / "pause.flag").touch()
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.post("/lot/{nom}/reprendre")
async def reprendre_lot(nom: str):
    nom = Path(nom).name
    (SESSIONS / nom / "pause.flag").unlink(missing_ok=True)
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.post("/lot/{nom}/autocorriger")
async def autocorriger_lot(nom: str):
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    if charger_lot(session_dir) is None or traitement_actif(session_dir):
        return RedirectResponse(f"/lot/{nom}", status_code=303)
    threading.Thread(target=_autocorriger_worker, args=(session_dir,), daemon=True).start()
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.post("/lot/{nom}/retraiter")
async def retraiter_lot(nom: str):
    """Relecture complète d'un lot déjà traité (l'état actuel est archivé)."""
    import os
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    if not (session_dir / "scans").exists() or traitement_actif(session_dir):
        return RedirectResponse(f"/lot/{nom}", status_code=303)
    if (session_dir / "session.json").exists():
        os.replace(session_dir / "session.json", session_dir / "session.json.bak")
    demarrer_traitement(session_dir, "retraitement")
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.post("/lot/{nom}/relancer")
async def relancer_lot(nom: str):
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    if not (session_dir / "scans").exists() or charger_lot(session_dir) is not None:
        return RedirectResponse(f"/lot/{nom}", status_code=303)
    if not traitement_actif(session_dir):
        demarrer_traitement(session_dir, "relance manuelle")
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.post("/lot/{nom}/maj")
async def maj_lot(nom: str, request: Request):
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    lot = charger_lot(session_dir)
    if lot is None:
        return HTMLResponse(page("<p class='rejet'>Lot introuvable.</p>"), status_code=404)
    form = await request.form()
    for c in lot["cheques"]:
        cid = c["id"]
        if f"z1_{cid}" not in form:
            continue  # chèque non présent dans ce formulaire (ex : isolé)
        for champ, cle in (("banque", f"banque_{cid}"), ("titulaire", f"titulaire_{cid}"),
                           ("z1", f"z1_{cid}"), ("z2", f"z2_{cid}"), ("z3", f"z3_{cid}"),
                           ("montant_eur", f"montant_{cid}")):
            if cle in form:
                c[champ] = str(form[cle]).strip()
        c["isole"] = f"isole_{cid}" in form
    sauver_lot(session_dir, lot)
    return RedirectResponse(f"/lot/{nom}?message=Modifications+enregistrées", status_code=303)


@app.post("/lot/{nom}/tlmc")
async def tlmc_lot(nom: str, numero_remise: str = Form(""), date_remise: str = Form("")):
    nom = Path(nom).name
    try:
        dr = date.fromisoformat(date_remise) if date_remise else None
        generer_tlmc(SESSIONS / nom, SPEC,
                     numero_remise=numero_remise.strip() or None, date_remise=dr)
    except Exception as exc:
        lot = charger_lot(SESSIONS / nom)
        return HTMLResponse(_rendre_lot(nom, lot, f"⚠️ Échec génération : {exc}"))
    return RedirectResponse(f"/lot/{nom}?message=Fichier+TLMC+généré", status_code=303)


# ---------------------------------------------------------------------------
# Traitement de nouveaux lots
# ---------------------------------------------------------------------------

def _nouvelle_session() -> Path:
    # suffixe aléatoire : deux uploads dans la même seconde (2 PC, 2 onglets…)
    # ne peuvent plus entrer en collision
    session_dir = SESSIONS / f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    (session_dir / "scans").mkdir(parents=True, exist_ok=True)
    return session_dir


def _traiter_session(session_dir: Path, source: str,
                     lien_drive: str | None = None,
                     options: dict | None = None) -> RedirectResponse:
    """Lance le traitement en arrière-plan et redirige tout de suite vers la
    page de progression — plus de requête bloquante."""
    demarrer_traitement(session_dir, source, lien_drive, options)
    return RedirectResponse(f"/lot/{session_dir.name}", status_code=303)


@app.post("/traiter/upload")
def traiter_upload(fichiers: list[UploadFile] = File(default=[]),
                         dossier: list[UploadFile] = File(default=[]),
                         lire_banque: str = Form(None), lire_titulaire: str = Form(None),
                         lire_numero: str = Form(None)):
    session_dir = _nouvelle_session()
    noms = []
    for upload in [*fichiers, *dossier]:
        if not upload.filename:
            continue
        nom_fichier = Path(upload.filename).name
        with (session_dir / "scans" / nom_fichier).open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        noms.append(nom_fichier)
    if not noms:
        return HTMLResponse(page("<p class='rejet'>Aucun fichier reçu.</p>"))
    source = ", ".join(noms[:2]) + (f" +{len(noms) - 2} autre(s)" if len(noms) > 2 else "")
    options = {"lire_banque": lire_banque is not None,
               "lire_titulaire": lire_titulaire is not None,
               "lire_numero": lire_numero is not None}
    return _traiter_session(session_dir, source, options=options)


@app.post("/traiter/drive")
async def traiter_drive(lien: str = Form(...),
                        lire_banque: str = Form(None), lire_titulaire: str = Form(None),
                        lire_numero: str = Form(None)):
    session_dir = _nouvelle_session()
    options = {"lire_banque": lire_banque is not None,
               "lire_titulaire": lire_titulaire is not None,
               "lire_numero": lire_numero is not None}
    return _traiter_session(session_dir, "Google Drive", lien_drive=lien, options=options)


# ---------------------------------------------------------------------------
# Fichiers
# ---------------------------------------------------------------------------

@app.post("/lot/{nom}/isole/{cid}/maj")
async def maj_isole(nom: str, cid: int, z1: str = Form(""), z2: str = Form(""),
                    z3: str = Form(""), montant: str = Form(""), banque: str = Form(""),
                    titulaire: str = Form(""), action: str = Form("garder")):
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    lot = charger_lot(session_dir)
    if lot is None:
        return RedirectResponse(f"/lot/{nom}", status_code=303)
    for c in lot["cheques"]:
        if c["id"] == cid:
            c.update({"z1": z1.strip(), "z2": z2.strip(), "z3": z3.strip(),
                      "montant_eur": montant.strip().replace(",", "."),
                      "banque": banque.strip(), "titulaire": titulaire.strip()})
            if action == "reintegrer":
                problemes = valider_cheque(c)
                if problemes:
                    return HTMLResponse(_rendre_lot(
                        nom, lot, f"⚠️ p{c.get('page', '?')} non réintégré — " + " ; ".join(problemes)))
                c["isole"] = False
                c["avertissements"] = [a for a in c.get("avertissements", [])
                                       if not a.startswith("correction manuelle requise")]
                c["avertissements"].append("réintégré après correction manuelle")
            break
    sauver_lot(session_dir, lot)
    suffixe = "réintégré" if action == "reintegrer" else "enregistré"
    return RedirectResponse(f"/lot/{nom}?message=Chèque+{suffixe}", status_code=303)


@app.post("/lot/{nom}/rejet/{idx}/integrer")
async def integrer_rejet(nom: str, idx: int, z1: str = Form(""), z2: str = Form(""),
                         z3: str = Form(""), montant: str = Form(""),
                         banque: str = Form(""), titulaire: str = Form("")):
    from tlmc.cmc7 import CMC7Error, montant_en_centimes, parser_ligne
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    lot = charger_lot(session_dir)
    if lot is None or not (0 <= idx < len(lot.get("rejets", []))):
        return RedirectResponse(f"/lot/{nom}", status_code=303)
    try:
        parser_ligne(f"{z1.strip()} {z2.strip()} {z3.strip()}")
        montant_en_centimes(montant.strip())
    except CMC7Error as exc:
        lot_html = _rendre_lot(nom, lot, f"⚠️ Rejet non intégré — {exc}")
        return HTMLResponse(lot_html)
    rejet = lot["rejets"].pop(idx)
    img_nom = Path(rejet.get("image", "")).name
    if img_nom and (session_dir / "rejets" / img_nom).exists():
        (session_dir / "cheques").mkdir(exist_ok=True)
        shutil.copy(session_dir / "rejets" / img_nom, session_dir / "cheques" / img_nom)
    lot["cheques"].append({
        "id": max((c["id"] for c in lot["cheques"]), default=-1) + 1,
        "fichier": rejet.get("fichier", ""), "page": rejet.get("page", ""),
        "banque": banque.strip(), "titulaire": titulaire.strip(),
        "z1": z1.strip(), "z2": z2.strip(), "z3": z3.strip(),
        "montant_eur": montant.strip().replace(",", "."),
        "methode": "saisie manuelle", "confiance": "manuelle",
        "avertissements": ["intégré manuellement depuis un rejet"],
        "isole": False, "image": img_nom,
    })
    sauver_lot(session_dir, lot)
    return RedirectResponse(f"/lot/{nom}?message=Rejet+intégré+au+lot", status_code=303)


@app.get("/rejet-image/{session}/{nom}")
def image_rejet(session: str, nom: str):
    chemin = (SESSIONS / Path(session).name / "rejets" / Path(nom).name).resolve()
    if not chemin.is_file() or not chemin.is_relative_to(SESSIONS.resolve()):
        return HTMLResponse("introuvable", status_code=404)
    return FileResponse(chemin)


@app.get("/image/{session}/{nom}")
def image_cheque(session: str, nom: str):
    chemin = (SESSIONS / Path(session).name / "cheques" / Path(nom).name).resolve()
    if not chemin.is_file() or not chemin.is_relative_to(SESSIONS.resolve()):
        return HTMLResponse("introuvable", status_code=404)
    return FileResponse(chemin)


@app.get("/telecharger/{session}/{nom}")
def telecharger(session: str, nom: str):
    chemin = (SESSIONS / Path(session).name / "sortie" / Path(nom).name).resolve()
    if not chemin.is_file() or not chemin.is_relative_to(SESSIONS.resolve()):
        return HTMLResponse("introuvable", status_code=404)
    return FileResponse(chemin, filename=chemin.name)


# ---------------------------------------------------------------------------
# API JSON (pilotage par agent : agent_ftp.py) — mêmes règles d'accès que l'UI
# ---------------------------------------------------------------------------
from fastapi.responses import JSONResponse
import re as _re

_ACTIONNABLE = _re.compile(r"n° imprimé|zone [123]|lecture douteuse|CMC7|clé de contrôle|"
                           r"correction manuelle|VÉRIFIER|montant|corriger", _re.I)


def _zones_ok(c: dict) -> bool:
    z1, z2, z3 = str(c.get("z1", "")), str(c.get("z2", "")), str(c.get("z3", ""))
    return (len(z1), len(z2), len(z3)) == (7, 12, 12) and (z1 + z2 + z3).isdigit()


def _problemes_cheque(c: dict) -> list[str]:
    """Tout ce qui empêcherait ce chèque d'entrer dans la remise, ou mérite un œil."""
    if c.get("isole"):
        return []
    problemes = list(valider_cheque(c))
    if not _zones_ok(c):
        problemes.append("zones CMC7 non conformes (7/12/12 attendu)")
    # garde-fou montant : au-delà de 300 € (panier moyen ≈ 50 €), exiger une vérification humaine
    # explicite (corrigé/vérifié manuellement), même si la relecture automatique a « confirmé »
    try:
        if float(str(c.get("montant_eur", "0")).replace(",", ".")) >= 300 and not any(
                a.startswith(("corrigé manuellement", "vérifié sur image")) for a in c.get("avertissements", [])):
            problemes.append("montant ≥ 300 € : vérification humaine requise (chiffres ET lettres)")
    except ValueError:
        pass
    for a in c.get("avertissements", []):
        if a.startswith(("corrigé", "réintégré", "intégré manuellement", "confirmé")) \
                or "✓" in a or "normalisée" in a:
            continue
        if _ACTIONNABLE.search(a):
            problemes.append(a)
    return list(dict.fromkeys(problemes))


def _resume_lot(nom: str, lot: dict | None) -> dict:
    session_dir = SESSIONS / nom
    prog = lire_progression(session_dir)
    actif = traitement_actif(session_dir)
    info = {"session": nom, "progression": prog, "actif": actif, "traite": lot is not None}
    if lot is not None:
        s = stats_lot(lot)
        a_corriger = [c for c in lot["cheques"] if _problemes_cheque(c)]
        info.update({
            "source": lot.get("source", ""), "stats": s,
            "nb_a_corriger": len(a_corriger), "nb_ignorees": len(lot.get("ignorees", [])),
            "dernier_tlmc": lot.get("dernier_tlmc"),
        })
    return info


@app.get("/api/lots")
def api_lots():
    return [_resume_lot(nom, lot) for nom, lot in lister_lots(SESSIONS)]


@app.post("/api/traiter/upload")
def api_traiter_upload(fichiers: list[UploadFile] = File(...),
                             lire_banque: bool = Form(True), lire_titulaire: bool = Form(True),
                             lire_numero: bool = Form(True), nom_session: str = Form("")):
    if nom_session:
        nom_session = _re.sub(r"[^A-Za-z0-9._-]+", "-", nom_session)[:80]
        session_dir = SESSIONS / nom_session
        if session_dir.exists():
            return JSONResponse({"erreur": "session existante", "session": nom_session}, status_code=409)
        (session_dir / "scans").mkdir(parents=True)
    else:
        session_dir = _nouvelle_session()
    noms = []
    for upload in fichiers:
        if not upload.filename:
            continue
        nom_fichier = Path(upload.filename).name
        with (session_dir / "scans" / nom_fichier).open("wb") as f:
            shutil.copyfileobj(upload.file, f)
        noms.append(nom_fichier)
    if not noms:
        return JSONResponse({"erreur": "aucun fichier"}, status_code=400)
    source = ", ".join(noms[:2]) + (f" +{len(noms) - 2} autre(s)" if len(noms) > 2 else "")
    demarrer_traitement(session_dir, source, options={
        "lire_banque": lire_banque, "lire_titulaire": lire_titulaire, "lire_numero": lire_numero})
    return {"session": session_dir.name, "fichiers": noms}


@app.get("/api/lot/{nom}")
def api_lot(nom: str, detail: bool = True):
    nom = Path(nom).name
    lot = charger_lot(SESSIONS / nom)
    info = _resume_lot(nom, lot)
    if lot is None and info["progression"] is None and not (SESSIONS / nom).exists():
        return JSONResponse({"erreur": "lot introuvable"}, status_code=404)
    if lot is not None and detail:
        info["cheques"] = [{**c, "problemes": _problemes_cheque(c),
                            "image_url": f"/image/{nom}/{c['image']}" if c.get("image") else None}
                           for c in lot["cheques"]]
        info["rejets"] = [{**r, "image_url": f"/rejet-image/{nom}/{Path(r.get('image', '')).name}"
                           if r.get("image") else None} for r in lot.get("rejets", [])]
        info["ignorees"] = lot.get("ignorees", [])
    return info


@app.get("/api/lot/{nom}/a_corriger")
def api_a_corriger(nom: str):
    nom = Path(nom).name
    lot = charger_lot(SESSIONS / nom)
    if lot is None:
        return JSONResponse({"erreur": "lot introuvable ou non traité"}, status_code=404)
    return [{**c, "problemes": _problemes_cheque(c),
             "image_url": f"/image/{nom}/{c['image']}" if c.get("image") else None}
            for c in lot["cheques"] if _problemes_cheque(c)]


@app.post("/api/lot/{nom}/autocorriger")
async def api_autocorriger(nom: str):
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    if charger_lot(session_dir) is None:
        return JSONResponse({"erreur": "lot introuvable"}, status_code=404)
    if traitement_actif(session_dir):
        return JSONResponse({"erreur": "traitement déjà en cours"}, status_code=409)
    prog = {"etat": "autocorrection", "pages": 0, "total_pages": 0, "ok": 0, "rejets": 0,
            "ignores": 0, "journal": [], "source": "correction automatique"}
    ecrire_progression(session_dir, prog)
    threading.Thread(target=_autocorriger_worker, args=(session_dir,), daemon=True).start()
    return {"ok": True}


@app.post("/api/lot/{nom}/cheque/{cid}")
async def api_maj_cheque(nom: str, cid: int, request: Request):
    """Met à jour un chèque (z1/z2/z3/montant_eur/banque/titulaire/isole/commentaire)
    et renvoie les problèmes restants. Un chèque corrigé manuellement est tracé."""
    nom = Path(nom).name
    session_dir = SESSIONS / nom
    lot = charger_lot(session_dir)
    if lot is None:
        return JSONResponse({"erreur": "lot introuvable"}, status_code=404)
    corps = await request.json()
    for c in lot["cheques"]:
        if c["id"] != cid:
            continue
        modifs = []
        for champ in ("z1", "z2", "z3", "montant_eur", "banque", "titulaire", "cle"):
            if champ in corps and str(corps[champ]).strip() != str(c.get(champ, "")):
                nv = str(corps[champ]).strip().replace(",", ".") if champ == "montant_eur" \
                    else str(corps[champ]).strip()
                modifs.append(f"{champ} {c.get(champ, '')}→{nv}")
                c[champ] = nv
        if "isole" in corps:
            c["isole"] = bool(corps["isole"])
        if corps.get("commentaire"):
            c["commentaire"] = str(corps["commentaire"])
        if corps.get("acquitter"):
            # vérifié sur image sans modification (ex. n° imprimé illisible) : on retire les alertes
            c["avertissements"] = [a for a in c.get("avertissements", []) if not _ACTIONNABLE.search(a)
                                   or a.startswith(("corrigé", "réintégré", "intégré", "confirmé"))]
            c["avertissements"].append(f"vérifié sur image sans modification par {corps.get('auteur', 'agent')}")
        problemes = valider_cheque(c)
        if modifs and not problemes and _zones_ok(c):
            # les avertissements consommés disparaissent, la trace reste
            c["avertissements"] = [a for a in c.get("avertissements", [])
                                   if not _ACTIONNABLE.search(a)
                                   or a.startswith(("corrigé", "réintégré", "intégré", "confirmé"))]
            c["avertissements"].append("corrigé manuellement (" + " ; ".join(modifs)
                                       + f") par {corps.get('auteur', 'agent')}")
        sauver_lot(session_dir, lot)
        return {"id": cid, "problemes": _problemes_cheque(c), "modifs": modifs,
                "cheque": c}
    return JSONResponse({"erreur": "chèque introuvable"}, status_code=404)


@app.post("/api/lot/{nom}/tlmc")
async def api_tlmc(nom: str, request: Request):
    nom = Path(nom).name
    corps = await request.json() if int(request.headers.get("content-length", "0") or 0) else {}
    try:
        dr = date.fromisoformat(corps["date_remise"]) if corps.get("date_remise") else None
        chemin = generer_tlmc(SESSIONS / nom, SPEC,
                              numero_remise=str(corps.get("numero_remise", "")).strip() or None,
                              date_remise=dr)
    except Exception as exc:
        return JSONResponse({"erreur": str(exc)}, status_code=422)
    lot = charger_lot(SESSIONS / nom)
    return {**lot["dernier_tlmc"], "url": f"/telecharger/{nom}/{chemin.name}"}
