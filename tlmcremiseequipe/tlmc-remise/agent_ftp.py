#!/usr/bin/env python3
"""Agent de remise TLMC depuis le SFTP Atlas For Men.

Pipeline : SFTP (PDF de chèques, lots de 50) → dépôt dans le générateur TLMC
(API JSON de app.py, hébergé sur Fly) → autocorrection → corrections manuelles
(sous-agents vision via `a_corriger` / `corriger`) → génération du fichier TLMC
→ rapport CSV (nom du fichier / montant endossé / lien TLMC / commentaire)
destiné à un Google Sheet.

Variables (.env) : TLMC_URL, TLMC_UTILISATEUR, TLMC_MOT_DE_PASSE,
                   SFTP_HOTE, SFTP_PORT, SFTP_LOGIN, SFTP_MDP, SFTP_DOSSIER

Sous-commandes :
  lister                      arborescence SFTP (fichiers scannables)
  sync [--limite N]           télécharge les nouveaux fichiers (ou --local-dir)
  traiter [--tout|fichiers]   dépose + attend + autocorrige
  etat                        tableau de bord
  a_corriger [--session S]    chèques à corriger (JSON, pour les sous-agents)
  image SESSION IMAGE         télécharge l'image d'un chèque
  corriger SESSION CID ...    met à jour un chèque (--z1 --z2 --z3 --montant --isoler)
  generer [--tout|sessions]   génère les fichiers TLMC (n° de remise JJMM+NN)
  rapport                     écrit sessions/agent_ftp/rapport.csv
  pipeline [--dossier D]      = sync + traiter (sur Fly) + liste des lots à corriger
  finaliser [--date AAAA-MM-JJ] = generer (lots propres) + rapport
  pousser                     (secours) lots lus en local → volume Fly
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from pathlib import Path

import httpx

RACINE = Path(__file__).parent
DOSSIER = RACINE / "sessions" / "agent_ftp"
ENTRANTS = DOSSIER / "entrants"
IMAGES = DOSSIER / "images"
ETAT = DOSSIER / "etat.json"
EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def charger_env():
    fichier = RACINE / ".env"
    if fichier.exists():
        for ligne in fichier.read_text().splitlines():
            if "=" in ligne and not ligne.strip().startswith("#"):
                k, _, v = ligne.partition("=")
                os.environ.setdefault(k.strip(), v.strip())


charger_env()
TLMC_URL = os.environ.get("TLMC_URL", "https://tlmc-remise.fly.dev").rstrip("/")
AUTH = (os.environ.get("TLMC_UTILISATEUR", "atlas"), os.environ.get("TLMC_MOT_DE_PASSE", ""))


def client() -> httpx.Client:
    return httpx.Client(base_url=TLMC_URL, auth=AUTH, timeout=httpx.Timeout(600, connect=30))


# ---------------------------------------------------------------------------
# État
# ---------------------------------------------------------------------------

def charger_etat() -> dict:
    if ETAT.exists():
        return json.loads(ETAT.read_text(encoding="utf-8"))
    return {"fichiers": {}, "prochain_numero": int(os.environ.get("TLMC_PROCHAIN_NUMERO", "5279"))}


def sauver_etat(etat: dict) -> None:
    DOSSIER.mkdir(parents=True, exist_ok=True)
    tmp = ETAT.with_suffix(".tmp")
    tmp.write_text(json.dumps(etat, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, ETAT)


def maj_fichier(nom: str, fiche: dict) -> None:
    """Mise à jour d'une fiche sous verrou : sync/traiter tournent en parallèle."""
    import fcntl
    DOSSIER.mkdir(parents=True, exist_ok=True)
    with (DOSSIER / "etat.lock").open("w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        etat = charger_etat()
        etat["fichiers"][nom] = fiche
        sauver_etat(etat)


def numero_remise_depuis_nom(nom: str, flux: str = "PAIEMENTS") -> str | None:
    """Schéma utilisé par l'équipe : JJMM + 2 chiffres (index du lot de 50, ou
    nombre de chèques pour les lots partiels). Ex : « 20082026FR … 50CH003 » →
    200803 ; « 17082026FR … 20CH » → 170820. Flux BDC (lots FID) : mois + 50 →
    JJ(MM+50)NN (6 chiffres, ex. 035803) pour ne pas entrer en collision avec PAIEMENTS du même jour."""
    m = re.match(r"(\d{7,8})[A-Z]{2}", nom)
    if not m:
        return None
    d = m.group(1).zfill(8)[:4]
    m50 = re.search(r"50CH(\d+)", nom)
    if m50:
        n = d + m50.group(1)[-2:].zfill(2)
    else:
        mn = re.search(r"(\d+)CH", nom)
        if not mn:
            return None
        n = d + mn.group(1)[-2:].zfill(2)
        if flux != "BDC" and int(mn.group(1)) <= 9:
            # lots partiels à 1 chiffre : NN + 50 pour ne pas entrer en collision
            # avec « 50CH00N » du même jour (ex. 3CH vs 50CH003 → 240803)
            n = n[:4] + f"{(int(n[4:6]) + 50) % 100:02d}"
    if flux == "BDC":  # champ n° de remise = 6 chiffres : mois + 50 pour les lots FID (0308 → 0358),
        # mois + 60 pour les lots REC du dossier BDC (même jour, mêmes index que les FID) ;
        # lots partiels « NNCH » : NN + 50 pour ne pas entrer en collision avec « 50CHn » (5CH vs 50CH5)
        decalage = 60 if re.search(r"\bREC\b", nom) else 50
        if not m50:
            n = n[:4] + f"{(int(n[4:6]) + 50) % 100:02d}"
        n = n[:2] + f"{int(n[2:4]) + decalage:02d}" + n[4:]
    return n


def marquer_deja_traites(etat: dict, c: httpx.Client) -> int:
    """Fichiers déjà déposés par l'équipe dans l'outil (même nom) : on garde le
    lot existant au lieu de relire 50 chèques."""
    n = 0
    lots = c.get("/api/lots").json()
    for nom, f in etat["fichiers"].items():
        if f.get("session"):
            continue
        for l in lots:
            if l.get("source", "") == nom and l.get("traite"):
                f.update({"session": l["session"], "statut": "deja_traite",
                          "autocorrige": True, "stats": l.get("stats"),
                          "nb_a_corriger": l.get("nb_a_corriger", 0)})
                if l.get("dernier_tlmc"):
                    f["numero_remise"] = l["dernier_tlmc"].get("numero_remise") or ""
                    f["tlmc"] = {**l["dernier_tlmc"],
                                 "url": f"{TLMC_URL}/telecharger/{l['session']}/{l['dernier_tlmc']['fichier']}"}
                maj_fichier(nom, f)
                n += 1
                break
    return n


FILTRE_CHEQUES = r"\d+CH\d*\b"   # « 50CH4 », « 19CH », « 50CH12 BDC » ; exclut « 1BDC » (bons seuls) et « 7CB » (cartes)


def date_lot(chemin: str) -> str | None:
    """AAAAMMJJ d'un lot : préfixe JJMMAAAA du nom de fichier, sinon sous-dossier JJMM + année du dossier mois."""
    nom = Path(chemin).name
    m = re.match(r"(\d{7,8})[A-Z]{2}", nom)
    if m:
        d = m.group(1).zfill(8)
        return d[4:8] + d[2:4] + d[0:2]
    m = re.search(r"/(\d{2})(\d{2})/[^/]*$", chemin)
    a = re.search(r"/\d{2} [A-ZÉÛ]+ (\d{4})/", chemin.upper())
    if m and a:
        return a.group(1) + m.group(2) + m.group(1)
    return None


def flux_de(chemin: str) -> str:
    """BDC (lots FID : chèques + bons de commande) ou PAIEMENTS, d'après le dossier SFTP."""
    return "BDC" if "/BDC/" in chemin.upper() else "PAIEMENTS"


def slug(nom: str, flux: str = "PAIEMENTS") -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(nom).stem).strip("-")[:60]
    return f"ftp-bdc-{base}" if flux == "BDC" else f"ftp-{base}"


