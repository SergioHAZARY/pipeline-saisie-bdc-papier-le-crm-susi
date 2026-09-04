"""Pipeline de traitement partagé entre le CLI (remise.py) et l'UI (app.py)."""

from __future__ import annotations

import os
import threading

# Plafond global de pages en cours de lecture (vision + OSD), tous lots
# confondus : 30 fichiers lancés en parallèle avancent tous, sans dépasser
# ce nombre d'appels API/processus simultanés. Réglable via TLMC_CONCURRENCE_GLOBALE.
_LIMITE_GLOBALE = threading.Semaphore(
    max(1, int(os.environ.get("TLMC_CONCURRENCE_GLOBALE", "16"))))
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .cmc7 import (CMC7Error, cle_rlmc_valide, montant_en_centimes,
                   parser_ligne, resoudre_par_cle)
from .ocr import (EXTENSIONS_IMAGES, charger_images, corriger_orientation,
                  extraire_cheque)
from .writer import ChequeValide, SpecManquante, ecrire_remise, total_remise


@dataclass
class ResultatRemise:
    retenus: list[ChequeValide] = field(default_factory=list)
    lignes_controle: list[dict] = field(default_factory=list)
    rejets: list[dict] = field(default_factory=list)
    ignorees: list[dict] = field(default_factory=list)
    chemin_tlmc: Path | None = None
    erreur_spec: str | None = None

    @property
    def total_centimes(self) -> int:
        return total_remise(self.retenus)


def collecter_fichiers(dossier: Path) -> list[Path]:
    """Tous les scans (PDF/images) du dossier, sous-dossiers compris."""
    extensions = EXTENSIONS_IMAGES | {".pdf"}
    return sorted(p for p in dossier.rglob("*") if p.suffix.lower() in extensions)


def arbitrer_montant(centimes: int, montant_lettres: float | None) -> tuple[int, str | None]:
    """Règle bancaire (art. L131-10 C. mon. fin.) : en cas de désaccord entre le
    montant en chiffres et le montant écrit en lettres, les LETTRES font foi."""
    if montant_lettres is None:
        return centimes, None
    try:
        centimes_lettres = montant_en_centimes(f"{montant_lettres:.2f}")
    except CMC7Error:
        return centimes, None
    if not centimes_lettres or centimes_lettres == centimes:
        return centimes, None
    return centimes_lettres, (
        f"montant : chiffres {centimes / 100:.2f} € ≠ lettres "
        f"{centimes_lettres / 100:.2f} € — montant EN LETTRES retenu (il fait foi)")


