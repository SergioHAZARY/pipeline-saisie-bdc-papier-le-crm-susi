"""susi-bdc — plateforme de saisie des bons de commande papier Atlas For Men dans SUSI.

Un seul fichier applicatif, servi sur le port 8760. Aucune base de données : le
dossier `lots/<LOT>/` est la seule source de vérité et l'état affiché est
recalculé depuis les fichiers par `etat_lot()`.

Réimplémentation d'après la passation du 03/09/2026, adaptée à Windows.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import threading
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

RACINE = Path(__file__).resolve().parent.parent
LOTS = RACINE / "lots"
LOTS.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
# .env
# --------------------------------------------------------------------------- #

def charger_env() -> None:
    """Charge app/.env dans os.environ sans écraser l'existant."""
    env = Path(__file__).resolve().parent / ".env"
    if not env.exists():
        return
    for ligne in env.read_text(encoding="utf-8").splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#") and "=" in ligne:
            cle, _, valeur = ligne.partition("=")
            os.environ.setdefault(cle.strip(), valeur.strip().strip('"'))


charger_env()


# --------------------------------------------------------------------------- #
# Poppler — pdftoppm n'est pas toujours sur le PATH sous Windows
# --------------------------------------------------------------------------- #

def _trouver_poppler() -> str:
    """Chemin de pdftoppm : PATH d'abord, puis emplacements winget/choco connus."""
    if shutil.which("pdftoppm"):
        return shutil.which("pdftoppm")
    if os.environ.get("POPPLER_BIN"):
        cand = Path(os.environ["POPPLER_BIN"]) / "pdftoppm.exe"
        if cand.exists():
            return str(cand)
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.exists():
        for p in base.glob("*Poppler*/**/bin/pdftoppm.exe"):
            return str(p)
    return "pdftoppm"


PDFTOPPM = _trouver_poppler()


# --------------------------------------------------------------------------- #
# Authentification — schéma TLMC : nom:mdp:role, rôles admin|viewer
# --------------------------------------------------------------------------- #

securite = HTTPBasic()


def _charger_utilisateurs() -> dict[str, dict]:
    utilisateurs: dict[str, dict] = {}
    for entree in os.environ.get("SUSI_BDC_UTILISATEURS", "").split(","):
        morceaux = entree.strip().split(":")
        if len(morceaux) == 3 and morceaux[0]:
            utilisateurs[morceaux[0]] = {"mdp": morceaux[1], "role": morceaux[2]}
    if not utilisateurs:
        # Compte admin auto-généré au premier démarrage, affiché une seule fois.
        mdp = secrets.token_urlsafe(12)
        utilisateurs["admin"] = {"mdp": mdp, "role": "admin"}
        print("=" * 68, flush=True)
        print("  Aucun compte configuré dans app/.env — compte admin généré :", flush=True)
        print(f"      identifiant : admin", flush=True)
        print(f"      mot de passe : {mdp}", flush=True)
        print("  Notez-le, il ne sera pas réaffiché. Pour le figer, ajoutez dans app/.env :", flush=True)
        print(f"      SUSI_BDC_UTILISATEURS=admin:{mdp}:admin", flush=True)
        print("=" * 68, flush=True)
    return utilisateurs


UTILISATEURS = _charger_utilisateurs()


def compte_courant(request: Request,
                   creds: HTTPBasicCredentials = Depends(securite)) -> dict:
    """Vérifie les identifiants et applique le rôle par méthode HTTP."""
    compte = UTILISATEURS.get(creds.username)
    if not compte or not secrets.compare_digest(creds.password, compte["mdp"]):
        raise HTTPException(status_code=401, detail="Identifiants invalides",
                            headers={"WWW-Authenticate": "Basic"})
    if request.method != "GET" and compte["role"] == "viewer":
        raise HTTPException(status_code=403, detail="Compte en lecture seule")
    return {"nom": creds.username, "role": compte["role"]}


# --------------------------------------------------------------------------- #
# Nom de lot
# --------------------------------------------------------------------------- #

# Grammaire observée en production (SFTP /POUR_OUTSOURCIA/BDC) :
#   JJMMAAAA<PAYS> ATLAS [III] <FID|REC|RECRUT> [<paiement>] <n><unité>[<liasse>]
# Le « III » n'est pas systématique, le type s'écrit REC ou RECRUT, le paiement
# peut être OA / CB / C3M / SANS PAIEMENTS, le compteur « 1 » ou « 001 », et on
# rencontre la coquille « 8DC » pour « 8BDC ».
RX_NOM = re.compile(
    r"^(?P<date>\d{8})"
    r"[ _]?(?P<pays>[A-Z]{2})"
    r"[ _]+ATLAS"
    r"(?:[ _]+(?P<campagne>I{1,3}|IV|V))?"
    r"[ _]+(?P<type>FID|RECRUT|REC)"
    r"(?:[ _]+(?P<paiement>OA|CB|C3M|SANS[ _]PAIEMENTS?))?"
    r"[ _]+(?P<nb>\d+)[ _]?(?P<unite>BDC|CH|DC)"
    r"(?P<liasse>\d+)?"
    r"(?P<reste>.*)$",
    re.IGNORECASE)


def parser_nom(nom: str) -> dict | None:
    """Déduit les métadonnées d'un nom de fichier de lot. None si non conforme."""
    base = re.sub(r"\.pdf$", "", nom.strip(), flags=re.IGNORECASE)
    m = RX_NOM.match(base)
    if not m:
        return None
    g = m.groupdict()
    unite = (g["unite"] or "").upper()
    if unite == "DC":          # coquille observée : « 8DC » pour « 8BDC »
        unite = "BDC"
    type_client = (g["type"] or "").upper()
    if type_client == "REC":
        type_client = "RECRUT"
    paiement = (g["paiement"] or "").upper().replace("_", " ")
    if not paiement:
        # unité CH = chèque joint ; unité BDC sans mention = bons seuls, sans paiement
        paiement = "CH" if unite == "CH" else "OA"
    elif paiement.startswith("SANS"):
        paiement = "OA"
    return {
        "date": g["date"],
        "pays": (g["pays"] or "").upper(),
        "campagne": (g["campagne"] or "").upper(),
        "type": type_client,
        "paiement": paiement,
        "nb_commandes": int(g["nb"]),
        "unite": unite,
        "liasse": g["liasse"] or "",
        "avec_cheque": unite == "CH",
    }