# ---------------------------------------------------------------------------
# SFTP
# ---------------------------------------------------------------------------

def sftp_client():
    import paramiko
    hote = os.environ["SFTP_HOTE"]
    port = int(os.environ.get("SFTP_PORT", "22"))
    t = paramiko.Transport((hote, port))
    t.banner_timeout = 30
    t.connect(username=os.environ["SFTP_LOGIN"], password=os.environ["SFTP_MDP"])
    return t, paramiko.SFTPClient.from_transport(t)


def sftp_lister(sftp, dossier: str, profondeur: int = 4) -> list[dict]:
    trouves = []

    def walk(p, d):
        for e in sftp.listdir_attr(p):
            full = p.rstrip("/") + "/" + e.filename
            if stat.S_ISDIR(e.st_mode):
                if d < profondeur:
                    walk(full, d + 1)
            elif Path(e.filename).suffix.lower() in EXTENSIONS:
                trouves.append({"chemin": full, "nom": e.filename, "taille": e.st_size,
                                "modifie": e.st_mtime})
    walk(dossier, 0)
    return sorted(trouves, key=lambda f: f["chemin"])


def cmd_lister(args):
    t, sftp = sftp_client()
    try:
        for f in sftp_lister(sftp, args.dossier or os.environ.get("SFTP_DOSSIER", "/")):
            print(f"{f['taille']:>10}  {time.strftime('%Y-%m-%d', time.localtime(f['modifie']))}  {f['chemin']}")
    finally:
        t.close()


