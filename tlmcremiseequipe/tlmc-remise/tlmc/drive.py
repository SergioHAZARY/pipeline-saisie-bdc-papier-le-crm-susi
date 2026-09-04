"""Téléchargement de scans depuis Google Drive via gdown.

Fonctionne avec les fichiers/dossiers partagés « toute personne disposant du
lien » — pas de service account ni d'OAuth. Liens acceptés :
  - https://drive.google.com/file/d/<id>/view
  - https://drive.google.com/drive/folders/<id>
  - https://drive.google.com/open?id=<id>
"""

from __future__ import annotations

from pathlib import Path


class DriveError(RuntimeError):
    pass


def telecharger_drive(lien: str, destination: Path) -> Path:
    """Télécharge un fichier ou un dossier Drive dans `destination`.

    Retourne le dossier contenant les fichiers téléchargés.
    """
    import gdown

    lien = lien.strip()
    if "drive.google.com" not in lien:
        raise DriveError("lien Google Drive attendu (drive.google.com/…)")
    destination.mkdir(parents=True, exist_ok=True)

    try:
        if "/folders/" in lien:
            fichiers = gdown.download_folder(url=lien, output=str(destination), quiet=True)
            if not fichiers:
                raise DriveError("dossier Drive vide ou non accessible")
        else:
            chemin = gdown.download(url=lien, output=str(destination) + "/", quiet=True, fuzzy=True)
            if not chemin:
                raise DriveError("fichier Drive non accessible")
    except DriveError:
        raise
    except Exception as exc:
        raise DriveError(
            f"téléchargement Drive impossible ({exc}). Vérifier que le partage "
            "est en « toute personne disposant du lien »."
        ) from exc
    return destination
