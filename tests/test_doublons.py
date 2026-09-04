"""Détection déterministe des doublons de scan (chantier n° 3 de la passation).

Le cas de référence est la cmd 26 du lot réel 50CH1 : un double passage du
scanner, même chèque CMC7 et même BDC que la paire voisine. Si les deux étaient
saisies, cela ferait un encaissement en trop.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.app import detecter_doublons  # noqa: E402


def cmd(cid, cmc7=None, client=None, total=50.0):
    return {"_id": cid, "id": cid, "total": total,
            "cheque": {"cmc7": cmc7} if cmc7 else {},
            "client": {"numero": client} if client else {}}


CMC7_A = "0003895 076030003908 103253602372"
CMC7_B = "0000399 075004505908 204189830032"


class TestDoublonsCMC7(unittest.TestCase):
    def test_lot_sain(self):
        self.assertEqual(detecter_doublons([
            cmd("01", CMC7_A, "0010060274"),
            cmd("02", CMC7_B, "0011078248"),
        ]), {})

    def test_meme_cmc7_signale_les_deux(self):
        d = detecter_doublons([
            cmd("25", CMC7_A, "0010060274"),
            cmd("26", CMC7_A, "0010060274"),
        ])
        self.assertEqual(set(d), {"25", "26"})
        for cid in ("25", "26"):
            self.assertTrue(any("DOUBLON DE SCAN" in m for m in d[cid]))

    def test_le_motif_nomme_l_autre_commande(self):
        d = detecter_doublons([cmd("25", CMC7_A), cmd("26", CMC7_A)])
        self.assertIn("#26", d["25"][0])
        self.assertIn("#25", d["26"][0])

    def test_separateurs_ignores(self):
        """Les glyphes CMC7 ⑈⑆⑉ et les espaces ne doivent pas masquer un doublon."""
        d = detecter_doublons([
            cmd("01", "0003895 076030003908 103253602372"),
            cmd("02", "⑈0003895⑆076030003908⑉103253602372"),
        ])
        self.assertEqual(set(d), {"01", "02"})

    def test_triplon(self):
        d = detecter_doublons([cmd(c, CMC7_A) for c in ("01", "02", "03")])
        self.assertEqual(set(d), {"01", "02", "03"})
        self.assertIn("#02", d["01"][0])
        self.assertIn("#03", d["01"][0])

    def test_cmc7_trop_courte_ignoree(self):
        """Une CMC7 illisible réduite à quelques chiffres ne doit pas créer de
        faux doublons entre toutes les commandes mal lues."""
        self.assertEqual(detecter_doublons([
            cmd("01", "0003895"), cmd("02", "0003895"),
        ]), {})

    def test_cheque_absent_lot_oa(self):
        self.assertEqual(detecter_doublons([cmd("01"), cmd("02")]), {})


class TestDoublonsClient(unittest.TestCase):
    def test_meme_client_est_une_suspicion_pas_une_certitude(self):
        d = detecter_doublons([
            cmd("01", CMC7_A, "0010060274"),
            cmd("02", CMC7_B, "0010060274"),
        ])
        self.assertEqual(set(d), {"01", "02"})
        for cid in ("01", "02"):
            self.assertFalse(any("DOUBLON DE SCAN" in m for m in d[cid]))
            self.assertTrue(any("à confirmer" in m for m in d[cid]))

    def test_zeros_de_tete_normalises(self):
        d = detecter_doublons([
            cmd("01", CMC7_A, "0010060274"),
            cmd("02", CMC7_B, "10060274"),
        ])
        self.assertEqual(set(d), {"01", "02"})

    def test_cmc7_ne_repete_pas_l_alerte_client(self):
        """Quand la CMC7 a déjà établi le doublon, ne pas empiler un second motif."""
        d = detecter_doublons([
            cmd("25", CMC7_A, "0010060274"),
            cmd("26", CMC7_A, "0010060274"),
        ])
        self.assertEqual(len(d["25"]), 1)
        self.assertIn("DOUBLON DE SCAN", d["25"][0])

    def test_client_absent(self):
        self.assertEqual(detecter_doublons([
            cmd("01", CMC7_A), cmd("02", CMC7_B),
        ]), {})


if __name__ == "__main__":
    unittest.main()