def cmd_sync(args):
    etat = charger_etat()
    ENTRANTS.mkdir(parents=True, exist_ok=True)
    nouveaux = []
    if args.local_dir:
        src = Path(args.local_dir)
        for p in sorted(src.rglob("*")):
            if p.suffix.lower() in EXTENSIONS and p.is_file():
                if args.filtre and not re.search(args.filtre, p.name, re.I):
                    continue
                nouveaux.append({"chemin": str(p), "nom": p.name, "taille": p.stat().st_size,
                                 "modifie": p.stat().st_mtime})
        copier = lambda f, dest: dest.write_bytes(Path(f["chemin"]).read_bytes())
        fermer = lambda: None
    else:
        t, sftp = sftp_client()
        nouveaux = sftp_lister(sftp, args.dossier or os.environ.get("SFTP_DOSSIER", "/"))
        filtre = args.filtre or FILTRE_CHEQUES
        nouveaux = [f for f in nouveaux if re.search(filtre, f["nom"], re.I)]
        if getattr(args, "depuis", None):
            d = args.depuis.zfill(8); borne = d[4:8] + d[2:4] + d[0:2]
            avant = len(nouveaux)
            nouveaux = [f for f in nouveaux if (date_lot(f["chemin"]) or "99999999") >= borne]
            print(f"(filtre --depuis {args.depuis} : {avant - len(nouveaux)} lot(s) antérieur(s) ignoré(s))")
        copier = lambda f, dest: sftp.get(f["chemin"], str(dest))
        fermer = t.close
    # lots déjà déposés dans l'outil par l'équipe : pas de téléchargement
    deja_dans_outil = {}
    try:
        with client() as c:
            for l in c.get("/api/lots").json():
                if l.get("traite") and l.get("source"):
                    deja_dans_outil.setdefault(l["source"], l)
    except Exception as exc:
        print(f"(outil injoignable pour la déduplication : {exc})")
    try:
        n = 0
        a_telecharger = []
        for f in nouveaux:
            cle = f["nom"]
            deja = etat["fichiers"].get(cle)
            if deja and deja.get("taille") == f["taille"] and (ENTRANTS / cle).exists():
                continue
            if deja and deja.get("statut") == "deja_traite":
                continue
            if cle in deja_dans_outil and not args.local_dir:
                l = deja_dans_outil[cle]
                fiche = {"chemin_source": f["chemin"], "taille": f["taille"], "local": "",
                         "flux": flux_de(f["chemin"]),
                         "statut": "deja_traite", "session": l["session"], "autocorrige": True,
                         "stats": l.get("stats"), "nb_a_corriger": l.get("nb_a_corriger", 0)}
                if l.get("dernier_tlmc"):
                    fiche["numero_remise"] = l["dernier_tlmc"].get("numero_remise") or ""
                    fiche["tlmc"] = {**l["dernier_tlmc"],
                                     "url": f"{TLMC_URL}/telecharger/{l['session']}/{l['dernier_tlmc']['fichier']}"}
                maj_fichier(cle, fiche)
                print(f"= {cle} : déjà traité dans l'outil ({l['session']})")
                continue
            if args.limite and len(a_telecharger) >= args.limite:
                break
            a_telecharger.append(f)

        def telecharger(f, sftp_local=None):
            cle = f["nom"]
            dest = ENTRANTS / cle
            partiel = dest.with_name(dest.name + ".part")
            print(f"⬇ {f['chemin']} ({f['taille']} o)", flush=True)
            if args.local_dir:
                copier(f, partiel)
            else:
                # une connexion SFTP par thread ; VPN instable → 5 tentatives espacées
                for essai in range(5):
                    try:
                        tl, sl = sftp_client()
                        try:
                            sl.get(f["chemin"], str(partiel))
                        finally:
                            tl.close()
                        break
                    except Exception as exc:
                        if essai == 4:
                            print(f"✗ téléchargement abandonné {cle} : {exc}", flush=True)
                            return None
                        print(f"  ↻ {cle}: {exc.__class__.__name__}, nouvel essai dans {30 * (essai + 1)} s", flush=True)
                        time.sleep(30 * (essai + 1))
            os.replace(partiel, dest)  # jamais de fichier partiel visible par `traiter`
            maj_fichier(cle, {"chemin_source": f["chemin"], "taille": f["taille"],
                              "local": str(dest), "statut": "telecharge", "flux": flux_de(f["chemin"])})
            return cle

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=1 if args.local_dir else 4) as ex:
            for ok in ex.map(telecharger, a_telecharger):
                n += 1 if ok else 0
        etat = charger_etat()
        print(f"{n} fichier(s) téléchargé(s), {len(etat['fichiers'])} connu(s).")
    finally:
        fermer()


# ---------------------------------------------------------------------------
# Traitement via l'API du générateur
# ---------------------------------------------------------------------------

def attendre(c: httpx.Client, session: str, etats_fin=("termine", "erreur"), delai=5, max_s=3600):
    debut = time.time()
    dernier = None
    relances = 0
    pannes = 0
    while time.time() - debut < max_s:
        try:
            r = c.get(f"/api/lot/{session}", params={"detail": "false"})
            r.raise_for_status()
            info = r.json()
            pannes = 0
        except (httpx.HTTPError, ValueError) as exc:
            pannes += 1
            if pannes >= 12:
                raise
            print(f"  ~ {session}: {exc.__class__.__name__} pendant l'attente, nouvel essai", flush=True)
            time.sleep(15)
            continue
        prog = info.get("progression") or {}
        etat_p = prog.get("etat")
        if etat_p in etats_fin or (info.get("traite") and not info.get("actif")):
            return info
        if prog and not info.get("actif") and not info.get("traite"):
            # traitement interrompu (redémarrage du serveur) : on relance
            if relances >= 3:
                raise RuntimeError(f"{session} : traitement interrompu, 3 relances sans succès")
            relances += 1
            print(f"  ↻ {session}: traitement interrompu → relance", flush=True)
            c.post(f"/lot/{session}/relancer")
            time.sleep(10)
            continue
        msg = f"{etat_p} {prog.get('pages', 0)}/{prog.get('total_pages', '?')}"
        if msg != dernier:
            print(f"  … {session}: {msg}", flush=True)
            dernier = msg
        time.sleep(delai)
    raise TimeoutError(session)


