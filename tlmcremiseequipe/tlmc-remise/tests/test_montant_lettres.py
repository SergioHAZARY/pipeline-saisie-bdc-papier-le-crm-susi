import unittest

from tlmc.pipeline import arbitrer_montant


class TestMontantLettres(unittest.TestCase):
    def test_desaccord_lettres_font_foi(self):
        centimes, note = arbitrer_montant(8089, 20.89)   # cas REBOUL : chiffres 80,89, lettres vingt euros 89
        self.assertEqual(centimes, 2089)
        self.assertIn("EN LETTRES retenu", note)

    def test_accord_pas_de_note(self):
        self.assertEqual(arbitrer_montant(2089, 20.89), (2089, None))

    def test_lettres_absentes(self):
        self.assertEqual(arbitrer_montant(8089, None), (8089, None))

    def test_lettres_invalides(self):
        self.assertEqual(arbitrer_montant(8089, -3.0), (8089, None))
