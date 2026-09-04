"""tlmc-remise — génération de fichiers de remise TLMC (CFONB) à partir de chèques scannés."""

__version__ = "0.1.0"


def charger_env() -> None:
    """Charge .env à la racine du projet dans os.environ (sans écraser)."""
    import os
    from pathlib import Path

    env = Path(__file__).parent.parent / ".env"
    if not env.exists():
        return
    for ligne in env.read_text().splitlines():
        ligne = ligne.strip()
        if ligne and not ligne.startswith("#") and "=" in ligne:
            cle, _, valeur = ligne.partition("=")
            os.environ.setdefault(cle.strip(), valeur.strip().strip('"'))