def traiter_fichier(c: httpx.Client, etat: dict, nom: str) -> dict:
    fiche = etat["fichiers"][nom]
    session = fiche.get("session") or slug(nom, fiche.get("flux", "PAIEMENTS"))
    info = c.get(f"/api/lot/{session}", params={"detail": "false"}).json()
    if info.get("erreur") or (not info.get("traite") and not info.get("actif")):
        print(f"⬆ {nom} → session {session}")
        r = None
        for essai in range(2):  # gros fichiers : le proxy Fly coupe les uploads HTTP trop longs
            try:
                with open(fiche["local"], "rb") as f:
                    r = c.post("/api/traiter/upload",
                               files=[("fichiers", (nom, f, "application/octet-stream"))],
                               data={"nom_session": session})
                break
            except (httpx.TransportError, httpx.ReadError) as exc:
                print(f"  ↻ {nom}: upload HTTP interrompu ({exc.__class__.__name__})", flush=True)
                time.sleep(5)
        def depot_sftp():
            # repli : dépôt du PDF directement sur le volume Fly via fly ssh sftp, puis relance
            import subprocess
            app_fly = os.environ.get("FLY_APP", "tlmc-remise")
            print(f"  ⇢ {nom}: dépôt via fly ssh sftp", flush=True)
            subprocess.run(["fly", "ssh", "console", "-a", app_fly, "-C",
                            f"sh -c \"rm -rf '/app/sessions/{session}' && mkdir -p '/app/sessions/{session}/scans'\""],
                           capture_output=True, timeout=120)
            r1 = subprocess.run(["fly", "ssh", "sftp", "put", fiche["local"],
                                 f"/app/sessions/{session}/scans/{nom}", "-a", app_fly],
                                capture_output=True, text=True, timeout=1800)
            if "uploaded" not in r1.stdout + r1.stderr:
                raise RuntimeError(f"dépôt sftp KO : {(r1.stdout + r1.stderr)[-200:]}")
            rr = c.post(f"/lot/{session}/relancer", follow_redirects=False)
            if rr.status_code not in (200, 303):
                raise RuntimeError(f"relance KO ({rr.status_code})")

        distant = "localhost" not in TLMC_URL and "127.0.0.1" not in TLMC_URL
        if r is None and distant:
            # l'upload HTTP « coupé » a pu aboutir côté serveur : si la session
            # existe et lit déjà, NE PAS redéposer (sinon rm -rf sous les pieds
            # de la lecture en cours → « No such file or directory _pXX.png »)
            time.sleep(10)
            info2 = c.get(f"/api/lot/{session}", params={"detail": "false"}).json()
            if info2.get("actif") or info2.get("traite"):
                print(f"  ✓ {nom}: upload finalement abouti côté serveur, on attend la lecture", flush=True)
            else:
                depot_sftp()
        if r is None:
            pass  # déposé via sftp + relancé
        elif r.status_code == 409 and ("localhost" in TLMC_URL or "127.0.0.1" in TLMC_URL) \
                and not (RACINE / "sessions" / session / "session.json").exists():
            # serveur local : session orpheline (upload interrompu) → on repart de zéro
            import shutil as _sh
            _sh.rmtree(RACINE / "sessions" / session, ignore_errors=True)
            with open(fiche["local"], "rb") as f2:
                r = c.post("/api/traiter/upload",
                           files=[("fichiers", (nom, f2, "application/octet-stream"))],
                           data={"nom_session": session})
            r.raise_for_status()
        elif r.status_code == 409 and distant:
            # session orpheline côté Fly (upload HTTP coupé → scan tronqué) : on refait proprement
            depot_sftp()
        else:
            r.raise_for_status()
            session = r.json()["session"]
    fiche["session"] = session
    fiche["statut"] = "en_cours"
    maj_fichier(nom, fiche)
    info = attendre(c, session)
    if (info.get("progression") or {}).get("etat") == "erreur" \
            and "No such file or directory" in str((info.get("progression") or {}).get("message", "")):
        # course dépôt/relance : une lecture concurrente a perdu ses PNG ;
        # une relance propre suffit
        print(f"  ↻ {nom}: lecture doublée détectée, relance propre", flush=True)
        c.post(f"/lot/{session}/relancer", follow_redirects=False)
        time.sleep(5)
        info = attendre(c, session)
    if (info.get("progression") or {}).get("etat") == "erreur":
        fiche["statut"] = "erreur"
        fiche["erreur"] = info["progression"].get("message")
        maj_fichier(nom, fiche)
        print(f"✗ {nom} : {fiche['erreur']}")
        return info
    if info.get("nb_a_corriger") and not fiche.get("autocorrige"):
        print(f"🔧 {nom} : {info['nb_a_corriger']} chèque(s) à corriger → autocorrection")
        r = c.post(f"/api/lot/{session}/autocorriger")
        if r.status_code == 200:
            time.sleep(2)
            info = attendre(c, session)
        fiche["autocorrige"] = True
    fiche["statut"] = "traite"
    fiche["lieu"] = "local" if "localhost" in TLMC_URL or "127.0.0.1" in TLMC_URL else "fly"
    fiche["nb_a_corriger"] = info.get("nb_a_corriger", 0)
    fiche["stats"] = info.get("stats")
    maj_fichier(nom, fiche)
    s = info.get("stats", {})
    print(f"✓ {nom} : {s.get('nb_actifs')} chèques, {s.get('total_centimes', 0) / 100:.2f} €, "
          f"{info.get('nb_a_corriger', 0)} à corriger, {s.get('nb_rejets', 0)} rejet(s)")
    return info


def cmd_traiter(args):
    etat = charger_etat()
    noms = args.fichiers or [n for n, f in etat["fichiers"].items()
                             if args.tout or f.get("statut") in ("telecharge", "en_cours", "erreur")]
    if not noms:
        print("rien à traiter")
        return
    from concurrent.futures import ThreadPoolExecutor
    with client() as c:
        if not args.fichiers:
            deja = marquer_deja_traites(etat, c)
            if deja:
                print(f"{deja} fichier(s) déjà traité(s) dans l'outil : lots existants conservés")
            noms = [n for n in noms if etat["fichiers"][n].get("statut") != "deja_traite"]
        def un(nom):
            try:
                for essai in range(4):  # réseau (VPN) instable : DNS/TLS transitoires
                    try:
                        return traiter_fichier(c, etat, nom)
                    except (httpx.TransportError, OSError) as exc:
                        if essai == 3:
                            raise
                        print(f"  ~ {nom}: {exc.__class__.__name__}, nouvel essai dans 30 s", flush=True)
                        time.sleep(30)
            except Exception as exc:
                print(f"✗ {nom} : {exc}")
                fiche = charger_etat()["fichiers"][nom]
                fiche.update({"statut": "erreur", "erreur": str(exc)})
                maj_fichier(nom, fiche)
        with ThreadPoolExecutor(max_workers=args.parallele) as ex:
            list(ex.map(un, noms))


