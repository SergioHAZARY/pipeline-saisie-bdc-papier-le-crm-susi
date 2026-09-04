"""Extraction de la ligne CMC7 et du montant depuis un scan de chèque.

Pipeline :
  1. PDF -> images (PyMuPDF, pas besoin de poppler)
  2. Crop de la bande CMC7 (bas du chèque) via OpenCV
  3. OCR : Tesseract avec un modèle 'cmc7' s'il est installé dans tessdata,
     sinon fallback vision via l'API Claude (sortie structurée).

Les imports lourds sont faits paresseusement pour que le reste du projet
(tests du writer, parsing CMC7) fonctionne sans ces dépendances.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

EXTENSIONS_IMAGES = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


@dataclass
class ExtractionCheque:
    fichier_source: str
    page: int
    ligne_cmc7: str | None
    montant: str | None
    confiance: str  # "haute" | "moyenne" | "basse"
    methode: str    # "tesseract" | "claude-vision"
    erreur: str | None = None
    pas_un_cheque: bool = False
    banque_nom: str = ""
    titulaire: str = ""
    numero_imprime: str = ""
    cle: str = ""
    # montant écrit en lettres, lu indépendamment des chiffres (il fait foi)
    montant_lettres: float | None = None
    # image retournée de 180° quand le chèque était scanné tête en bas et que
    # l'OSD tesseract ne l'a pas vu : à substituer à l'image d'origine
    png_redresse: bytes | None = None


# ---------------------------------------------------------------------------
# Chargement des scans
# ---------------------------------------------------------------------------

def charger_images(chemin: Path, dpi: int = 250):
    """Rend un itérable de (numéro_page, image PNG bytes) pour un PDF ou une image."""
    if chemin.suffix.lower() == ".pdf":
        import pymupdf

        doc = pymupdf.open(chemin)
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=dpi)
            yield i, pix.tobytes("png")
        doc.close()
    elif chemin.suffix.lower() in EXTENSIONS_IMAGES:
        yield 1, chemin.read_bytes()
    else:
        raise ValueError(f"format non géré : {chemin}")


# ---------------------------------------------------------------------------
# Prétraitement : orientation et tri des pages
# ---------------------------------------------------------------------------

def est_format_cheque(png_bytes: bytes) -> bool:
    """Vrai si la page est au format paysage d'un chèque (écarte les A4 portrait,
    typiquement des bons de commande scannés dans le même lot)."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return False
    h, w = img.shape
    return w > h * 1.5


def corriger_orientation(png_bytes: bytes) -> bytes:
    """Détecte et corrige une page scannée à l'envers via tesseract OSD.

    Les lots réels contiennent des chèques tête en bas ; l'OSD (osd.traineddata,
    livré avec tesseract) suffit à détecter 90/180/270°. Sans tesseract, ou si
    l'OSD échoue, l'image est retournée telle quelle.
    """
    if not shutil.which("tesseract"):
        return png_bytes
    try:
        osd = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "0"],
            input=png_bytes, capture_output=True, timeout=30,
        )
        rotation = 0
        for ligne in osd.stdout.decode(errors="replace").splitlines():
            if ligne.startswith("Rotate:"):
                rotation = int(ligne.split(":")[1])
    except (subprocess.SubprocessError, OSError, ValueError):
        return png_bytes
    if not rotation:
        return png_bytes

    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    sens = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
            270: cv2.ROTATE_90_COUNTERCLOCKWISE}.get(rotation)
    if img is None or sens is None:
        return png_bytes
    ok, encode = cv2.imencode(".png", cv2.rotate(img, sens))
    return encode.tobytes() if ok else png_bytes


def tourner_180(png_bytes: bytes) -> bytes:
    """Retourne l'image de 180° (chèque scanné tête en bas non détecté par l'OSD)."""
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return png_bytes
    ok, encode = cv2.imencode(".png", cv2.rotate(img, cv2.ROTATE_180))
    return encode.tobytes() if ok else png_bytes


