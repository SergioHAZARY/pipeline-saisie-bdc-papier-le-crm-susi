"""Score de confiance pré-saisie — barème de la passation du 03/09/2026."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.app import classe_score, score_extraction  # noqa: E402


def cmd(**kw) -> dict:
    """Commande cohérente : 1 × 29.99 + 5.90 = 35.89, chèque au même montant."""
    base = {
        "articles": [{"refco": "K8179", "quantite": 1, "prix_unitaire": 29.99,
                      "confiance": "haute"}],
        "frais_port": 5.90,
        "total": 35.89,
        "cheque": {"montant": 35.89},
        "anomalies": [],
    }
    base.update(kw)
    return base


class TestScoreExtraction(unittest.TestCase):
    def test_commande_parfaite(self):
        score, motifs = score_extraction(cmd())
        self.assertEqual(score, 100)
        self.assertEqual(motifs, [])

    def test_arithmetique_fausse(self):
        score, motifs = score_extraction(cmd(total=99.00, cheque={"montant": 99.00}))
        self.assertEqual(score, 60)
        self.assertTrue(any("arithmétique" in m for m in motifs))

    def test_cheque_different_du_total(self):
        score, _ = score_extraction(cmd(cheque={"montant": 40.00}))
        self.assertEqual(score, 60)

    def test_arithmetique_et_cheque_cumulent(self):
        """Deux retenues de 40 se cumulent : 100 − 40 − 40 = 20."""
        score, _ = score_extraction(cmd(total=99.00, cheque={"montant": 35.89}))
        self.assertEqual(score, 20)

    def test_refco_manquant(self):
        score, motifs = score_extraction(cmd(
            articles=[{"refco": None, "quantite": 1, "prix_unitaire": 29.99,
                       "confiance": "haute"}]))
        self.assertEqual(score, 75)
        self.assertTrue(any("refco" in m for m in motifs))

    def test_refco_avec_point_interrogation(self):
        score, _ = score_extraction(cmd(
            articles=[{"refco": "K81?9", "quantite": 1, "prix_unitaire": 29.99,
                       "confiance": "haute"}]))
        self.assertEqual(score, 75)

    def test_confiance_basse(self):
        score, _ = score_extraction(cmd(
            articles=[{"refco": "K8179", "quantite": 1, "prix_unitaire": 29.99,
                       "confiance": "basse"}]))
        self.assertEqual(score, 80)

    def test_confiance_moyenne(self):
        score, _ = score_extraction(cmd(
            articles=[{"refco": "K8179", "quantite": 1, "prix_unitaire": 29.99,
                       "confiance": "moyenne"}]))
        self.assertEqual(score, 90)

    def test_confiance_par_article_cumule(self):
        score, _ = score_extraction(cmd(
            articles=[
                {"refco": "A1", "quantite": 1, "prix_unitaire": 15.00, "confiance": "basse"},
                {"refco": "B2", "quantite": 1, "prix_unitaire": 14.99, "confiance": "moyenne"},
            ]))
        self.assertEqual(score, 70)   # 100 − 20 − 10

    def test_anomalies_plafonnees_a_20(self):
        score, motifs = score_extraction(cmd(anomalies=["a", "b", "c", "d", "e", "f"]))
        self.assertEqual(score, 80)   # 6 × 5 = 30, plafonné à 20
        self.assertTrue(any("plafond" in m for m in motifs))

    def test_anomalies_sous_le_plafond(self):
        self.assertEqual(score_extraction(cmd(anomalies=["a", "b"]))[0], 90)

    def test_jamais_negatif(self):
        score, _ = score_extraction(cmd(
            total=99.00, cheque={"montant": 1.00},
            articles=[{"refco": None, "quantite": 1, "prix_unitaire": 5.0,
                       "confiance": "basse"}],
            anomalies=["a", "b", "c", "d", "e"]))
        self.assertEqual(score, 0)

    def test_total_absent_pas_de_retenue_arithmetique(self):
        """Sans total lu, on ne peut rien conclure : pas de retenue arbitraire."""
        score, motifs = score_extraction(cmd(total=None, cheque={}))
        self.assertEqual(score, 100)
        self.assertEqual(motifs, [])

    def test_prix_illisible_pas_de_retenue_arithmetique(self):
        score, _ = score_extraction(cmd(
            articles=[{"refco": "K8179", "quantite": 1, "prix_unitaire": None,
                       "confiance": "haute"}]))
        self.assertEqual(score, 100)

    def test_sans_cheque_lot_oa(self):
        """Un lot OA n'a pas de chèque : aucune retenue de ce chef."""
        score, _ = score_extraction(cmd(cheque={}, paiement="OA"))
        self.assertEqual(score, 100)

    def test_tolerance_au_centime(self):
        score, _ = score_extraction(cmd(total=35.89, cheque={"montant": 35.894}))
        self.assertEqual(score, 100)


class TestClasseScore(unittest.TestCase):
    def test_seuils(self):
        self.assertEqual(classe_score(100), "ok")
        self.assertEqual(classe_score(90), "ok")
        self.assertEqual(classe_score(89), "tiede")
        self.assertEqual(classe_score(70), "tiede")
        self.assertEqual(classe_score(69), "rouge")
        self.assertEqual(classe_score(0), "rouge")


if __name__ == "__main__":
    unittest.main()