def cmd_pousser(args):
    """Lots lus en local (TLMC_URL=localhost) → volume Fly, pour que les liens
    (lot, images, TLMC) soient ceux de l'outil hébergé. Tous les lots en attente
    partent dans UNE archive (fly ssh sftp a ~30 s de frais fixes par appel)."""
    import io
    import subprocess
    import tarfile
    import cv2
    import numpy as np
    app_fly = os.environ.get("FLY_APP", "tlmc-remise")
    url_fly = os.environ.get("TLMC_URL_FLY", "https://tlmc-remise.fly.dev").rstrip("/")
    etat = charger_etat()
    (DOSSIER / "push").mkdir(parents=True, exist_ok=True)
    cibles = [(n, f) for n, f in etat["fichiers"].items()
              if f.get("statut") in ("traite", "genere") and f.get("lieu") == "local"
              and (args.force or not f.get("pousse"))
              and (not args.sessions or f.get("session") in args.sessions)
              and (RACINE / "sessions" / f["session"] / "session.json").exists()]
    if not cibles:
        return
    tgz = DOSSIER / "push" / "_lot.tgz"
    with tarfile.open(tgz, "w:gz") as t:
        for nom, f in cibles:
            session = f["session"]
            sdir = RACINE / "sessions" / session
            # images PNG (≈800 Ko) → JPEG (≈150 Ko) ; session.json réécrit dans l'archive
            lot = json.loads((sdir / "session.json").read_text(encoding="utf-8"))
            renommages = {}
            for p in sdir.rglob("*"):
                rel = p.relative_to(sdir.parent)
                if rel.parts[1:2] == ("scans",) or p.name.endswith((".tmp", ".bak", ".json")):
                    continue
                if p.suffix.lower() == ".png" and rel.parts[1:2] in (("cheques",), ("rejets",)):
                    img = cv2.imdecode(np.frombuffer(p.read_bytes(), np.uint8), cv2.IMREAD_COLOR)
                    ok, enc = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85]) if img is not None else (False, None)
                    if ok:
                        data = enc.tobytes()
                        ti = tarfile.TarInfo(str(rel.with_suffix(".jpg"))); ti.size = len(data)
                        t.addfile(ti, io.BytesIO(data))
                        renommages[p.name] = p.with_suffix(".jpg").name
                        continue
                t.add(p, arcname=str(rel), recursive=False)
            for ch in lot.get("cheques", []):
                if ch.get("image") in renommages:
                    ch["image"] = renommages[ch["image"]]
            for rj in lot.get("rejets", []):
                n = Path(rj.get("image", "")).name
                if n in renommages:
                    rj["image"] = str(Path(rj["image"]).with_name(renommages[n]))
            data = json.dumps(lot, ensure_ascii=False, indent=1).encode("utf-8")
            ti = tarfile.TarInfo(f"{session}/session.json"); ti.size = len(data)
            t.addfile(ti, io.BytesIO(data))
    sessions = [f["session"] for _, f in cibles]
    print(f"⬆ {len(cibles)} lot(s) → Fly ({tgz.stat().st_size / 1e6:.1f} Mo) : "
          + ", ".join(s[-12:] for s in sessions), flush=True)
    distant = "/app/sessions/_push_lot.tgz"
    subprocess.run(["fly", "ssh", "console", "-a", app_fly, "-C", f"rm -f {distant}"],
                   capture_output=True, timeout=120)
    r1 = subprocess.run(["fly", "ssh", "sftp", "put", str(tgz), distant, "-a", app_fly],
                        capture_output=True, text=True, timeout=1800)
    if "uploaded" not in r1.stdout + r1.stderr:
        print(f"✗ envoi KO — {(r1.stdout + r1.stderr)[-300:]}")
        return
    rms = " ".join(f"'{s}'" for s in sessions)
    cmd = (f"cd /app/sessions && rm -rf {rms} && tar xzf _push_lot.tgz && rm -f _push_lot.tgz "
           f"&& ls */session.json | wc -l")
    r2 = subprocess.run(["fly", "ssh", "console", "-a", app_fly, "-C", f"sh -c \"{cmd}\""],
                        capture_output=True, text=True, timeout=600)
    fly = httpx.Client(base_url=url_fly, auth=AUTH, timeout=httpx.Timeout(300, connect=30))
    for nom, f in cibles:
        try:
            info = fly.get(f"/api/lot/{f['session']}", params={"detail": "false"}).json()
            ok = bool(info.get("traite"))
        except Exception as exc:
            ok, info = False, {"erreur": str(exc)}
        if ok:
            f["pousse"] = True
            f["lieu"] = "fly"
            maj_fichier(nom, f)
            print(f"✓ {nom} visible sur {url_fly}/lot/{f['session']}")
        else:
            print(f"✗ {nom} : lot non visible côté Fly — {str(info)[:200]} / {r2.stdout[-100:]} {r2.stderr[-100:]}")
    tgz.unlink(missing_ok=True)


def cmd_etat(args):
    etat = charger_etat()
    with client() as c:
        for nom, f in etat["fichiers"].items():
            ligne = f"{f.get('statut', '?'):<10} {nom}"
            if f.get("session"):
                info = c.get(f"/api/lot/{f['session']}", params={"detail": "false"}).json()
                s = info.get("stats") or {}
                ligne += (f"  → {f['session']} : {s.get('nb_actifs', '?')} actifs, "
                          f"{s.get('nb_isoles', 0)} isolés, {s.get('nb_rejets', 0)} rejets, "
                          f"{info.get('nb_a_corriger', '?')} à corriger, "
                          f"{s.get('total_centimes', 0) / 100:.2f} €")
                if info.get("dernier_tlmc"):
                    ligne += f"  TLMC={info['dernier_tlmc']['fichier']}"
            print(ligne)


def cmd_a_corriger(args):
    etat = charger_etat()
    sessions = [args.session] if args.session else [f["session"] for f in etat["fichiers"].values()
                                                      if f.get("session")]
    sortie = []
    with client() as c:
        for s in sessions:
            r = c.get(f"/api/lot/{s}/a_corriger")
            if r.status_code != 200:
                continue
            for ch in r.json():
                sortie.append({"session": s, "id": ch["id"], "page": ch.get("page"),
                               "fichier": ch.get("fichier"), "image": ch.get("image"),
                               "image_url": TLMC_URL + ch["image_url"] if ch.get("image_url") else None,
                               "z1": ch.get("z1"), "z2": ch.get("z2"), "z3": ch.get("z3"),
                               "cle": ch.get("cle"), "montant_eur": ch.get("montant_eur"),
                               "banque": ch.get("banque"), "titulaire": ch.get("titulaire"),
                               "problemes": ch["problemes"], "avertissements": ch.get("avertissements", [])})
    print(json.dumps(sortie, ensure_ascii=False, indent=1))


