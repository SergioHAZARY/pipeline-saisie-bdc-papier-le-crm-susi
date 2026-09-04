import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from tlmc.cmc7 import parser_ligne
from tlmc.writer import (
    ChequeValide, SpecManquante, champ_alpha, champ_numerique,
    ecrire_remise, total_remise,
)


def cheque(numero="1234567", centimes=12345):
    return ChequeValide(
        cmc7=parser_ligne(f"{numero} 300041234567 123456789012"),
        montant_centimes=centimes,
        fichier_source="test.pdf",
    )


class TestChamps(unittest.TestCase):
    def test_numerique_padding_zeros(self):
        self.assertEqual(champ_numerique(42, 6), "000042")

    def test_numerique_depassement(self):
        with self.assertRaises(ValueError):
            champ_numerique(1234567, 3)

    def test_numerique_refuse_non_chiffres(self):
        with self.assertRaises(ValueError):
            champ_numerique("12A4", 6)

    def test_alpha_padding_espaces(self):
        self.assertEqual(champ_alpha("AB", 5), "AB   ")


class TestTotal(unittest.TestCase):
    def test_total_recalcule(self):
        self.assertEqual(total_remise([cheque(centimes=100), cheque(centimes=250)]), 350)

    def test_total_vide(self):
        self.assertEqual(total_remise([]), 0)


class TestEcriture(unittest.TestCase):
    LAYOUT_FACTICE = {
        "longueur_enregistrement": 40,
        "fin_de_ligne": "\r\n",
        "enregistrements": {
            "entete": [
                {"nom": "code", "type": "an", "longueur": 2, "valeur": "01"},
                {"nom": "date", "type": "n", "longueur": 6, "source": "date_remise"},
                {"nom": "filler", "type": "an", "longueur": 32, "valeur": ""},
            ],
            "detail": [
                {"nom": "code", "type": "an", "longueur": 2, "valeur": "02"},
                {"nom": "cheque", "type": "n", "longueur": 7, "source": "numero_cheque"},
                {"nom": "interbancaire", "type": "n", "longueur": 12, "source": "zone_interbancaire"},
                {"nom": "montant", "type": "n", "longueur": 12, "source": "montant_centimes"},
                {"nom": "filler", "type": "an", "longueur": 7, "valeur": ""},
            ],
            "total": [
                {"nom": "code", "type": "an", "longueur": 2, "valeur": "08"},
                {"nom": "nombre", "type": "n", "longueur": 6, "source": "nombre_cheques"},
                {"nom": "total", "type": "n", "longueur": 12, "source": "total_centimes"},
                {"nom": "filler", "type": "an", "longueur": 20, "valeur": ""},
            ],
        },
    }

    def test_spec_manquante(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SpecManquante):
                ecrire_remise([cheque()], Path(tmp) / "out.tlmc", Path(tmp))

    def test_ecriture_layout_factice(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = Path(tmp)
            (spec / "layout.json").write_text(json.dumps(self.LAYOUT_FACTICE))
            sortie = ecrire_remise(
                [cheque(centimes=100), cheque(numero="7654321", centimes=250)],
                spec / "out.tlmc", spec, date_remise=date(2026, 8, 18),
            )
            brut = sortie.read_bytes()
            self.assertFalse(brut.endswith(b"\r\n"))  # pas de terminateur final
            lignes = brut.decode("ascii").split("\r\n")
            self.assertEqual(len(lignes), 4)  # entête + 2 détails + total
            for ligne in lignes:
                self.assertEqual(len(ligne), 40)
            self.assertTrue(lignes[0].startswith("01180826"))
            self.assertIn("000000000100", lignes[1])   # montant zero-padded
            self.assertTrue(lignes[3].startswith("08000002000000000350"))  # total recalculé


if __name__ == "__main__":
    unittest.main()


class TestLayoutBRED(unittest.TestCase):
    """Valide la génération contre la structure du fichier réel OUTSOURCIA/BRED."""

    def test_generation_conforme_au_fichier_exemple(self):
        from datetime import date as d
        spec = Path(__file__).parent.parent / "spec"
        with tempfile.TemporaryDirectory() as tmp:
            sortie = ecrire_remise(
                [cheque(numero="4675026", centimes=5988),
                 cheque(numero="9191932", centimes=7688)],
                Path(tmp) / "remise.tlmc", spec,
                date_remise=d(2026, 8, 18),
                surcharges={"numero_remise": "005277"},
            )
            brut = sortie.read_bytes()
            self.assertNotIn(b"\n", brut)          # CR seul, comme le fichier BRED
            lignes = [l for l in brut.split(b"\r") if l]
            self.assertEqual(len(lignes), 4)        # 03 + 2×04 + 08
            for l in lignes:
                self.assertEqual(len(l), 320)
            e03, d1, _, e08 = (l.decode("ascii") for l in lignes)
            # offsets observés dans le fichier réel
            self.assertEqual(e03[0:10], "03CH000001")
            self.assertEqual(e03[10:18], "20260818")
            self.assertEqual(e03[128:134], "005277")
            self.assertEqual(e03[169:172], "EUR")
            self.assertEqual(d1[0:10], "04CH000002")
            self.assertEqual(d1[19:26], "4675026")
            self.assertEqual(d1[26:38], "300041234567")   # z2 du chèque factice
            self.assertEqual(d1[117:129], "000000005988")
            self.assertEqual(d1[130], "0")
            self.assertEqual(e08[0:10], "08CH000004")      # nb enregistrements
            self.assertEqual(e08[172:184], "000000013676") # 59,88 + 76,88
            self.assertEqual(e08[184:190], "000002")

    def test_reference_7_chiffres_comme_serie_000xxx(self):
        from datetime import date as d
        spec = Path(__file__).parent.parent / "spec"
        with tempfile.TemporaryDirectory() as tmp:
            sortie = ecrire_remise(
                [cheque(numero="6129844", centimes=5287)],
                Path(tmp) / "r.tlmc", spec, date_remise=d(2026, 8, 14),
                surcharges={"numero_remise": "000670", "reference_base": 2727751},
            )
            d1 = sortie.read_bytes().split(b"\r")[1].decode()
            # référence n24 cadrée droite, comme dans tlmc_000670 réel
            self.assertEqual(d1[93:117], "000000000000000002727752")
            self.assertEqual(d1[117:129], "000000005287")
