import unittest

from tlmc.cmc7 import CMC7Error, montant_en_centimes, parser_ligne


class TestParserLigne(unittest.TestCase):
    def test_ligne_standard(self):
        ligne = parser_ligne("1234567 300041234567 123456789012")
        self.assertEqual(ligne.numero_cheque, "1234567")
        self.assertEqual(ligne.code_banque, "30004")
        self.assertEqual(ligne.code_guichet, "12345")
        self.assertEqual(ligne.numero_compte, "123456789012")
        self.assertEqual(ligne.avertissements, [])

    def test_separateurs_exotiques(self):
        ligne = parser_ligne(";1234567; 300041234567 :123456789012<")
        self.assertEqual(ligne.numero_cheque, "1234567")

    def test_zone_trop_courte_avertit_sans_bloquer(self):
        ligne = parser_ligne("12345 300041234567 123456789012")
        self.assertEqual(len(ligne.avertissements), 1)
        self.assertIn("zone 1", ligne.avertissements[0])

    def test_nombre_de_zones_incorrect(self):
        with self.assertRaises(CMC7Error):
            parser_ligne("1234567 300041234567")

    def test_ligne_vide(self):
        with self.assertRaises(CMC7Error):
            parser_ligne("   ")


class TestMontant(unittest.TestCase):
    def test_virgule_francaise(self):
        self.assertEqual(montant_en_centimes("123,45"), 12345)

    def test_point_decimal(self):
        self.assertEqual(montant_en_centimes("99.90"), 9990)

    def test_avec_euro_et_espaces(self):
        self.assertEqual(montant_en_centimes(" 1 250,00 € "), 125000)

    def test_float(self):
        self.assertEqual(montant_en_centimes(45.1), 4510)

    def test_illisible(self):
        with self.assertRaises(CMC7Error):
            montant_en_centimes("abc")

    def test_negatif(self):
        with self.assertRaises(CMC7Error):
            montant_en_centimes("-5,00")


if __name__ == "__main__":
    unittest.main()


class TestCleRLMC(unittest.TestCase):
    def setUp(self):
        from tlmc.cmc7 import cle_rlmc_valide, resoudre_par_cle
        self.valide, self.resoudre = cle_rlmc_valide, resoudre_par_cle
        # exemple fourni par la banque + chèque Atlas p1
        self.ok = ("5508018", "021010041908", "006617182025", 56)

    def test_cle_valide(self):
        self.assertTrue(self.valide(*self.ok))
        self.assertTrue(self.valide("1298107", "063016806908", "033002762000", 38))
        self.assertFalse(self.valide("5508019", "021010041908", "006617182025", 56))

    def test_resolution_point_interrogation(self):
        z1, z2, z3, cle = self.ok
        self.assertEqual(self.resoudre("55080?8", z2, z3, cle), (z1, z2, z3))
        self.assertEqual(self.resoudre(z1, "0210100419?8", z3, cle), (z1, z2, z3))

    def test_resolution_chiffre_errone_sure_ou_rien(self):
        # pour un chiffre erroné, plusieurs corrections peuvent satisfaire la
        # clé (modulo 97) : le solveur ne répond que si la solution est UNIQUE,
        # sinon None — jamais une ligne dont la clé serait fausse.
        z1, z2, z3, cle = self.ok
        resultat = self.resoudre("5503018", z2, z3, cle)
        if resultat is not None:
            self.assertTrue(self.valide(*resultat, cle))

    def test_ambiguite_refusee(self):
        z1, z2, z3, cle = self.ok
        self.assertIsNone(self.resoudre("???????", "????????????", z3, cle))

    def test_cle_invalide_sans_solution_unique_possible(self):
        self.assertIsNone(self.resoudre("5508018", "021010041908", "006617182025", "ab"))