def cmd_rejets(args):
    """Pages rejetées (CMC7 illisible, montant non lu…) : à intégrer manuellement."""
    etat = charger_etat()
    sessions = [args.session] if args.session else [f["session"] for f in etat["fichiers"].values()
                                                      if f.get("session")]
    sortie = []
    with client() as c:
        for s in sessions:
            r = c.get(f"/api/lot/{s}")
            if r.status_code != 200:
                continue
            for idx, rj in enumerate(r.json().get("rejets", [])):
                sortie.append({"session": s, "idx": idx, "page": rj.get("page"), "motif": rj.get("motif"),
                               "image": Path(rj.get("image", "")).name,
                               "image_url": TLMC_URL + rj["image_url"] if rj.get("image_url") else None,
                               "z1": rj.get("z1"), "z2": rj.get("z2"), "z3": rj.get("z3"),
                               "montant": rj.get("montant"), "banque": rj.get("banque"),
                               "titulaire": rj.get("titulaire")})
    print(json.dumps(sortie, ensure_ascii=False, indent=1))


def cmd_integrer(args):
    """Intègre un rejet au lot (index = position dans la liste des rejets, qui se
    décale après chaque intégration : toujours relire `rejets` avant)."""
    with client() as c:
        r = c.post(f"/lot/{args.session}/rejet/{args.idx}/integrer",
                   data={"z1": args.z1 or "", "z2": args.z2 or "", "z3": args.z3 or "",
                         "montant": args.montant or "", "banque": args.banque or "",
                         "titulaire": args.titulaire or ""}, follow_redirects=False)
        if r.status_code == 303:
            print("intégré :", r.headers.get("location"))
        else:
            m = re.search(r"Rejet non intégré[^<]*", r.text)
            print("ÉCHEC :", m.group(0) if m else r.status_code)
            sys.exit(1)


def cmd_image(args):
    IMAGES.mkdir(parents=True, exist_ok=True)
    dest = IMAGES / f"{args.session}__{args.image}"
    with client() as c:
        r = c.get(f"/{'rejet-image' if args.rejet else 'image'}/{args.session}/{args.image}")
        r.raise_for_status()
        dest.write_bytes(r.content)
    if args.zoom_bande:
        # bande CMC7 (bas du chèque) agrandie ×2 : plus lisible pour la vision
        from PIL import Image
        im = Image.open(dest)
        l, h = im.size
        bande = im.crop((0, int(h * 0.72), l, h)).resize((l * 2, int(h * 0.28) * 2))
        dest_b = dest.with_name(dest.stem + "__bande.png")
        bande.save(dest_b)
        print(dest_b)
    print(dest)


def cmd_corriger(args):
    corps = {"auteur": args.auteur}
    for champ in ("z1", "z2", "z3", "banque", "titulaire", "cle"):
        v = getattr(args, champ)
        if v is not None:
            corps[champ] = v
    if args.montant is not None:
        corps["montant_eur"] = args.montant
    if args.isoler:
        corps["isole"] = True
    if args.reintegrer:
        corps["isole"] = False
    if args.commentaire:
        corps["commentaire"] = args.commentaire
    if args.acquitter:
        corps["acquitter"] = True
    with client() as c:
        r = c.post(f"/api/lot/{args.session}/cheque/{args.cid}", json=corps)
        print(json.dumps(r.json(), ensure_ascii=False, indent=1))
        if r.status_code != 200:
            sys.exit(1)


def cmd_generer(args):
    etat = charger_etat()
    with client() as c:
        for nom, f in etat["fichiers"].items():
            if not f.get("session"):
                continue
            if args.sessions and f["session"] not in args.sessions:
                continue
            if f.get("tlmc") and not args.force:
                continue
            numero = f.get("numero_remise") or numero_remise_depuis_nom(nom, f.get("flux", "PAIEMENTS"))
            if not numero:
                numero = f"{etat['prochain_numero']:06d}"
                etat["prochain_numero"] += 1
            if not args.force and f.get("statut") == "deja_traite" and f.get("tlmc"):
                continue  # TLMC déjà généré par l'équipe
                f["numero_remise"] = numero
                maj_fichier(nom, f)  # sous verrou : le pipeline écrit en parallèle
            f["numero_remise"] = numero
            corps = {"numero_remise": numero}
            if args.date:
                corps["date_remise"] = args.date
            r = c.post(f"/api/lot/{f['session']}/tlmc", json=corps)
            if r.status_code == 200:
                f["tlmc"] = r.json()
                f["tlmc"]["url"] = TLMC_URL + f["tlmc"]["url"]
                f["statut"] = "genere"
                print(f"✓ {nom} → {f['tlmc']['fichier']} ({f['tlmc']['total_centimes'] / 100:.2f} €, "
                      f"{f['tlmc']['nb_cheques']} chèques)")
            else:
                f["erreur_tlmc"] = r.json().get("erreur")
                print(f"✗ {nom} : {f['erreur_tlmc']}")
            maj_fichier(nom, f)  # sous verrou : le pipeline écrit en parallèle