def nom_lot(nom_fichier: str) -> str:
    """Nom de dossier normalisé : espaces → underscores, sans extension."""
    base = re.sub(r"\.pdf$", "", nom_fichier.strip(), flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9]+", "_", base).strip("_").upper()


# --------------------------------------------------------------------------- #
# Score de confiance pré-saisie
# --------------------------------------------------------------------------- #

def score_extraction(cmd: dict) -> tuple[int, list[str]]:
    """Score 0-100 d'une commande extraite, et le détail des retenues.

    Barème (passation du 03/09/2026) : 100 moins
      - arithmétique lignes + frais ≠ total .............. −40
      - montant du chèque ≠ total ........................ −40
      - refco illisible .................................. −25
      - confiance d'article basse / moyenne .......... −20 / −10
      - anomalies ........................ −5 chacune, plafond −20
    """
    score = 100
    motifs: list[str] = []

    articles = cmd.get("articles") or []
    total = cmd.get("total")
    frais = cmd.get("frais_port") or 0

    # arithmétique
    if total is not None and articles:
        somme = 0.0
        chiffrable = True
        for a in articles:
            pu, qte = a.get("prix_unitaire"), a.get("quantite")
            if pu is None or qte is None:
                chiffrable = False
                break
            somme += float(pu) * float(qte)
        if chiffrable and abs(somme + float(frais) - float(total)) > 0.01:
            score -= 40
            motifs.append(f"arithmétique : lignes {somme:.2f} + frais {float(frais):.2f} "
                          f"≠ total {float(total):.2f} (−40)")

    # chèque ≠ total
    cheque = cmd.get("cheque") or {}
    montant_cheque = cheque.get("montant")
    if montant_cheque is not None and total is not None:
        if abs(float(montant_cheque) - float(total)) > 0.01:
            score -= 40
            motifs.append(f"chèque {float(montant_cheque):.2f} ≠ total "
                          f"{float(total):.2f} (−40)")

    # refco illisible
    if any(not a.get("refco") or "?" in str(a.get("refco")) for a in articles):
        score -= 25
        motifs.append("refco illisible (−25)")

    # confiance par article
    for a in articles:
        conf = str(a.get("confiance", "")).lower()
        if conf == "basse":
            score -= 20
            motifs.append(f"article {a.get('refco', '?')} : confiance basse (−20)")
        elif conf == "moyenne":
            score -= 10
            motifs.append(f"article {a.get('refco', '?')} : confiance moyenne (−10)")

    # anomalies, plafonnées
    anomalies = cmd.get("anomalies") or []
    if anomalies:
        retenue = min(5 * len(anomalies), 20)
        score -= retenue
        motifs.append(f"{len(anomalies)} anomalie(s) (−{retenue}, plafond −20)")

    return max(0, min(100, score)), motifs


def classe_score(score: int) -> str:
    return "ok" if score >= 90 else ("tiede" if score >= 70 else "rouge")


# --------------------------------------------------------------------------- #
# Détection déterministe des doublons de scan
# --------------------------------------------------------------------------- #

def _cmc7_normalise(cmd: dict) -> str:
    """Chiffres de la ligne CMC7, séparateurs et glyphes ⑈⑆⑉ retirés."""
    brut = ((cmd.get("cheque") or {}).get("cmc7") or "")
    return re.sub(r"\D", "", str(brut))


def _client_normalise(cmd: dict) -> str:
    """N° client sans zéros de tête : « 0010060274 » et « 10060274 » sont le même."""
    num = ((cmd.get("client") or {}).get("numero") or "")
    return re.sub(r"\D", "", str(num)).lstrip("0")


def detecter_doublons(cmds: list[dict]) -> dict[str, list[str]]:
    """Compare les commandes d'un lot deux à deux et renvoie, par id, les motifs
    de doublon trouvés.

    Un même chèque scanné deux fois (double passage du scanner) produit deux
    commandes identiques : si elles étaient toutes deux saisies, cela ferait un
    encaissement en trop. C'est le défaut le plus coûteux du pipeline.

    La ligne CMC7 est la preuve forte — elle est unique par chèque. Le n° client
    seul ne prouve rien (un client peut légitimement passer deux commandes), donc
    il n'est signalé que comme suspicion, à la différence du CMC7.
    """
    par_cmc7: dict[str, list[str]] = {}
    par_client: dict[str, list[str]] = {}
    for c in cmds:
        cid = str(c.get("_id") or c.get("id") or "?")
        cmc7 = _cmc7_normalise(c)
        if len(cmc7) >= 20:          # une CMC7 plausible fait ~31 chiffres
            par_cmc7.setdefault(cmc7, []).append(cid)
        cli = _client_normalise(c)
        if cli:
            par_client.setdefault(cli, []).append(cid)

    motifs: dict[str, list[str]] = {}
    for cmc7, ids in par_cmc7.items():
        if len(ids) > 1:
            for cid in ids:
                autres = [i for i in ids if i != cid]
                motifs.setdefault(cid, []).append(
                    f"DOUBLON DE SCAN : même ligne CMC7 (…{cmc7[-8:]}) que "
                    f"{', '.join('#' + a for a in autres)} — un seul chèque, "
                    f"une seule commande à saisir")
    for cli, ids in par_client.items():
        if len(ids) > 1:
            for cid in ids:
                autres = [i for i in ids if i != cid]
                # ne pas répéter l'alerte si la CMC7 l'a déjà établie
                if any("CMC7" in m for m in motifs.get(cid, [])):
                    continue
                motifs.setdefault(cid, []).append(
                    f"même n° client que {', '.join('#' + a for a in autres)} — "
                    f"à confirmer : deux commandes distinctes ou un doublon de scan ?")
    return motifs


