"""Parsing des noms de lot — les variantes viennent du SFTP réel (/POUR_OUTSOURCIA/BDC)."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.app import nom_lot, parser_nom  # noqa: E402


class TestParserNom(unittest.TestCase):
    def test_forme_de_la_passation(self):
        m = parser_nom("28082026FR ATLAS III FID 50CH1.pdf")
        self.assertEqual(m["date"], "28082026")
        self.assertEqual(m["pays"], "FR")
        self.assertEqual(m["campagne"], "III")
        self.assertEqual(m["type"], "FID")
        self.assertEqual(m["nb_commandes"], 50)
        self.assertEqual(m["unite"], "CH")
        self.assertEqual(m["liasse"], "1")
        self.assertEqual(m["paiement"], "CH")
        self.assertTrue(m["avec_cheque"])

    def test_sans_campagne(self):
        """Le « III » n'est pas systématique : la production du 03/09 n'en a pas."""
        m = parser_nom("03092026FR ATLAS FID 50CH1.pdf")
        self.assertEqual(m["campagne"], "")
        self.assertEqual(m["type"], "FID")
        self.assertEqual(m["nb_commandes"], 50)

    def test_rec_normalise_en_recrut(self):
        self.assertEqual(parser_nom("03092026FR ATLAS REC 33CH.pdf")["type"], "RECRUT")
        self.assertEqual(parser_nom("28082026FR ATLAS III RECRUT 10CH.pdf")["type"], "RECRUT")

    def test_paiement_oa_sans_cheque(self):
        m = parser_nom("03092026FR ATLAS FID OA 49BDC.pdf")
        self.assertEqual(m["paiement"], "OA")
        self.assertEqual(m["unite"], "BDC")
        self.assertFalse(m["avec_cheque"])

    def test_paiement_cb(self):
        m = parser_nom("03092026FR ATLAS REC CB 17BDC.pdf")
        self.assertEqual(m["paiement"], "CB")
        self.assertFalse(m["avec_cheque"])

    def test_paiement_c3m(self):
        self.assertEqual(parser_nom("03092026FR ATLAS FID C3M 1BDC.pdf")["paiement"], "C3M")

    def test_sans_paiements_devient_oa(self):
        m = parser_nom("02092026FR ATLAS REC SANS PAIEMENTS 1BDC.pdf")
        self.assertEqual(m["paiement"], "OA")

    def test_compteur_sur_trois_chiffres(self):
        """« 50CH001 » et « 50BDC001 » existent à côté de « 50CH1 »."""
        self.assertEqual(parser_nom("02092026FR ATLAS REC 50CH001.pdf")["liasse"], "001")
        self.assertEqual(parser_nom("02092026FR ATLAS FID OA 50BDC001.pdf")["nb_commandes"], 50)

    def test_coquille_8dc(self):
        """« 8DC » pour « 8BDC » : coquille réellement présente en production."""
        m = parser_nom("03092026FR ATLAS REC CB 8DC.pdf")
        self.assertEqual(m["unite"], "BDC")
        self.assertEqual(m["nb_commandes"], 8)

    def test_bdc_sans_paiement_est_oa(self):
        """Une unité BDC sans mention de paiement = bons seuls, donc sans encaissement."""
        self.assertEqual(parser_nom("03092026FR ATLAS FID 12BDC.pdf")["paiement"], "OA")

    def test_underscores_acceptes(self):
        m = parser_nom("28082026FR_ATLAS_III_FID_50CH1.pdf")
        self.assertEqual(m["nb_commandes"], 50)
        self.assertEqual(m["type"], "FID")

    def test_sans_extension(self):
        self.assertIsNotNone(parser_nom("03092026FR ATLAS FID 27CH"))

    def test_non_conforme(self):
        for mauvais in ("facture.pdf", "ATLAS FID 50CH1.pdf", "", "20260903 ATLAS FID 50CH1.pdf"):
            self.assertIsNone(parser_nom(mauvais), mauvais)


class TestNomLot(unittest.TestCase):
    def test_normalisation(self):
        self.assertEqual(nom_lot("03092026FR ATLAS FID 50CH1.pdf"),
                         "03092026FR_ATLAS_FID_50CH1")

    def test_sans_paiements_normalise(self):
        self.assertEqual(nom_lot("02092026FR ATLAS REC SANS PAIEMENTS 1BDC.pdf"),
                         "02092026FR_ATLAS_REC_SANS_PAIEMENTS_1BDC")

    def test_idempotent(self):
        once = nom_lot("03092026FR ATLAS FID 50CH1.pdf")
        self.assertEqual(nom_lot(once), once)


if __name__ == "__main__":
    unittest.main()