def cmd_rapport(args):
    import csv
    etat = charger_etat()
    lignes = []
    with client() as c:
        for nom, f in etat["fichiers"].items():
            if args.flux and f.get("flux", "PAIEMENTS") != args.flux:
                continue
            commentaire = []
            montant = ""
            lien = ""
            lien_lot = ""
            if f.get("session"):
                info = c.get(f"/api/lot/{f['session']}").json()
                s = info.get("stats") or {}
                cheques = info.get("cheques", [])
                isoles = [ch for ch in cheques if ch.get("isole")]
                manuels = [ch for ch in cheques if any(a.startswith("corrigé manuellement")
                                                       for a in ch.get("avertissements", []))]
                autos = [ch for ch in cheques if any(a.startswith("corrigé auto")
                                                     for a in ch.get("avertissements", []))]
                if f.get("statut") == "deja_traite":
                    commentaire.append("Lot déjà déposé dans l'outil par l'équipe le "
                                       f"{(f.get('tlmc') or {}).get('genere_le', '?')} (non relu par l'agent)")
                if f.get("tlmc"):
                    montant = f"{f['tlmc']['total_centimes'] / 100:.2f}"
                    lien = f["tlmc"]["url"]
                    commentaire.append(f"{f['tlmc']['nb_cheques']} chèques endossés, remise n° "
                                       f"{f.get('numero_remise') or f['tlmc'].get('numero_remise') or '?'}")
                else:
                    montant = f"{s.get('total_centimes', 0) / 100:.2f}" if s else ""
                    commentaire.append("TLMC NON GÉNÉRÉ" + (f" : {f.get('erreur_tlmc')}" if f.get("erreur_tlmc") else ""))
                if autos:
                    commentaire.append(f"{len(autos)} corrigé(s) auto")
                if manuels:
                    commentaire.append(f"{len(manuels)} corrigé(s) manuellement (p" +
                                       ", p".join(str(ch.get("page")) for ch in manuels) + ")")
                if isoles:
                    commentaire.append(f"{len(isoles)} ISOLÉ(S) : " + " ; ".join(
                        f"p{ch.get('page')} {ch.get('montant_eur', '?')} € — {ch.get('commentaire') or '; '.join(ch.get('problemes', []))}"
                        for ch in isoles))
                restants = [ch for ch in cheques if not ch.get("isole") and any(
                    not p.startswith("corrigé") for p in ch.get("problemes", []))]
                if restants:
                    commentaire.append(f"{len(restants)} chèque(s) encore à corriger (p"
                                       + ", p".join(str(ch.get("page")) for ch in restants) + ")")
                signales = [ch for ch in cheques if not ch.get("isole") and ch.get("commentaire")]
                if signales:
                    commentaire.append("À SIGNALER : " + " ; ".join(
                        f"p{ch.get('page')} ({ch.get('montant_eur')} €) {ch['commentaire']}" for ch in signales))
                rejets = info.get("rejets", [])
                if rejets:
                    commentaire.append(f"{len(rejets)} CHÈQUE(S) NON REMIS (page rejetée) : " + " ; ".join(
                        f"p{r.get('page')} {('(' + str(r.get('montant')) + ' €) ') if r.get('montant') else ''}— {str(r.get('motif', ''))[:90]}"
                        for r in rejets))
                if info.get("nb_ignorees"):
                    commentaire.append(f"{info['nb_ignorees']} page(s) ignorée(s) (bons de commande)")
                gros = [ch for ch in cheques if not ch.get("isole") and any("VÉRIFIER" in a for a in ch.get("avertissements", []))]
                if gros:
                    commentaire.append("montant > 500 € à vérifier : p" + ", p".join(str(ch.get("page")) for ch in gros))
                lien_lot = f"{TLMC_URL}/lot/{f['session']}"
            else:
                commentaire.append(f"non traité ({f.get('statut')}{': ' + f['erreur'] if f.get('erreur') else ''})")
            lignes.append({"flux": f.get("flux", "PAIEMENTS"), "nom du fichier": nom, "montant endossé (€)": montant,
                           "lien vers le fichier TLMC": lien, "lien vers le lot (tlmc-remise)": lien_lot,
                           "commentaire": " | ".join(commentaire)})
    DOSSIER.mkdir(parents=True, exist_ok=True)
    chemin = DOSSIER / (f"rapport_{args.flux}.csv" if args.flux else "rapport.csv")
    with chemin.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["flux", "nom du fichier", "montant endossé (€)", "lien vers le fichier TLMC",
                                           "lien vers le lot (tlmc-remise)", "commentaire"])
        w.writeheader()
        w.writerows(lignes)
    print(chemin)
    if args.json:
        print(json.dumps(lignes, ensure_ascii=False, indent=1))


def cmd_pipeline(args):
    """Étape 1 industrialisée : sync SFTP (en continu, dossier par dossier) pendant
    que les lots déjà arrivés sont déposés/lus/autocorrigés sur l'outil (Fly).
    Fin : liste des lots à confier aux sous-agents correcteurs."""
    import threading
    from types import SimpleNamespace as NS
    dossiers = args.dossier or [os.environ.get("SFTP_DOSSIER", "/")]
    fin_sync = threading.Event()

    def sync_tout():
        try:
            for d in dossiers:
                print(f"▶ sync {d}", flush=True)
                cmd_sync(NS(dossier=d, local_dir=args.local_dir, limite=args.limite, filtre=args.filtre, depuis=args.depuis))
        except Exception as exc:
            print(f"✗ sync : {exc}", flush=True)
        finally:
            fin_sync.set()

    if not args.sans_sync:
        threading.Thread(target=sync_tout, daemon=True).start()
    else:
        fin_sync.set()
    while True:
        cmd_traiter(NS(fichiers=[], tout=False, parallele=args.parallele))
        if fin_sync.is_set():
            reste = [n for n, f in charger_etat()["fichiers"].items() if f.get("statut") in ("telecharge", "en_cours")]
            if not reste:
                break
        time.sleep(30)
    etat = charger_etat()
    a_faire = []
    with client() as c:
        for nom, f in etat["fichiers"].items():
            if f.get("statut") != "traite" or f.get("tlmc"):
                continue
            info = c.get(f"/api/lot/{f['session']}", params={"detail": "false"}).json()
            s = info.get("stats") or {}
            a_faire.append({"session": f["session"], "fichier": nom, "flux": f.get("flux", "PAIEMENTS"),
                            "a_corriger": info.get("nb_a_corriger", 0), "rejets": s.get("nb_rejets", 0),
                            "total_eur": round(s.get("total_centimes", 0) / 100, 2),
                            "nb_cheques": s.get("nb_actifs", 0)})
    (DOSSIER / "a_corriger_par_lot.json").write_text(json.dumps(a_faire, ensure_ascii=False, indent=1))
    print(f"\nPIPELINE_TERMINE : {len(a_faire)} lot(s) lus, en attente de correction → {DOSSIER / 'a_corriger_par_lot.json'}")
    for l in a_faire:
        alerte = " ⚠ total suspect" if l["nb_cheques"] and l["total_eur"] / max(l["nb_cheques"], 1) > 120 else ""
        print(f"  {l['session']}  {l['a_corriger']} à corriger, {l['rejets']} rejet(s), {l['total_eur']} €{alerte}")