# --------------------------------------------------------------------------- #
# Lecture du stream-json de `claude -p` en lignes lisibles
# --------------------------------------------------------------------------- #

def _lignes_lisibles(brut: str) -> list[str]:
    """Traduit un flux `--output-format stream-json` en lignes affichables.

    En mode `text`, rien ne sort avant la fin du job : on lit donc du
    stream-json, ligne à ligne, et on n'affiche que ce qui informe.
    """
    sorties: list[str] = []
    for ligne in brut.splitlines():
        ligne = ligne.strip()
        if not ligne:
            continue
        if not ligne.startswith("{"):
            sorties.append(ligne)
            continue
        try:
            evt = json.loads(ligne)
        except json.JSONDecodeError:
            sorties.append(ligne)
            continue

        t = evt.get("type")
        if t == "system" and evt.get("subtype") == "init":
            outils = evt.get("tools") or []
            sorties.append(f"· session {evt.get('session_id', '?')[:8]} ouverte "
                           f"({len(outils)} outils)")
        elif t == "assistant":
            for bloc in (evt.get("message", {}) or {}).get("content", []) or []:
                if bloc.get("type") == "text":
                    txt = (bloc.get("text") or "").strip()
                    if txt:
                        for l in txt.splitlines():
                            if l.strip():
                                sorties.append(f"  {l.strip()}")
                elif bloc.get("type") == "tool_use":
                    nom = bloc.get("name", "?")
                    entree = bloc.get("input") or {}
                    if nom == "Task":
                        sorties.append(f"→ sous-agent {entree.get('subagent_type', '?')} : "
                                       f"{str(entree.get('description', ''))[:70]}")
                    elif nom == "Bash":
                        sorties.append(f"→ bash : {str(entree.get('command', ''))[:90]}")
                    elif nom in ("Write", "Edit"):
                        sorties.append(f"→ {nom.lower()} {entree.get('file_path', '?')}")
                    else:
                        sorties.append(f"→ {nom}")
        elif t == "user":
            for bloc in (evt.get("message", {}) or {}).get("content", []) or []:
                if bloc.get("type") == "tool_result" and bloc.get("is_error"):
                    sorties.append("  ! erreur d'outil")
        elif t == "result":
            duree = evt.get("duration_ms")
            cout = evt.get("total_cost_usd")
            fin = f"■ terminé ({evt.get('subtype', '?')})"
            if duree:
                fin += f" en {duree / 1000:.0f} s"
            if cout:
                fin += f" — {cout:.4f} $"
            sorties.append(fin)
    return sorties


# --------------------------------------------------------------------------- #
# État d'un lot, déduit des fichiers
# --------------------------------------------------------------------------- #

PHASES = ["cree", "extraction", "revue", "validation", "saisie", "verification", "termine"]