def cle_coherente(ligne: str | None, cle: str | None) -> bool | None:
    """True/False si la clé RLMC valide la ligne, None si indécidable."""
    if not ligne or not cle:
        return None
    zones = ligne.split()
    if len(zones) != 3 or "?" in ligne or not all(z.isdigit() for z in zones):
        return None
    from .cmc7 import cle_rlmc_valide
    return cle_rlmc_valide(*zones, cle)


# ---------------------------------------------------------------------------
# Prétraitement : isoler la bande CMC7 (bas du chèque)
# ---------------------------------------------------------------------------

def crop_bande_cmc7(png_bytes: bytes, fraction_bas: float = 0.28) -> bytes:
    """Retourne la bande inférieure du chèque (zone CMC7), binarisée.

    Approche simple et robuste : on garde `fraction_bas` de la hauteur en bas
    de l'image, deskew léger + binarisation Otsu. Suffisant pour un scan droit ;
    à affiner sur les scans réels.
    """
    import cv2
    import numpy as np

    img = cv2.imdecode(np.frombuffer(png_bytes, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise ValueError("image illisible")
    h = img.shape[0]
    bande = img[int(h * (1 - fraction_bas)):, :]
    _, binaire = cv2.threshold(bande, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    ok, encode = cv2.imencode(".png", binaire)
    if not ok:
        raise ValueError("échec d'encodage de la bande CMC7")
    return encode.tobytes()


# ---------------------------------------------------------------------------
# OCR Tesseract (si un modèle CMC7 est installé)
# ---------------------------------------------------------------------------

def modele_cmc7_disponible() -> bool:
    """Vrai si tesseract est installé avec un traineddata 'cmc7'."""
    if not shutil.which("tesseract"):
        return False
    try:
        sortie = subprocess.run(
            ["tesseract", "--list-langs"], capture_output=True, text=True, timeout=10
        ).stdout
    except (subprocess.SubprocessError, OSError):
        return False
    return "cmc7" in sortie.split()


def ocr_tesseract(bande_png: bytes) -> str:
    """OCR de la bande CMC7 avec le modèle tesseract 'cmc7'."""
    proc = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", "cmc7", "--psm", "7"],
        input=bande_png,
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"tesseract a échoué : {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode(errors="replace").strip()


# ---------------------------------------------------------------------------
# Fallback : vision Claude avec sortie structurée
# ---------------------------------------------------------------------------

def lire_cheque_claude(png_bytes: bytes, options: dict | None = None) -> dict:
    """Lit la ligne CMC7 + le montant sur l'image complète du chèque via Claude.

    Retourne un dict {ligne_cmc7, montant, confiance}. Nécessite une clé API
    (ANTHROPIC_API_KEY ou profil `ant auth login`).
    """
    import base64

    import anthropic
    from pydantic import BaseModel

    options = options or {}
    lire_banque = options.get("lire_banque", True)
    lire_titulaire = options.get("lire_titulaire", True)
    lire_numero = options.get("lire_numero", True)

    class LectureCheque(BaseModel):
        est_un_cheque: bool
        ligne_cmc7: str | None
        montant: str | None
        montant_lettres_en_euros: float | None
        banque: str | None
        titulaire: str | None
        numero_cheque_imprime: str | None
        cle_controle: str | None
        tete_en_bas: bool
        confiance: str  # "haute" | "moyenne" | "basse"

    # L'API refuse les images > 10 Mo : recompresser en JPEG (scan rayé noir
    # → PNG énorme) avant l'envoi plutôt que de rejeter la page.
    media_type = "image/png"
    if len(png_bytes) > 9_500_000:
        import io
        from PIL import Image
        im = Image.open(io.BytesIO(png_bytes)).convert("L")
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=85)
        png_bytes, media_type = buf.getvalue(), "image/jpeg"
    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-5",
        # le raisonnement interne compte dans max_tokens : 1024 tronquait les
        # réponses (stop_reason max_tokens → rejets massifs)
        max_tokens=8000,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.standard_b64encode(png_bytes).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": (
                        "Ceci est un document scanné d'un lot de courrier. D'abord, "
                        "détermine s'il s'agit d'un CHÈQUE bancaire : si c'est un "
                        "coupon de paiement, une facture, un bon de commande ou tout "
                        "autre document, mets est_un_cheque=false et laisse les autres "
                        "champs à null. Si c'est bien un chèque, lis :\n"
                        "1. la ligne magnétique CMC7 en bas du chèque — recopie les "
                        "chiffres exactement, en séparant les 3 zones par un espace. "
                        "Zones attendues : 7 chiffres / 12 chiffres / 12 chiffres. "
                        "Les symboles CMC7 (petits glyphes ⑈ ⑆ ⑉ en début, fin et "
                        "entre les zones) ne sont PAS des chiffres : ne les transcris "
                        "jamais. Recopie fidèlement les zéros de tête (zone 1 = exactement "
                        "7 chiffres, zones 2 et 3 = exactement 12). Ne corrige ni ne "
                        "complète rien : si un chiffre est illisible, mets '?'.\n"
                        "2. le montant en CHIFFRES (dans le cadre à droite).\n"
                        "2bis. le montant EN LETTRES (lignes manuscrites « Payez "
                        "contre ce chèque… »), converti en nombre décimal d'euros "
                        "(ex : « vingt euros 89 » → 20.89). Lis chiffres et lettres "
                        "INDÉPENDAMMENT, sans corriger l'un par l'autre ; null si "
                        "la ligne en lettres est absente ou illisible.\n"
                        + ("3. la banque émettrice (logo/en-tête, ex : 'Crédit Agricole "
                           "des Savoie').\n" if lire_banque else
                           "3. banque : non demandée, mets null.\n")
                        + ("4. le titulaire du compte (nom imprimé au centre, au-dessus "
                           "de l'adresse).\n" if lire_titulaire else
                           "4. titulaire : non demandé, mets null.\n")
                        + ("5. le numéro de chèque imprimé (champ 'N° du chèque' ou "
                           "'Chèque N°', hors ligne magnétique).\n" if lire_numero else
                           "5. numéro imprimé : non demandé, mets null.\n")
                        + "6. la clé de contrôle : le nombre à 2 chiffres entre parenthèses "
                          "imprimé isolément sur le chèque, ex '(56)' — mets uniquement "
                          "les 2 chiffres, null si introuvable.\n"
                        + "7. tete_en_bas : true si le document est scanné à l'envers "
                          "(texte imprimé renversé, ligne magnétique en HAUT de l'image), "
                          "false s'il se lit normalement.\n"
                        + "Donne 'confiance' = haute si tout est net, moyenne si un "
                        "doute, basse si des caractères sont illisibles. Mets null "
                        "pour un champ introuvable."
                    ),
                },
            ],
        }],
        output_format=LectureCheque,
    )
    if response.stop_reason == "refusal":
        raise RuntimeError("lecture refusée par l'API")
    lecture = response.parsed_output
    if lecture is None:
        raise RuntimeError(f"réponse non structurée (stop_reason: {response.stop_reason})")
    return {
        "est_un_cheque": lecture.est_un_cheque,
        "ligne_cmc7": lecture.ligne_cmc7,
        "montant": lecture.montant,
        "montant_lettres": lecture.montant_lettres_en_euros,
        "banque": (lecture.banque or "") if lire_banque else "",
        "titulaire": (lecture.titulaire or "") if lire_titulaire else "",
        "numero_imprime": (lecture.numero_cheque_imprime or "") if lire_numero else "",
        "cle": "".join(c for c in (lecture.cle_controle or "") if c.isdigit())[:2],
        "tete_en_bas": bool(lecture.tete_en_bas),
        "confiance": lecture.confiance,
    }