def _post_extraction(resultat, fichier, page, png, extraction,
                     dossier_rejets, dossier_cheques, log, notifier):
    """Validation et classement d'une page extraite (exécuté dans l'ordre des pages)."""
    if extraction.pas_un_cheque:
        log(f"  IGNORE {fichier.name} p{page} — pas un chèque (coupon/BC/document)")
        resultat.ignorees.append({"fichier": fichier.name, "page": page,
                                  "motif": "pas un chèque (coupon/BC/document)"})
        notifier({"fichier": fichier.name, "page": page, "statut": "ignore"})
        return

    motif_rejet = None
    cmc7 = centimes = None
    notes_cle = []
    notes_montant = []

    # la clé RLMC (nombre entre parenthèses) valide mathématiquement la ligne :
    # elle répare les '?' et détecte les coquilles avant tout autre contrôle
    if extraction.cle and extraction.ligne_cmc7:
        zones = extraction.ligne_cmc7.split()
        if len(zones) == 3:
            if "?" in extraction.ligne_cmc7 or not cle_rlmc_valide(*zones, extraction.cle):
                repare = resoudre_par_cle(*zones, extraction.cle)
                if repare:
                    extraction.ligne_cmc7 = " ".join(repare)
                    notes_cle.append("CMC7 réparée par la clé de contrôle "
                                     f"({extraction.cle}) ✓")
            elif cle_rlmc_valide(*zones, extraction.cle):
                notes_cle.append(f"clé de contrôle ({extraction.cle}) ✓")

    if extraction.erreur or not extraction.ligne_cmc7:
        motif_rejet = extraction.erreur or "ligne CMC7 non lue"
    elif "?" in extraction.ligne_cmc7 or extraction.confiance == "basse":
        motif_rejet = f"lecture douteuse (confiance {extraction.confiance}) : {extraction.ligne_cmc7}"
    elif not extraction.montant:
        motif_rejet = f"montant non lu (CMC7 : {extraction.ligne_cmc7})"
    else:
        try:
            cmc7 = parser_ligne(extraction.ligne_cmc7)
            centimes = montant_en_centimes(extraction.montant)
            centimes, note = arbitrer_montant(centimes, extraction.montant_lettres)
            if note:
                notes_montant.append(note)
        except CMC7Error as exc:
            motif_rejet = f"validation : {exc}"

    if motif_rejet:
        image_rejet = dossier_rejets / f"{fichier.stem}_p{page}.png"
        image_rejet.write_bytes(png)
        zones = (extraction.ligne_cmc7 or "").split()
        resultat.rejets.append({
            "fichier": fichier.name, "page": page,
            "motif": motif_rejet, "image": str(image_rejet),
            # lecture brute proposée : pré-remplit le formulaire de correction
            "z1": zones[0] if len(zones) > 0 else "",
            "z2": zones[1] if len(zones) > 1 else "",
            "z3": zones[2] if len(zones) > 2 else "",
            "montant": (extraction.montant or "").replace("€", "").strip(),
            "banque": extraction.banque_nom, "titulaire": extraction.titulaire,
        })
        log(f"  REJET  {fichier.name} p{page} — {motif_rejet}")
        notifier({"fichier": fichier.name, "page": page,
                  "statut": "rejet", "motif": motif_rejet})
        return

    image_cheque = ""
    if dossier_cheques:
        chemin_image = dossier_cheques / f"{fichier.stem}_p{page}.png"
        chemin_image.write_bytes(png)
        image_cheque = chemin_image.name
    avertissements_extra = list(cmc7.avertissements)
    avertissements_extra.extend(notes_montant)
    if extraction.cle:
        if not cle_rlmc_valide(cmc7.numero_cheque, cmc7.zone_interbancaire,
                               cmc7.numero_compte, extraction.cle):
            avertissements_extra.append(
                f"clé de contrôle ({extraction.cle}) INVALIDE — un chiffre de la "
                "ligne CMC7 est faux, à corriger")
        else:
            avertissements_extra.extend(a for a in notes_cle if a not in avertissements_extra)
    # double contrôle des montants inhabituels (panier moyen ~50 €)
    seuil = int(os.environ.get("TLMC_MONTANT_ALERTE", "50000"))
    if centimes > seuil:
        try:
            from .ocr import relire_montant_claude
            relecture = relire_montant_claude(png)
            coherents = []
            for source, valeur in (("chiffres", relecture.get("chiffres")),
                                   ("lettres", relecture.get("lettres"))):
                if valeur in (None, ""):
                    continue
                try:
                    coherents.append(montant_en_centimes(str(valeur)) == centimes)
                except CMC7Error:
                    coherents.append(False)
            if coherents and all(coherents):
                avertissements_extra.append(
                    f"montant élevé ({centimes / 100:.2f} €) confirmé par double lecture ✓")
            else:
                avertissements_extra.append(
                    f"montant élevé ({centimes / 100:.2f} €) NON confirmé par la double "
                    f"lecture (chiffres : {relecture.get('chiffres')}, lettres : "
                    f"{relecture.get('lettres')} €) — VÉRIFIER MANUELLEMENT")
        except Exception:
            avertissements_extra.append(
                f"montant élevé ({centimes / 100:.2f} €) — relecture impossible, "
                "VÉRIFIER MANUELLEMENT")
    if extraction.numero_imprime:
        imprime = "".join(ch for ch in extraction.numero_imprime if ch.isdigit())
        # les banques impriment souvent un préfixe de série (ex BP : « 26 6429004 C ») :
        # seule la fin doit correspondre au n° CMC7
        if imprime and not imprime.endswith(cmc7.numero_cheque.lstrip("0") or "0") \
                and not imprime.lstrip("0").endswith(cmc7.numero_cheque.lstrip("0") or "0"):
            avertissements_extra.append(
                f"n° imprimé {imprime} ≠ CMC7 {cmc7.numero_cheque}")
    resultat.retenus.append(ChequeValide(
        cmc7=cmc7, montant_centimes=centimes, fichier_source=fichier.name))
    resultat.lignes_controle.append({
        "image": image_cheque,
        "fichier": fichier.name, "page": page,
        "cmc7": cmc7.brut,
        "numero_cheque": cmc7.numero_cheque,
        "banque": cmc7.code_banque, "guichet": cmc7.code_guichet,
        "compte": cmc7.numero_compte,
        "montant_eur": f"{centimes / 100:.2f}",
        "banque_nom": extraction.banque_nom, "titulaire": extraction.titulaire,
        "cle": extraction.cle,
        "methode": extraction.methode, "confiance": extraction.confiance,
        "avertissements": " | ".join(avertissements_extra),
    })
    log(f"  OK     {fichier.name} p{page} — {centimes / 100:.2f} € ({extraction.methode})")
    notifier({"fichier": fichier.name, "page": page, "statut": "ok",
              "montant": f"{centimes / 100:.2f}"})


