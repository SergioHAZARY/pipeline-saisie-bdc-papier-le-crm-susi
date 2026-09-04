"""Écriture du fichier de remise TLMC (enregistrements à longueur fixe, CFONB).

⚠️ Le format exact des enregistrements (codes, positions, longueur — souvent
160 caractères) dépend du cahier des charges TLMC de la banque. Il N'EST PAS
inventé ici : déposer la spec dans spec/ puis décrire la structure dans
spec/layout.json. Tant que ce fichier n'existe pas, l'écriture s'arrête avec
un message clair — mais controle.csv et rejets.csv sont toujours produits.

Format attendu de spec/layout.json :
{
  "longueur_enregistrement": 160,
  "fin_de_ligne": "\r\n",
  "enregistrements": {
    "entete":  [ {"nom": "code", "type": "an", "longueur": 2, "valeur": "…"}, … ],
    "detail":  [ … ],
    "total":   [ … ]
  }
}
Types de champ : "n" (numérique, cadré droite, complété par des zéros),
"an" (alphanumérique, cadré gauche, complété par des espaces).
Champs dynamiques : "valeur" absente et "source" ∈ {"numero_cheque",
"zone_interbancaire", "numero_compte", "montant_centimes", "nombre_cheques",
"total_centimes", "date_remise" (JJMMAA)}.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .cmc7 import LigneCMC7


class SpecManquante(RuntimeError):
    """La spec TLMC de la banque n'a pas encore été fournie/décrite."""


@dataclass
class ChequeValide:
    cmc7: LigneCMC7
    montant_centimes: int
    fichier_source: str


# ---------------------------------------------------------------------------
# Utilitaires longueur fixe (testables sans spec)
# ---------------------------------------------------------------------------

def champ_numerique(valeur: int | str, longueur: int) -> str:
    """Cadre à droite, complète par des zéros. Erreur si dépassement."""
    texte = str(valeur)
    if not texte.isdigit():
        raise ValueError(f"champ numérique attendu : {valeur!r}")
    if len(texte) > longueur:
        raise ValueError(f"{valeur!r} dépasse {longueur} caractères")
    return texte.rjust(longueur, "0")


def champ_alpha(valeur: str, longueur: int) -> str:
    """Cadre à gauche, complète par des espaces. Erreur si dépassement."""
    texte = str(valeur)
    if len(texte) > longueur:
        raise ValueError(f"{valeur!r} dépasse {longueur} caractères")
    return texte.ljust(longueur, " ")


def total_remise(cheques: list[ChequeValide]) -> int:
    """Total de la remise en centimes — toujours recalculé, jamais codé en dur."""
    return sum(c.montant_centimes for c in cheques)


# ---------------------------------------------------------------------------
# Writer piloté par la spec
# ---------------------------------------------------------------------------

def charger_layout(dossier_spec: Path) -> dict:
    layout = dossier_spec / "layout.json"
    if not layout.exists():
        raise SpecManquante(
            "spec/layout.json introuvable. Déposer le cahier des charges TLMC "
            "de la banque dans spec/ puis décrire la structure des "
            "enregistrements dans spec/layout.json (voir tlmc/writer.py). "
            "Le fichier TLMC ne sera pas généré sans cela."
        )
    return json.loads(layout.read_text(encoding="utf-8"))


def _valeur_champ(champ: dict, contexte: dict) -> str:
    if "valeur" in champ:
        brute = champ["valeur"]
    else:
        source = champ.get("source")
        if source not in contexte:
            raise ValueError(f"source inconnue dans layout.json : {source!r}")
        brute = contexte[source]
    if champ["type"] == "n":
        return champ_numerique(brute, champ["longueur"])
    return champ_alpha(str(brute), champ["longueur"])


def _enregistrement(champs: list[dict], contexte: dict, longueur: int) -> str:
    ligne = "".join(_valeur_champ(c, contexte) for c in champs)
    if len(ligne) != longueur:
        raise ValueError(
            f"enregistrement de {len(ligne)} caractères au lieu de {longueur} — "
            "vérifier layout.json"
        )
    return ligne


def ecrire_remise(
    cheques: list[ChequeValide],
    destination: Path,
    dossier_spec: Path,
    date_remise: date | None = None,
    surcharges: dict | None = None,
) -> Path:
    """Écrit le fichier TLMC selon spec/layout.json. Retourne le chemin écrit.

    `surcharges` écrase ponctuellement les "parametres" du layout (ex :
    numero_remise saisi dans l'UI).
    """
    layout = charger_layout(dossier_spec)
    longueur = layout["longueur_enregistrement"]
    eol = layout.get("fin_de_ligne", "\r\n")
    enregistrements = layout["enregistrements"]
    date_remise = date_remise or date.today()

    parametres = {**layout.get("parametres", {}), **(surcharges or {})}
    # entete + n détails + total
    nombre_enregistrements = len(cheques) + 2
    reference_base = int(parametres.get("reference_base", 0))

    contexte_commun = {
        **parametres,
        "numero_remise_7": str(parametres.get("numero_remise", "0")).zfill(7),
        "date_remise": date_remise.strftime("%d%m%y"),
        "date_remise_aaaammjj": date_remise.strftime("%Y%m%d"),
        "nombre_cheques": len(cheques),
        "nombre_enregistrements": nombre_enregistrements,
        "total_centimes": total_remise(cheques),
    }

    lignes = [_enregistrement(enregistrements["entete"],
                              {**contexte_commun, "sequence": 1}, longueur)]
    for i, cheque in enumerate(cheques, start=1):
        contexte = {
            **contexte_commun,
            "sequence": i + 1,
            "reference_cheque": reference_base + i,
            "numero_cheque": cheque.cmc7.numero_cheque,
            "zone_interbancaire": cheque.cmc7.zone_interbancaire,
            "numero_compte": cheque.cmc7.numero_compte,
            "montant_centimes": cheque.montant_centimes,
        }
        lignes.append(_enregistrement(enregistrements["detail"], contexte, longueur))
    lignes.append(_enregistrement(enregistrements["total"], contexte_commun, longueur))

    # écriture binaire : CR seul entre les enregistrements, SANS terminateur
    # final — les fichiers acceptés par la BRED se terminent juste après le 08.
    destination.write_bytes(eol.join(lignes).encode("ascii"))
    return destination