def normaliser_zones(ligne: str | None) -> str | None:
    """Une 4e « zone » d'1-2 chiffres après trois zones 7/12/12 est la clé RLMC
    ou un glyphe recopié par erreur : on la retire plutôt que de rejeter la page."""
    if not ligne:
        return ligne
    zones = ligne.split()
    if len(zones) == 4 and [len(z) for z in zones[:3]] == [7, 12, 12] and len(zones[3]) <= 2:
        return " ".join(zones[:3])
    return ligne


def zones_conformes(ligne: str | None) -> bool:
    """Vrai si la ligne CMC7 a exactement 3 zones de 7/12/12 chiffres, sans '?'."""
    import re
    if not ligne or "?" in ligne:
        return False
    zones = [z for z in re.split(r"[^0-9]+", ligne.strip()) if z]
    return len(zones) == 3 and tuple(len(z) for z in zones) == (7, 12, 12)


def lire_bande_claude(png_bytes: bytes) -> str | None:
    """Seconde passe : relit uniquement la bande CMC7, croppée et agrandie ×2.

    Utilisée quand la lecture pleine page donne des zones anormales (zéros
    dédoublés, symboles pris pour des chiffres, zéro de tête perdu).
    """
    import base64

    import cv2
    import numpy as np

    import anthropic
    from pydantic import BaseModel

    bande = crop_bande_cmc7(png_bytes, fraction_bas=0.30)
    img = cv2.imdecode(np.frombuffer(bande, np.uint8), cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    img = cv2.resize(img, (img.shape[1] * 2, img.shape[0] * 2),
                     interpolation=cv2.INTER_CUBIC)
    ok, encode = cv2.imencode(".png", img)
    if not ok:
        return None

    class LectureBande(BaseModel):
        ligne_cmc7: str | None

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-5",
        max_tokens=6000,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": base64.standard_b64encode(encode.tobytes()).decode()}},
                {"type": "text",
                 "text": ("Voici la bande magnétique CMC7 d'un chèque français, agrandie. "
                          "Recopie-la en séparant les 3 zones par un espace. Structure "
                          "EXACTE : 7 chiffres, puis 12 chiffres, puis 12 chiffres. "
                          "Recopie fidèlement les zéros de tête. Les petits symboles "
                          "(⑈ ⑆ ⑉) en début, fin et entre les zones ne sont PAS des "
                          "chiffres — ne les transcris jamais. Compte tes chiffres avant "
                          "de répondre : zone 1 = 7, zones 2 et 3 = 12 chacune. "
                          "Si un chiffre est illisible mets '?'. Si ce n'est pas une "
                          "bande CMC7, mets null.")},
            ],
        }],
        output_format=LectureBande,
    )
    lecture = response.parsed_output
    return lecture.ligne_cmc7 if lecture else None


