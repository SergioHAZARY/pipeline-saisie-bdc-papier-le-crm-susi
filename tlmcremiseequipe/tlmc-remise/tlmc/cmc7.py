"""Parsing et validation de la ligne magnétique CMC7 d'un chèque français.

Structure attendue (à confirmer avec la spec TLMC de la banque, cf. spec/) :
  zone 1 : numéro de chèque        — 7 chiffres
  zone 2 : zone interbancaire      — 12 chiffres (code banque 5 + code guichet 5 + ...)
  zone 3 : numéro de compte        — 12 chiffres
Les zones sont séparées par les symboles CMC7 (transcrits ici par tout
caractère non numérique : espace, ';', ':', '<', etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Longueurs de zones par défaut (chèque français standard). Ajustables si la
# spec banque diffère.
ZONES_ATTENDUES = (7, 12, 12)


class CMC7Error(ValueError):
    """Ligne CMC7 invalide."""


@dataclass
class LigneCMC7:
    numero_cheque: str
    zone_interbancaire: str
    numero_compte: str
    brut: str = ""
    avertissements: list[str] = field(default_factory=list)

    @property
    def code_banque(self) -> str:
        return self.zone_interbancaire[:5]

    @property
    def code_guichet(self) -> str:
        return self.zone_interbancaire[5:10]


def parser_ligne(brut: str, zones_attendues: tuple[int, ...] = ZONES_ATTENDUES) -> LigneCMC7:
    """Parse une ligne CMC7 brute (sortie OCR) en zones.

    Tolérant sur les séparateurs : tout groupe de caractères non numériques
    sépare deux zones. Lève CMC7Error si le nombre de zones ne correspond pas
    ou si une zone est vide ; les écarts de longueur sont remontés en
    avertissements (pas bloquants tant que la spec n'est pas confirmée).
    """
    if not brut or not brut.strip():
        raise CMC7Error("ligne CMC7 vide")

    zones = [z for z in re.split(r"[^0-9]+", brut.strip()) if z]
    if len(zones) != len(zones_attendues):
        raise CMC7Error(
            f"{len(zones)} zone(s) trouvée(s) au lieu de {len(zones_attendues)} : {zones!r}"
        )

    avertissements = [
        f"zone {i + 1} : {len(zone)} chiffres au lieu de {attendu} ({zone})"
        for i, (zone, attendu) in enumerate(zip(zones, zones_attendues))
        if len(zone) != attendu
    ]

    return LigneCMC7(
        numero_cheque=zones[0],
        zone_interbancaire=zones[1],
        numero_compte=zones[2],
        brut=brut.strip(),
        avertissements=avertissements,
    )


def montant_en_centimes(montant: str | float) -> int:
    """Convertit un montant ('123,45', '123.45' ou float) en centimes (int).

    Lève CMC7Error si le montant est illisible ou négatif.
    """
    if isinstance(montant, (int, float)):
        centimes = round(float(montant) * 100)
    else:
        texte = montant.strip().replace(" ", "").replace(" ", "").replace("€", "")
        texte = texte.replace(",", ".")
        if texte.count(".") > 1:  # séparateur de milliers type 1.234.56 — ambigu
            raise CMC7Error(f"montant ambigu : {montant!r}")
        try:
            centimes = round(float(texte) * 100)
        except ValueError:
            raise CMC7Error(f"montant illisible : {montant!r}") from None
    if centimes < 0:
        raise CMC7Error(f"montant négatif : {montant!r}")
    return centimes


# ---------------------------------------------------------------------------
# Clé RLMC : le nombre entre parenthèses imprimé sur le chèque valide toute la
# ligne CMC7 : cle == 97 - (100 × int(z1+z2+z3)) % 97
# ---------------------------------------------------------------------------

def cle_rlmc_valide(z1: str, z2: str, z3: str, cle: int | str) -> bool:
    ligne = f"{z1}{z2}{z3}"
    if not ligne.isdigit():
        return False
    try:
        return int(cle) == 97 - (100 * int(ligne)) % 97
    except (TypeError, ValueError):
        return False


def resoudre_par_cle(z1: str, z2: str, z3: str, cle: int | str,
                     max_inconnues: int = 3) -> tuple[str, str, str] | None:
    """Tente de réparer une ligne CMC7 grâce à la clé RLMC.

    Gère les '?' (chiffres illisibles, max 3) et, à défaut, la correction d'un
    unique chiffre mal lu. Retourne les zones corrigées seulement si la
    solution est UNIQUE — sinon None (l'ambiguïté revient à l'humain).
    """
    import itertools

    try:
        cle = int(cle)
    except (TypeError, ValueError):
        return None
    ligne = f"{z1}{z2}{z3}"
    if len(ligne) != 31 or any(c != "?" and not c.isdigit() for c in ligne):
        return None

    def decoupe(l):
        return l[:7], l[7:19], l[19:31]

    inconnues = [i for i, c in enumerate(ligne) if c == "?"]
    if inconnues:
        if len(inconnues) > max_inconnues:
            return None
        solutions = []
        for combo in itertools.product("0123456789", repeat=len(inconnues)):
            essai = list(ligne)
            for pos, chiffre in zip(inconnues, combo):
                essai[pos] = chiffre
            essai = "".join(essai)
            if int(cle) == 97 - (100 * int(essai)) % 97:
                solutions.append(essai)
                if len(solutions) > 1:
                    return None
        return decoupe(solutions[0]) if len(solutions) == 1 else None

    if cle_rlmc_valide(z1, z2, z3, cle):
        return decoupe(ligne)
    # un seul chiffre mal lu ?
    solutions = []
    for i in range(31):
        for chiffre in "0123456789":
            if chiffre == ligne[i]:
                continue
            essai = ligne[:i] + chiffre + ligne[i + 1:]
            if int(cle) == 97 - (100 * int(essai)) % 97:
                solutions.append(essai)
                if len(solutions) > 1:
                    return None
    return decoupe(solutions[0]) if len(solutions) == 1 else None