def traiter_dossier(
    dossier_scans: Path,
    dossier_rejets: Path,
    sortie_tlmc: Path,
    dossier_spec: Path,
    date_remise: date | None = None,
    dossier_cheques: Path | None = None,
    log=print,
    sur_progression=None,
    attendre_si_pause=None,
    options: dict | None = None,
) -> ResultatRemise:
    """Traite tous les scans d'un dossier et tente d'écrire la remise TLMC.

    Si `dossier_cheques` est fourni, l'image (redressée) de chaque chèque retenu
    y est sauvée pour contrôle visuel dans l'UI.
    """
    resultat = ResultatRemise()
    dossier_rejets.mkdir(parents=True, exist_ok=True)
    sortie_tlmc.parent.mkdir(parents=True, exist_ok=True)
    if dossier_cheques:
        dossier_cheques.mkdir(parents=True, exist_ok=True)

    # Extraction parallèle : les appels vision (réseau) dominent le temps de
    # traitement — un pool de workers avec fenêtre bornée (2×workers pages en
    # vol) permet ~100 pages en <2 min sans exploser la mémoire.
    workers = max(1, int(os.environ.get("TLMC_WORKERS", "8")))
    executor = ThreadPoolExecutor(max_workers=workers)
    en_vol: deque = deque()
    verrou = threading.Lock()

    def _notifier(evt):
        if sur_progression:
            with verrou:
                sur_progression(evt)

    def _corriger_et_extraire(fichier, page, png):
        with _LIMITE_GLOBALE:
            png = corriger_orientation(png)
            extraction = extraire_cheque(fichier, page, png, options)
            if extraction.png_redresse:
                png = extraction.png_redresse  # image stockée à l'endroit
            return png, extraction

    def _consommer(element):
        fichier, page, futur = element
        png, extraction = futur.result()
        _post_extraction(resultat, fichier, page, png, extraction,
                         dossier_rejets, dossier_cheques, log, _notifier)

    try:
        # NB : plus de filtre géométrique portrait/paysage — des chèques arrivent
        # aussi scannés en pleine page A4. La classification chèque/non-chèque
        # est faite par la lecture vision, page par page.
        for fichier in collecter_fichiers(dossier_scans):
            for page, png in charger_images(fichier):
                if attendre_si_pause:
                    attendre_si_pause()
                en_vol.append((fichier, page,
                               executor.submit(_corriger_et_extraire, fichier, page, png)))
                if len(en_vol) >= workers + 4:
                    _consommer(en_vol.popleft())
        while en_vol:
            _consommer(en_vol.popleft())
    finally:
        executor.shutdown(wait=True)

    if resultat.retenus:
        try:
            resultat.chemin_tlmc = ecrire_remise(
                resultat.retenus, sortie_tlmc, dossier_spec, date_remise=date_remise)
        except (SpecManquante, ValueError) as exc:
            resultat.erreur_spec = str(exc)
    return resultat


def ecrire_csv_controle(resultat: ResultatRemise, chemin: Path) -> Path | None:
    import csv

    if not resultat.lignes_controle:
        return None
    with chemin.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(resultat.lignes_controle[0]), delimiter=";")
        writer.writeheader()
        writer.writerows(resultat.lignes_controle)
    return chemin


def ecrire_csv_rejets(resultat: ResultatRemise, chemin: Path) -> Path | None:
    import csv

    if not resultat.rejets:
        return None
    with chemin.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=["fichier", "page", "motif", "image"], delimiter=";")
        writer.writeheader()
        writer.writerows(resultat.rejets)
    return chemin