def relire_montant_claude(png_bytes: bytes) -> dict:
    """Seconde lecture indépendante du montant (chiffres ET lettres)."""
    import base64

    import anthropic
    from pydantic import BaseModel

    class LectureMontant(BaseModel):
        montant_chiffres: str | None
        montant_lettres_en_euros: float | None

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model="claude-opus-5",
        max_tokens=6000,
        output_config={"effort": "low"},
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png",
                            "data": base64.standard_b64encode(png_bytes).decode()}},
                {"type": "text",
                 "text": ("Sur ce chèque : 1. le montant en CHIFFRES (cadre à droite). "
                          "2. le montant EN LETTRES (lignes manuscrites du haut), "
                          "converti en nombre décimal d'euros. Ne te sers pas de l'un "
                          "pour corriger l'autre : lis-les indépendamment.")},
            ],
        }],
        output_format=LectureMontant,
    )
    lecture = response.parsed_output
    if lecture is None:
        raise RuntimeError("relecture montant non structurée")
    return {"chiffres": lecture.montant_chiffres,
            "lettres": lecture.montant_lettres_en_euros}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def extraire_cheque(fichier: Path, page: int, png_bytes: bytes,
                    options: dict | None = None) -> ExtractionCheque:
    """Extrait CMC7 + montant d'une page de scan (déjà redressée), Tesseract
    d'abord si possible."""
    if modele_cmc7_disponible():
        try:
            bande = crop_bande_cmc7(png_bytes)
            ligne = ocr_tesseract(bande)
            if ligne:
                # Tesseract ne lit pas le montant manuscrit : montant via Claude
                # uniquement si dispo, sinon laissé à la saisie manuelle.
                return ExtractionCheque(
                    fichier_source=str(fichier), page=page,
                    ligne_cmc7=ligne, montant=None,
                    confiance="moyenne", methode="tesseract",
                )
        except Exception as exc:  # OCR local KO -> fallback vision
            derniere_erreur = str(exc)
        else:
            derniere_erreur = "tesseract : sortie vide"
    else:
        derniere_erreur = "pas de modèle tesseract cmc7"

    try:
        lecture = lire_cheque_claude(png_bytes, options)
        lecture["ligne_cmc7"] = normaliser_zones(lecture.get("ligne_cmc7"))
        methode = "claude-vision"
        png_redresse = None
        # chèque scanné tête en bas non vu par l'OSD : la lecture renversée
        # mélange les chiffres (6↔9, ordre inversé) → on retourne et on relit
        if lecture.get("est_un_cheque", True) and lecture.get("tete_en_bas"):
            try:
                tourne = tourner_180(png_bytes)
                relecture = lire_cheque_claude(tourne, options)
                if relecture.get("est_un_cheque", True) and relecture.get("ligne_cmc7"):
                    lecture, png_bytes, png_redresse = relecture, tourne, tourne
                    methode = "claude-vision (retourné 180°)"
            except Exception:
                pass
        # clé RLMC contredite : dernier recours, la bande de l'image retournée
        if lecture.get("est_un_cheque", True) and png_redresse is None \
                and cle_coherente(lecture.get("ligne_cmc7"), lecture.get("cle")) is False:
            try:
                tourne = tourner_180(png_bytes)
                releve = lire_bande_claude(tourne)
            except Exception:
                releve = None
            if releve and cle_coherente(releve, lecture.get("cle")):
                lecture["ligne_cmc7"] = releve
                png_bytes, png_redresse = tourne, tourne
                methode = "claude-vision+zoom (retourné 180°)"
        # zones anormales sur un chèque → seconde chance sur la bande zoomée
        if lecture.get("est_un_cheque", True) and lecture.get("ligne_cmc7") \
                and not zones_conformes(lecture["ligne_cmc7"]):
            try:
                releve = lire_bande_claude(png_bytes)
            except Exception:
                releve = None
            if releve and zones_conformes(releve):
                lecture["ligne_cmc7"] = releve
                lecture["confiance"] = "moyenne"
                methode = "claude-vision+zoom"
        return ExtractionCheque(
            fichier_source=str(fichier), page=page,
            ligne_cmc7=lecture["ligne_cmc7"], montant=lecture["montant"],
            montant_lettres=lecture.get("montant_lettres"),
            confiance=lecture["confiance"], methode=methode,
            pas_un_cheque=not lecture.get("est_un_cheque", True),
            banque_nom=lecture.get("banque") or "",
            titulaire=lecture.get("titulaire") or "",
            numero_imprime=lecture.get("numero_imprime") or "",
            cle=lecture.get("cle") or "",
            png_redresse=png_redresse,
        )
    except Exception as exc:
        return ExtractionCheque(
            fichier_source=str(fichier), page=page,
            ligne_cmc7=None, montant=None, confiance="basse",
            methode="aucune", erreur=f"{derniere_erreur} ; claude-vision : {exc}",
        )