def etat_lot(dossier: Path) -> dict:
    """Recalcule l'état complet d'un lot depuis le disque. Pas de base."""
    meta = {}
    if (dossier / "meta.json").exists():
        meta = json.loads((dossier / "meta.json").read_text(encoding="utf-8"))

    pages = sorted((dossier / "pages").glob("p-*.jpg")) if (dossier / "pages").exists() else []
    extraits = sorted((dossier / "extraits").glob("cmd_*.json")) if (dossier / "extraits").exists() else []
    a_lot = (dossier / "lot.json").exists()
    a_validation = (dossier / "validation.json").exists()
    a_saisie = (dossier / "saisie.log.json").exists()
    a_verif = (dossier / "verification.json").exists()

    jobs = {}
    dj = dossier / "jobs"
    if dj.exists():
        for st in dj.glob("*.status.json"):
            try:
                jobs[st.name.split(".")[0]] = json.loads(st.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass

    if a_verif:
        phase = "termine"
    elif a_saisie:
        phase = "verification"
    elif a_validation:
        phase = "saisie"
    elif a_lot:
        phase = "validation"
    elif extraits:
        phase = "revue"
    elif pages:
        phase = "extraction"
    else:
        phase = "cree"

    en_cours = next((n for n, j in jobs.items() if j.get("statut") == "en_cours"), None)

    return {
        "nom": dossier.name,
        "meta": meta,
        "nb_pages": len(pages),
        "nb_extraits": len(extraits),
        "nb_attendu": meta.get("nb_commandes", 0),
        "phase": phase,
        "jobs": jobs,
        "job_en_cours": en_cours,
        "a_validation": a_validation,
        "a_saisie": a_saisie,
        "a_verif": a_verif,
    }


def charger_commandes(dossier: Path) -> list[dict]:
    """Les extraits, enrichis de leur score et de leur bloc de revue."""
    cmds = []
    rep = dossier / "extraits"
    if not rep.exists():
        return cmds
    for f in sorted(rep.glob("cmd_*.json")):
        try:
            cmd = json.loads(f.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        cmd["_fichier"] = f.name
        cmd["_id"] = f.stem.replace("cmd_", "")
        score, motifs = score_extraction(cmd)
        cmd["_score"] = score
        cmd["_motifs"] = motifs
        cmd["_classe"] = classe_score(score)
        cmd.setdefault("revue", {"statut": "a_verifier", "verifie": False,
                                 "par": "", "commentaire": ""})
        cmds.append(cmd)

    # Contrôle déterministe, à l'échelle du lot : score_extraction() ne voit
    # qu'une commande à la fois et ne peut donc pas repérer un doublon.
    doublons = detecter_doublons(cmds)
    for c in cmds:
        c["_doublons"] = doublons.get(c["_id"], [])
        if c["_doublons"]:
            c["_motifs"] = c["_doublons"] + c["_motifs"]
            # un doublon de scan est bloquant, pas une simple retenue de score
            if any("DOUBLON DE SCAN" in m for m in c["_doublons"]):
                c["_classe"] = "rouge"
    return cmds


# --------------------------------------------------------------------------- #
# Job runner — `claude -p`, un seul job à la fois
# --------------------------------------------------------------------------- #

_VERROU = threading.Lock()
_JOB_ACTIF: str | None = None
_PROC_ACTIF: subprocess.Popen | None = None   # pour distinguer un job vivant d'un verrou orphelin


def _job_vraiment_vivant() -> bool:
    """Le verrou protège-t-il un job réel, ou a-t-il fui ?

    Le verrou vit en mémoire. Si le processus `claude -p` meurt sans que le
    thread ait pu relâcher (machine mise en veille, processus tué), le verrou
    reste pris et **plus aucun job ne peut démarrer** — y compris la relance que
    le runbook préconise comme réparation. On vérifie donc l'état réel du
    sous-processus plutôt que de faire confiance au verrou seul.
    """
    return _PROC_ACTIF is not None and _PROC_ACTIF.poll() is None


def liberer_verrou_orphelin() -> str | None:
    """Relâche le verrou si le job qu'il protège n'existe plus. Renvoie le nom du
    job libéré, ou None s'il n'y avait rien à libérer."""
    global _JOB_ACTIF, _PROC_ACTIF
    if _JOB_ACTIF is None or _job_vraiment_vivant():
        return None
    orphelin = _JOB_ACTIF
    _JOB_ACTIF = None
    _PROC_ACTIF = None
    try:
        _VERROU.release()
    except RuntimeError:
        pass          # déjà relâché : rien à faire
    return orphelin


def reconcilier_statuts_au_demarrage() -> list[str]:
    """Un job ne survit pas à un redémarrage du serveur : tout `en_cours` trouvé
    au démarrage est forcément un orphelin. On le marque `interrompu` plutôt que
    de laisser l'UI afficher indéfiniment un job qui ne tourne plus."""
    repares = []
    for statut in LOTS.glob("*/jobs/*.status.json"):
        try:
            data = json.loads(statut.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if data.get("statut") == "en_cours":
            data["statut"] = "interrompu"
            data["note"] = ("job perdu au redémarrage du serveur — les fichiers déjà "
                            "écrits sont conservés ; relancer le job pour reprendre")
            data["reconcilie"] = datetime.now().isoformat(timespec="seconds")
            statut.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            repares.append(f"{statut.parts[-3]}/{statut.name.split('.')[0]}")
    return repares

# Les variables héritées d'une session Claude court-circuitent l'auth Trousseau
# et provoquent un 401 « OAuth access token is invalid » même connecté.
RX_ENV_A_PURGER = re.compile(r"CLAUDE|ANTHROPIC|BAGGAGE|AI_AGENT|SENTRY")


def _env_epure() -> dict:
    return {k: v for k, v in os.environ.items() if not RX_ENV_A_PURGER.search(k)}


# Un `claude -p` n'est pas interactif : toute demande d'approbation est refusée en
# silence, et le job se termine malgré tout en code 0 avec extraits/ vide — symptôme
# trompeur. Un .claude/settings.json ne suffit pas : un fichier de settings projet
# nouvellement créé n'est pas honoré par un process qui ne peut pas répondre à la
# demande de confiance. On passe donc les autorisations explicitement en argument.
OUTILS_AUTORISES = [
    "Read", "Write", "Edit", "Glob", "Grep", "Task", "TodoWrite",
    "Bash(pdftoppm:*)", "Bash(pdfinfo:*)", "Bash(pdftotext:*)",
    "Bash(pdfimages:*)", "Bash(ls:*)",
]

PROMPTS = {
    "extraction": (
        "Tu traites le lot de bons de commande Atlas For Men « {lot} » dans {dossier}.\n"
        "Suis la skill susi-saisie-bdc (phases 1 à 3 : lecture, extraction, consolidation).\n"
        "Les pages rendues sont dans pages/ (impaires = chèques, paires = BDC quand meta.avec_cheque).\n"
        "Lance des sous-agents bdc-lecteur (~5 commandes chacun, jusqu'à 10 en parallèle).\n"
        "Chaque sous-agent écrit un extraits/cmd_NN.json par commande.\n"
        "Termine par la consolidation dans lot.json et le tableau de contrôle recap.md.\n"
        "N'écris rien hors de {dossier}."
    ),
    "saisie": (
        "Saisis le lot validé « {lot} » ({dossier}) dans SUSI, module « Saisie commande rapide ».\n"
        "Utilise l'agent susi-saisisseur. UN SEUL exemplaire, strictement séquentiel.\n"
        "validation.json doit être présent — sinon arrête-toi immédiatement.\n"
        "Journalise chaque commande dans saisie.log.json pour permettre la reprise.\n"
        "Ne saisis JAMAIS d'identifiant ni de mot de passe : la session SUSI est déjà ouverte."
    ),
    "verification": (
        "Vérifie la saisie du lot « {lot} » ({dossier}) avec l'agent susi-verificateur.\n"
        "Lecture seule. Compare SUSI (rapport Excel + détail UI) au SCAN D'ORIGINE,\n"
        "jamais aux extraits — sinon une erreur de lecture se confirmerait elle-même.\n"
        "Sors verification.json et review.md triés par score croissant."
    ),
}


def lancer_job(dossier: Path, genre: str) -> tuple[bool, str]:
    """Démarre un job en tâche de fond. Un seul à la fois, verrou global."""
    global _JOB_ACTIF
    if genre not in PROMPTS:
        return False, f"genre de job inconnu : {genre}"
    if genre in ("saisie",) and not (dossier / "validation.json").exists():
        return False, "saisie refusée : validation.json absent (checkpoint humain manquant)"
    if genre == "verification" and not (dossier / "saisie.log.json").exists():
        return False, "vérification refusée : aucune saisie journalisée"

    if not _VERROU.acquire(blocking=False):
        # Avant de refuser, vérifier que le verrou protège un job réel. Sinon il a
        # fui, et le refuser rendrait la relance — la réparation préconisée —
        # impossible.
        orphelin = liberer_verrou_orphelin()
        if orphelin is None:
            return False, (f"un job tourne déjà ({_JOB_ACTIF}) — "
                           "SUSI ne supporte pas le parallélisme")
        print(f"verrou orphelin libere : {orphelin} (processus disparu)", flush=True)
        _VERROU.acquire(blocking=False)
    _JOB_ACTIF = f"{dossier.name}/{genre}"

    dj = dossier / "jobs"
    dj.mkdir(exist_ok=True)
    log = dj / f"{genre}.log"
    statut = dj / f"{genre}.status.json"
    log.write_text("", encoding="utf-8")
    statut.write_text(json.dumps({"statut": "en_cours",
                                  "demarre": datetime.now().isoformat(timespec="seconds")},
                                 ensure_ascii=False), encoding="utf-8")

    # Le harness refuse de créer un sous-dossier hors des répertoires de travail
    # autorisés : les sorties doivent exister avant que l'agent n'écrive dedans.
    (dossier / "extraits").mkdir(exist_ok=True)

    prompt = PROMPTS[genre].format(lot=dossier.name, dossier=dossier)
    cmd = ["claude", "-p", prompt, "--output-format", "stream-json", "--verbose",
           "--allowedTools", ",".join(OUTILS_AUTORISES)]
    if genre == "extraction":
        # aucun MCP nécessaire → démarrage rapide. La forme {} est refusée
        # (« Invalid MCP configuration ») : il faut {"mcpServers": {}}.
        cmd += ["--mcp-config", json.dumps({"mcpServers": {}}), "--strict-mcp-config"]
    # la saisie et la vérification gardent la config par défaut : claude-in-chrome

    def _tourner():
        global _JOB_ACTIF, _PROC_ACTIF
        code = -1
        try:
            with log.open("a", encoding="utf-8") as fl:
                proc = _PROC_ACTIF = subprocess.Popen(
                    cmd,
                    cwd=str(RACINE),
                    stdin=subprocess.DEVNULL,   # sans ça, hérite du stdin d'uvicorn et se fige
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env=_env_epure(),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                )
                for ligne in proc.stdout:
                    fl.write(ligne)
                    fl.flush()
                code = proc.wait()
        except Exception as exc:  # noqa: BLE001
            with log.open("a", encoding="utf-8") as fl:
                fl.write(f"\n!! échec du lancement : {exc}\n")
        finally:
            statut.write_text(json.dumps(
                {"statut": "termine" if code == 0 else "echec",
                 "code": code,
                 "fini": datetime.now().isoformat(timespec="seconds")},
                ensure_ascii=False), encoding="utf-8")
            _JOB_ACTIF = None
            _PROC_ACTIF = None
            try:
                _VERROU.release()
            except RuntimeError:
                pass      # déjà libéré comme orphelin par un lancement concurrent

    threading.Thread(target=_tourner, daemon=True).start()
    return True, f"job {genre} démarré"


# --------------------------------------------------------------------------- #
# Rendu des pages
# --------------------------------------------------------------------------- #

def rendre_pages(pdf: Path, sortie: Path, dpi: int = 150) -> int:
    """Rend le PDF en pages/p-NNN.jpg. Renomme systématiquement : pdftoppm
    adapte le zéro-padding au nombre de pages (p-1.jpg pour un petit PDF), ce
    qui casserait le tri lexicographique."""
    sortie.mkdir(parents=True, exist_ok=True)
    for vieux in sortie.glob("p-*.jpg"):
        vieux.unlink()
    subprocess.run([PDFTOPPM, "-jpeg", "-r", str(dpi), str(pdf), str(sortie / "p")],
                   check=True, stdin=subprocess.DEVNULL,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    produits = sorted(sortie.glob("p-*.jpg"),
                      key=lambda p: int(re.sub(r"\D", "", p.stem) or 0))
    for p in produits:
        num = int(re.sub(r"\D", "", p.stem) or 0)
        cible = sortie / f"p-{num:03d}.jpg"
        if p != cible:
            p.rename(cible)
    return len(produits)


# --------------------------------------------------------------------------- #
# Application
# --------------------------------------------------------------------------- #

app = FastAPI(title="susi-bdc")


@app.on_event("startup")
def _reconcilier():
    # La console Windows est en cp1252 : un caractère hors de cette page (une
    # flèche unicode, par exemple) fait lever UnicodeEncodeError à print() et
    # **tue le démarrage du serveur**. S'en tenir à l'ASCII dans les messages
    # de console, quoi qu'il arrive.
    for job in reconcilier_statuts_au_demarrage():
        print(f"statut orphelin repare : {job} -> interrompu", flush=True)


CSS = """
:root{--bg:#f7f7f6;--fg:#1c1b19;--mut:#6b6862;--bd:#dedbd4;--ok:#1a7f4b;--tiede:#b06a00;--rouge:#b3261e;--acc:#1f5fa9}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,Segoe UI,Roboto,sans-serif}
header{background:#fff;border-bottom:1px solid var(--bd);padding:12px 22px;display:flex;align-items:center;gap:16px}
header h1{font-size:15px;margin:0;letter-spacing:.02em}
header .sp{flex:1}
header .who{color:var(--mut);font-size:12px}
main{max-width:1180px;margin:22px auto;padding:0 22px}
a{color:var(--acc)}
table{width:100%;border-collapse:collapse;background:#fff;border:1px solid var(--bd);border-radius:6px;overflow:hidden}
th,td{padding:9px 12px;text-align:left;border-bottom:1px solid var(--bd);font-size:13px;vertical-align:top}
th{background:#fbfbfa;font-weight:600;color:var(--mut);text-transform:uppercase;font-size:11px;letter-spacing:.05em}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:2px 9px;border-radius:11px;font-size:11px;font-weight:600}
.ok{background:#e4f4ea;color:var(--ok)}.tiede{background:#fdf0dc;color:var(--tiede)}.rouge{background:#fbe6e4;color:var(--rouge)}
.btn{display:inline-block;padding:7px 14px;border:1px solid var(--bd);border-radius:5px;background:#fff;cursor:pointer;font-size:13px;text-decoration:none;color:var(--fg)}
.btn:hover{background:#f2f1ee}
.btn.p{background:var(--acc);color:#fff;border-color:var(--acc)}
.btn[disabled]{opacity:.45;cursor:not-allowed}
.card{background:#fff;border:1px solid var(--bd);border-radius:6px;padding:16px;margin-bottom:16px}
.mut{color:var(--mut)}
.log{background:#14130f;color:#dedbd4;padding:14px;border-radius:6px;font:12px/1.5 Consolas,monospace;max-height:460px;overflow:auto;white-space:pre-wrap}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}
input,select,textarea{font:13px inherit;padding:6px 8px;border:1px solid var(--bd);border-radius:4px;width:100%;background:#fff}
.scan{width:100%;border:1px solid var(--bd);border-radius:5px;margin-bottom:10px}
.bar{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:14px 0}
.warn{background:#fdf0dc;border:1px solid #eccb95;color:#7a4a00;padding:10px 13px;border-radius:5px;margin-bottom:14px;font-size:13px}
.danger{background:#fbe6e4;border:1px solid #eeb4af;color:#8c1d18;padding:10px 13px;border-radius:5px;margin-bottom:14px;font-size:13px}
code{background:#f0efec;padding:1px 5px;border-radius:3px;font-size:12px}
"""


def page(titre: str, corps: str, compte: dict) -> str:
    return f"""<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{titre} · susi-bdc</title><style>{CSS}</style></head><body>
<header><h1>susi-bdc</h1><span class="mut">saisie des BDC papier → SUSI</span>
<span class="sp"></span><span class="who">{compte['nom']} · {compte['role']}</span>
<a class="btn" href="/">Lots</a></header><main>{corps}</main></body></html>"""


@app.get("/", response_class=HTMLResponse)
def accueil(compte: dict = Depends(compte_courant)):
    lignes = []
    for d in sorted(LOTS.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        e = etat_lot(d)
        m = e["meta"]
        prog = f"{e['nb_extraits']}/{e['nb_attendu']}" if e["nb_attendu"] else str(e["nb_extraits"])
        badge = f'<span class="pill tiede">{e["job_en_cours"]} en cours</span>' if e["job_en_cours"] else e["phase"]
        lignes.append(
            f"<tr><td><a href='/lot/{d.name}'>{d.name}</a></td>"
            f"<td>{m.get('pays','?')} · {m.get('type','?')} · {m.get('paiement','?')}</td>"
            f"<td>{e['nb_pages']} pages</td><td>{prog}</td><td>{badge}</td></tr>")

    corps = f"""
<div class="card"><form method="post" action="/upload" enctype="multipart/form-data" class="bar">
<strong>Nouveau lot</strong>
<input type="file" name="fichier" accept="application/pdf" required style="width:auto">
<button class="btn p" type="submit" {"disabled" if compte["role"] != "admin" else ""}>Téléverser</button>
<span class="mut">nom attendu : <code>JJMMAAAAFR ATLAS [III] FID|REC [OA|CB|C3M] 50CH1.pdf</code></span>
</form></div>
<table><tr><th>Lot</th><th>Nature</th><th>Scan</th><th>Extraits</th><th>Phase</th></tr>
{"".join(lignes) or "<tr><td colspan=5 class=mut>Aucun lot. Téléversez un PDF de liasse.</td></tr>"}</table>"""
    return page("Lots", corps, compte)


@app.post("/upload")
async def upload(fichier: UploadFile = File(...), compte: dict = Depends(compte_courant)):
    meta = parser_nom(fichier.filename or "")
    if not meta:
        raise HTTPException(400, f"nom de fichier non conforme : {fichier.filename}")
    dossier = LOTS / nom_lot(fichier.filename)
    dossier.mkdir(exist_ok=True)
    pdf = dossier / (fichier.filename or "lot.pdf")
    pdf.write_bytes(await fichier.read())
    meta["fichier"] = pdf.name
    meta["cree"] = datetime.now().isoformat(timespec="seconds")
    # créés dès l'accueil : le harness interdit à l'agent de créer un sous-dossier
    for sous in ("extraits", "jobs"):
        (dossier / sous).mkdir(exist_ok=True)
    try:
        meta["nb_pages"] = rendre_pages(pdf, dossier / "pages")
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        meta["nb_pages"] = 0
        meta["erreur_rendu"] = str(exc)
    (dossier / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
    return RedirectResponse(f"/lot/{dossier.name}", status_code=303)


@app.get("/lot/{nom}", response_class=HTMLResponse)
def voir_lot(nom: str, compte: dict = Depends(compte_courant)):
    dossier = LOTS / nom
    if not dossier.is_dir():
        raise HTTPException(404, "lot inconnu")
    e = etat_lot(dossier)
    cmds = charger_commandes(dossier)
    m = e["meta"]

    lignes = []
    for c in cmds:
        r = c.get("revue", {})
        lignes.append(
            f"<tr><td><a href='/lot/{nom}/cmd/{c['_id']}'>#{c['_id']}</a></td>"
            f"<td>{c.get('client', {}).get('nom', '') or c.get('client_nom', '') or '<span class=mut>—</span>'}</td>"
            f"<td>{c.get('total', '—')} €</td>"
            f"<td>{len(c.get('articles') or [])}</td>"
            f"<td><span class='pill {c['_classe']}'>{c['_score']}</span></td>"
            f"<td class=mut>{r.get('statut','')}</td></tr>")

    alerte = ""
    if not e["a_validation"] and e["phase"] in ("validation", "revue"):
        alerte = ('<div class="warn">La saisie est verrouillée : <code>validation.json</code> '
                  'est absent. La revue humaine est le point de passage obligatoire.</div>')
    if e["a_validation"] and not e["a_saisie"]:
        alerte += ('<div class="danger">La saisie écrit de <strong>vraies commandes</strong> et de '
                   '<strong>vrais encaissements</strong> dans SUSI. Ne la lancez pas sans supervision.</div>')

    peut = compte["role"] == "admin" and not e["job_en_cours"]
    corps = f"""
<h2 style="margin:0 0 4px">{nom}</h2>
<p class="mut">{m.get('date','')} · {m.get('pays','')} · {m.get('type','')} ·
paiement {m.get('paiement','')} · {m.get('nb_commandes','?')} commandes annoncées ·
{e['nb_pages']} pages rendues · phase <strong>{e['phase']}</strong></p>
{alerte}
<div class="bar">
<form method="post" action="/lot/{nom}/job/extraction"><button class="btn" {"" if peut else "disabled"}>Lancer l'extraction</button></form>
<form method="post" action="/lot/{nom}/valider"><button class="btn p" {"" if peut and cmds else "disabled"}>Valider le lot</button></form>
<form method="post" action="/lot/{nom}/job/saisie"><button class="btn" {"" if peut and e["a_validation"] else "disabled"}>Saisir dans SUSI</button></form>
<form method="post" action="/lot/{nom}/job/verification"><button class="btn" {"" if peut and e["a_saisie"] else "disabled"}>Vérifier</button></form>
<a class="btn" href="/lot/{nom}/log/extraction">Log extraction</a>
</div>
<table><tr><th>Cmd</th><th>Client</th><th>Total</th><th>Articles</th><th>Score</th><th>Revue</th></tr>
{"".join(lignes) or "<tr><td colspan=6 class=mut>Aucun extrait. Lancez l'extraction.</td></tr>"}</table>
<p class="mut" style="margin-top:14px">Échelle : <span class="pill ok">≥ 90</span> OK ·
<span class="pill tiede">70-89</span> à vérifier · <span class="pill rouge">&lt; 70</span> revue obligatoire</p>"""
    return page(nom, corps, compte)


@app.get("/lot/{nom}/cmd/{cid}", response_class=HTMLResponse)
def voir_cmd(nom: str, cid: str, compte: dict = Depends(compte_courant)):
    dossier = LOTS / nom
    f = dossier / "extraits" / f"cmd_{cid}.json"
    if not f.exists():
        raise HTTPException(404, "commande inconnue")
    cmd = json.loads(f.read_text(encoding="utf-8"))
    score, motifs = score_extraction(cmd)
    r = cmd.get("revue", {}) or {}

    scans = ""
    for p in cmd.get("pages") or []:
        scans += f"<img class='scan' src='/lot/{nom}/page/{int(p):03d}' alt='page {p}'>"
    if not scans:
        scans = "<p class='mut'>Aucune page associée dans l'extrait (<code>pages</code>).</p>"

    corps = f"""
<p class="mut"><a href="/lot/{nom}">← {nom}</a></p>
<h2 style="margin:0 0 10px">Commande #{cid} <span class="pill {classe_score(score)}">{score}</span></h2>
{"<div class='warn'><strong>Retenues :</strong><br>" + "<br>".join(motifs) + "</div>" if motifs else ""}
<div class="grid">
  <div><div class="card"><strong>Scans</strong>{scans}</div></div>
  <div><form method="post" action="/lot/{nom}/cmd/{cid}">
    <div class="card"><strong>Extraction</strong>
      <p class="mut" style="font-size:12px">JSON éditable — tout est modifiable en revue.</p>
      <textarea name="json" rows="22" style="font:12px Consolas,monospace">{json.dumps({k: v for k, v in cmd.items() if not k.startswith('_') and k != 'revue'}, ensure_ascii=False, indent=1)}</textarea>
    </div>
    <div class="card"><strong>Revue</strong>
      <p><label class="mut">Statut</label>
      <select name="statut">
        {"".join(f"<option value='{s}' {'selected' if r.get('statut') == s else ''}>{s}</option>" for s in ('a_verifier', 'valide', 'ecarte'))}
      </select></p>
      <p><label class="mut">Commentaire</label>
      <input name="commentaire" value="{(r.get('commentaire') or '').replace('"', '&quot;')}"></p>
      <button class="btn p" {"disabled" if compte["role"] != "admin" else ""}>Enregistrer</button>
    </div>
  </form></div>
</div>"""
    return page(f"cmd {cid}", corps, compte)


@app.post("/lot/{nom}/cmd/{cid}")
def maj_cmd(nom: str, cid: str, json_: str = Form(alias="json"),
            statut: str = Form("a_verifier"), commentaire: str = Form(""),
            compte: dict = Depends(compte_courant)):
    f = LOTS / nom / "extraits" / f"cmd_{cid}.json"
    if not f.exists():
        raise HTTPException(404, "commande inconnue")
    try:
        cmd = json.loads(json_)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"JSON invalide : {exc}") from exc
    cmd["revue"] = {"statut": statut, "verifie": statut != "a_verifier",
                    "par": compte["nom"], "commentaire": commentaire,
                    "le": datetime.now().isoformat(timespec="seconds")}
    # écriture atomique : jamais de fichier à moitié écrit pour un lecteur concurrent
    tmp = f.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cmd, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, f)
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.get("/lot/{nom}/page/{num}")
def page_image(nom: str, num: str, compte: dict = Depends(compte_courant)):
    p = LOTS / nom / "pages" / f"p-{int(num):03d}.jpg"
    if not p.exists():
        raise HTTPException(404, "page inconnue")
    return FileResponse(p, media_type="image/jpeg")


@app.post("/lot/{nom}/job/{genre}")
def demarrer_job(nom: str, genre: str, compte: dict = Depends(compte_courant)):
    dossier = LOTS / nom
    if not dossier.is_dir():
        raise HTTPException(404, "lot inconnu")
    ok, msg = lancer_job(dossier, genre)
    if not ok:
        raise HTTPException(409, msg)
    return RedirectResponse(f"/lot/{nom}/log/{genre}", status_code=303)


@app.get("/lot/{nom}/log/{genre}", response_class=HTMLResponse)
def voir_log(nom: str, genre: str, compte: dict = Depends(compte_courant)):
    dossier = LOTS / nom
    log = dossier / "jobs" / f"{genre}.log"
    brut = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
    lignes = _lignes_lisibles(brut)
    e = etat_lot(dossier)
    st = e["jobs"].get(genre, {})
    rafraichir = '<meta http-equiv="refresh" content="5">' if st.get("statut") == "en_cours" else ""
    corps = f"""{rafraichir}
<p class="mut"><a href="/lot/{nom}">← {nom}</a></p>
<h2 style="margin:0 0 10px">Job {genre} <span class="pill {'tiede' if st.get('statut') == 'en_cours' else ('ok' if st.get('statut') == 'termine' else 'rouge')}">{st.get('statut', 'jamais lancé')}</span></h2>
<p class="mut">{len(lignes)} lignes{" · rafraîchi toutes les 5 s" if st.get("statut") == "en_cours" else ""}</p>
<div class="log">{"<br>".join(l.replace("<", "&lt;") for l in lignes) or "(vide)"}</div>"""
    return page(f"log {genre}", corps, compte)


@app.post("/lot/{nom}/valider")
def valider(nom: str, compte: dict = Depends(compte_courant)):
    """Checkpoint humain. Sans ce fichier, la saisie est refusée."""
    dossier = LOTS / nom
    cmds = charger_commandes(dossier)
    if not cmds:
        raise HTTPException(409, "rien à valider : aucun extrait")
    retenues = [c for c in cmds if (c.get("revue") or {}).get("statut") != "ecarte"]

    # Un doublon de scan encore retenu = un encaissement en trop si on saisit.
    # On refuse la validation plutôt que de compter sur la vigilance du relecteur.
    bloquants = [c for c in retenues
                 if any("DOUBLON DE SCAN" in m for m in c.get("_doublons") or [])]
    if bloquants:
        detail = " ; ".join(f"#{c['_id']} : {c['_doublons'][0]}" for c in bloquants)
        raise HTTPException(409, "validation refusée — doublon(s) de scan non résolu(s). "
                                 "Écartez la copie superflue avant de valider. " + detail)

    total = sum(float(c.get("total") or 0) for c in retenues)
    payload = {
        "valide_par": compte["nom"],
        "le": datetime.now().isoformat(timespec="seconds"),
        "nb_commandes": len(retenues),
        "nb_ecartees": len(cmds) - len(retenues),
        "total_eur": round(total, 2),
        "scores": {c["_id"]: c["_score"] for c in retenues},
        "commandes": [c["_id"] for c in retenues],
        "ecartees": [c["_id"] for c in cmds if (c.get("revue") or {}).get("statut") == "ecarte"],
    }
    (dossier / "validation.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return RedirectResponse(f"/lot/{nom}", status_code=303)


@app.get("/lot/{nom}/verification", response_class=HTMLResponse)
def voir_verification(nom: str, compte: dict = Depends(compte_courant)):
    f = LOTS / nom / "verification.json"
    if not f.exists():
        raise HTTPException(404, "aucune vérification")
    data = json.loads(f.read_text(encoding="utf-8"))
    cmds = sorted(data.get("commandes", []), key=lambda c: c.get("score", 0))
    lignes = "".join(
        f"<tr><td>#{c.get('id')}</td><td><span class='pill {classe_score(c.get('score', 0))}'>"
        f"{c.get('score', 0)}</span></td><td>{c.get('verdict', '')}</td>"
        f"<td class=mut>{'; '.join(c.get('ecarts', []))}</td></tr>" for c in cmds)
    corps = f"""<p class="mut"><a href="/lot/{nom}">← {nom}</a></p>
<h2>Vérification post-saisie</h2>
<table><tr><th>Cmd</th><th>Score</th><th>Verdict</th><th>Écarts</th></tr>{lignes}</table>"""
    return page("vérification", corps, compte)


@app.get("/api/lots")
def api_lots(compte: dict = Depends(compte_courant)):
    return JSONResponse([etat_lot(d) for d in sorted(LOTS.iterdir()) if d.is_dir()])


@app.get("/api/lot/{nom}")
def api_lot(nom: str, compte: dict = Depends(compte_courant)):
    dossier = LOTS / nom
    if not dossier.is_dir():
        raise HTTPException(404, "lot inconnu")
    return JSONResponse({"etat": etat_lot(dossier), "commandes": charger_commandes(dossier)})


@app.get("/sante")
def sante():
    """Diagnostic. `job_actif` rend le verrou global observable : le runbook cite le
    verrou orphelin (en mémoire, perdu à un redémarrage) comme symptôme connu, et
    sans ce champ on ne peut pas distinguer « un job tourne » de « le verrou a
    fui » autrement qu'en tentant un lancement."""
    return {
        "ok": True,
        "pdftoppm": PDFTOPPM,
        "lots": len([d for d in LOTS.iterdir() if d.is_dir()]),
        "job_actif": _JOB_ACTIF,
        "verrou_pris": _JOB_ACTIF is not None,
        "job_vivant": _job_vraiment_vivant(),
        "verrou_orphelin": _JOB_ACTIF is not None and not _job_vraiment_vivant(),
    }


@app.post("/jobs/liberer")
def liberer(compte: dict = Depends(compte_courant)):
    """Soupape manuelle : relâche un verrou dont le processus a disparu.

    Le lancement d'un job le fait déjà tout seul ; cet endpoint sert au
    diagnostic et aux cas où l'on veut débloquer sans relancer.
    """
    orphelin = liberer_verrou_orphelin()
    if orphelin is None:
        if _JOB_ACTIF is None:
            return {"libere": None, "message": "aucun verrou pris"}
        raise HTTPException(409, f"le job {_JOB_ACTIF} tourne réellement — "
                                 "l'interrompre plutôt que de forcer le verrou")
    return {"libere": orphelin, "message": "verrou orphelin relâché"}