def cmd_finaliser(args):
    """Étape 3 : génération des TLMC de tous les lots corrigés + rapport CSV.
    Refuse un lot où il reste des chèques à corriger (hors traces de correction)."""
    from types import SimpleNamespace as NS
    etat = charger_etat()
    prets, bloques = [], []
    with client() as c:
        for nom, f in etat["fichiers"].items():
            if f.get("statut") != "traite" or f.get("tlmc"):
                continue
            info = c.get(f"/api/lot/{f['session']}").json()
            restants = [ch for ch in info.get("cheques", []) if not ch.get("isole") and any(
                not p.startswith("corrigé") for p in ch.get("problemes", []))]
            if restants and not args.force:
                bloques.append((f["session"], [ch.get("page") for ch in restants]))
            else:
                prets.append(f["session"])
    for s, pages in bloques:
        print(f"⏸ {s} : encore à corriger p{', p'.join(map(str, pages))} (ou --force)")
    if prets:
        cmd_generer(NS(sessions=prets, date=args.date, force=False))
    cmd_rapport(NS(json=False, flux=None))
    cmd_rapport(NS(json=False, flux="BDC"))
    cmd_rapport(NS(json=False, flux="PAIEMENTS"))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = p.add_subparsers(dest="cmd", required=True)
    a = sp.add_parser("lister"); a.add_argument("--dossier"); a.set_defaults(f=cmd_lister)
    a = sp.add_parser("sync"); a.add_argument("--dossier"); a.add_argument("--local-dir"); a.add_argument("--depuis", help="JJMMAAAA")
    a.add_argument("--limite", type=int); a.add_argument("--filtre"); a.set_defaults(f=cmd_sync)
    a = sp.add_parser("traiter"); a.add_argument("fichiers", nargs="*"); a.add_argument("--tout", action="store_true")
    a.add_argument("--parallele", type=int, default=4); a.set_defaults(f=cmd_traiter)
    a = sp.add_parser("etat"); a.set_defaults(f=cmd_etat)
    a = sp.add_parser("pousser"); a.add_argument("sessions", nargs="*"); a.add_argument("--force", action="store_true")
    a.set_defaults(f=cmd_pousser)
    a = sp.add_parser("a_corriger"); a.add_argument("--session"); a.set_defaults(f=cmd_a_corriger)
    a = sp.add_parser("image"); a.add_argument("session"); a.add_argument("image")
    a.add_argument("--zoom-bande", action="store_true"); a.add_argument("--rejet", action="store_true")
    a.set_defaults(f=cmd_image)
    a = sp.add_parser("rejets"); a.add_argument("--session"); a.set_defaults(f=cmd_rejets)
    a = sp.add_parser("integrer"); a.add_argument("session"); a.add_argument("idx", type=int)
    for champ in ("z1", "z2", "z3", "montant", "banque", "titulaire"):
        a.add_argument(f"--{champ}")
    a.set_defaults(f=cmd_integrer)
    a = sp.add_parser("corriger"); a.add_argument("session"); a.add_argument("cid", type=int)
    for champ in ("z1", "z2", "z3", "montant", "banque", "titulaire", "cle", "commentaire"):
        a.add_argument(f"--{champ}")
    a.add_argument("--isoler", action="store_true"); a.add_argument("--reintegrer", action="store_true")
    a.add_argument("--acquitter", action="store_true", help="vérifié sur image, valeurs inchangées : retire les alertes")
    a.add_argument("--auteur", default="agent"); a.set_defaults(f=cmd_corriger)
    a = sp.add_parser("generer"); a.add_argument("sessions", nargs="*"); a.add_argument("--date")
    a.add_argument("--force", action="store_true"); a.set_defaults(f=cmd_generer)
    a = sp.add_parser("rapport"); a.add_argument("--json", action="store_true")
    a.add_argument("--flux", choices=["BDC", "PAIEMENTS"]); a.set_defaults(f=cmd_rapport)
    a = sp.add_parser("pipeline", help="sync + traiter + liste des lots à corriger")
    a.add_argument("--dossier", action="append", help="répétable ; traités dans l'ordre")
    a.add_argument("--local-dir"); a.add_argument("--limite", type=int)
    a.add_argument("--filtre", help=f"regex sur le nom (défaut : lots de chèques {FILTRE_CHEQUES!r})")
    a.add_argument("--depuis", help="ne traiter que les lots datés à partir de JJMMAAAA")
    a.add_argument("--sans-sync", action="store_true")
    a.add_argument("--parallele", type=int, default=8); a.set_defaults(f=cmd_pipeline)
    a = sp.add_parser("finaliser", help="generer (lots sans reste à corriger) + rapport")
    a.add_argument("--date"); a.add_argument("--force", action="store_true"); a.set_defaults(f=cmd_finaliser)
    args = p.parse_args()
    args.f(args)


if __name__ == "__main__":
    main()
