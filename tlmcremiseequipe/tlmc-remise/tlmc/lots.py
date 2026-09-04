"""Persistance et édition des lots de chèques scannés.

Chaque lot vit dans sessions/<id>/ :
  - session.json : état éditable (chèques, zones CMC7, montants, avertissements,
    indicateur "isole" pour retirer un chèque de la remise)
  - cheques/*.png : image de chaque chèque (pour contrôle visuel dans l'UI)
  - rejets/, sortie/ : comme avant
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from .cmc7 import CMC7Error, cle_rlmc_valide, montant_en_centimes, parser_ligne
from .writer import ChequeValide, charger_layout, ecrire_remise, total_remise


def charger_layout_numero(dossier_spec: Path) -> str:
    try:
        return charger_layout(dossier_spec).get("parametres", {}).get("numero_remise", "000000")
    except Exception:
        return "000000"


def charger_lot(session_dir: Path) -> dict | None:
    fichier = session_dir / "session.json"
    if not fichier.exists():
        return None
    return json.loads(fichier.read_text(encoding="utf-8"))


def sauver_lot(session_dir: Path, lot: dict) -> None:
    # écriture atomique : jamais de fichier à moitié écrit pour un lecteur concurrent
    import os
    tmp = session_dir / "session.json.tmp"
    tmp.write_text(json.dumps(lot, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, session_dir / "session.json")


def lister_lots(dossier_sessions: Path) -> list[tuple[str, dict]]:
    lots = []
    if dossier_sessions.exists():
        for d in sorted(dossier_sessions.iterdir(), reverse=True):
            lot = charger_lot(d)
            if lot:
                lots.append((d.name, lot))
    return lots


def valider_cheque(cheque: dict) -> list[str]:
    """Revalide un chèque après édition ; retourne les problèmes bloquants."""
    problemes = []
    ligne = f"{cheque.get('z1', '')} {cheque.get('z2', '')} {cheque.get('z3', '')}"
    try:
        cmc7 = parser_ligne(ligne)
        problemes.extend(cmc7.avertissements)
        cle = str(cheque.get("cle", "")).strip()
        if cle and not cle_rlmc_valide(cmc7.numero_cheque, cmc7.zone_interbancaire,
                                       cmc7.numero_compte, cle):
            problemes.append(f"clé de contrôle ({cle}) invalide — un chiffre est faux")
    except CMC7Error as exc:
        problemes.append(f"CMC7 : {exc}")
    try:
        montant_en_centimes(str(cheque.get("montant_eur", "")))
    except CMC7Error as exc:
        problemes.append(f"montant : {exc}")
    return problemes


def stats_lot(lot: dict) -> dict:
    actifs = [c for c in lot["cheques"] if not c.get("isole")]
    total = 0
    invalides = 0
    for c in actifs:
        try:
            total += montant_en_centimes(str(c.get("montant_eur", "")))
        except CMC7Error:
            invalides += 1
    return {
        "nb_actifs": len(actifs),
        "nb_isoles": len(lot["cheques"]) - len(actifs),
        "nb_invalides": invalides,
        "total_centimes": total,
        "nb_rejets": len(lot.get("rejets", [])),
    }


def generer_tlmc(session_dir: Path, dossier_spec: Path,
                 numero_remise: str | None = None,
                 date_remise: date | None = None) -> Path:
    """Génère le fichier TLMC du lot (chèques non isolés uniquement)."""
    lot = charger_lot(session_dir)
    if lot is None:
        raise ValueError("lot introuvable")
    cheques = []
    problemes = []
    for c in lot["cheques"]:
        if c.get("isole"):
            continue
        try:
            cmc7 = parser_ligne(f"{c['z1']} {c['z2']} {c['z3']}")
            if len(cmc7.numero_cheque) != 7 or len(cmc7.zone_interbancaire) != 12 \
                    or len(cmc7.numero_compte) != 12:
                raise CMC7Error(
                    f"zones {len(cmc7.numero_cheque)}/{len(cmc7.zone_interbancaire)}"
                    f"/{len(cmc7.numero_compte)} chiffres (attendu 7/12/12)")
            cheques.append(ChequeValide(
                cmc7=cmc7,
                montant_centimes=montant_en_centimes(str(c["montant_eur"])),
                fichier_source=c.get("fichier", ""),
            ))
        except CMC7Error as exc:
            problemes.append(f"p{c.get('page', '?')} : {exc}")
    if problemes:
        raise ValueError(
            "chèque(s) à corriger ou isoler avant génération — " + " ; ".join(problemes))
    if not cheques:
        raise ValueError("aucun chèque actif dans le lot")
    date_remise = date_remise or date.today()
    sortie = session_dir / "sortie"
    sortie.mkdir(parents=True, exist_ok=True)
    surcharges = {"numero_remise": numero_remise} if numero_remise else None
    # nommage identique aux fichiers acceptés par la banque : les portails EBICS
    # résolvent le type d'ordre via des règles de nommage — .txt obligatoire
    numero_effectif = numero_remise or str(charger_layout_numero(dossier_spec))
    destination = sortie / (f"REMISE OUTSOURCIA OUT DU {date_remise:%d.%m.%Y} "
                            f"tlmc_{numero_effectif}.txt")
    ecrire_remise(cheques, destination, dossier_spec,
                  date_remise=date_remise, surcharges=surcharges)
    lot["dernier_tlmc"] = {
        "fichier": destination.name,
        "genere_le": f"{date.today():%Y-%m-%d}",
        "nb_cheques": len(cheques),
        "total_centimes": total_remise(cheques),
        "numero_remise": numero_remise or "",
    }
    sauver_lot(session_dir, lot)
    return destination


def lot_depuis_resultat(resultat, session_dir: Path, source: str) -> dict:
    """Construit et sauve session.json depuis un ResultatRemise du pipeline."""
    cheques = []
    for i, l in enumerate(resultat.lignes_controle):
        cheques.append({
            "id": i,
            "fichier": l["fichier"], "page": l["page"],
            "banque": l.get("banque_nom", ""), "titulaire": l.get("titulaire", ""),
            "z1": l["numero_cheque"],
            "z2": l["cmc7"].split()[1] if len(l["cmc7"].split()) == 3 else "",
            "z3": l["compte"],
            "montant_eur": l["montant_eur"],
            "cle": l.get("cle", ""),
            "methode": l["methode"], "confiance": l["confiance"],
            "avertissements": [a for a in l["avertissements"].split(" | ") if a],
            "isole": False,
            "image": l.get("image", ""),
        })
    lot = {
        "source": source,
        "cree_le": f"{date.today():%Y-%m-%d}",
        "cheques": cheques,
        "rejets": resultat.rejets,
        "ignorees": getattr(resultat, "ignorees", []),
    }
    sauver_lot(session_dir, lot)
    return lot
