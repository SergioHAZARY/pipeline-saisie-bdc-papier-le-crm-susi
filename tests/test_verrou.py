"""Ordonnancement des jobs.

Le parallélisme se décide par ce que le job TOUCHE :

  - extraction   : lit des scans, écrit des fichiers, ne touche jamais SUSI
                   -> plusieurs lots en parallèle, dans une limite réglable
  - saisie       : écrit dans SUSI  } strictement exclusifs — SUSI rend des
  - verification : lit dans SUSI    } HTTP 500 au-delà de ~80-100 requêtes

Sérialiser l'extraction revenait à appliquer à tout le pipeline une contrainte
qui ne concerne que SUSI. Ces tests fixent la frontière.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.app as A  # noqa: E402


class FauxProc:
    """Imite le minimum de subprocess.Popen utilisé par l'ordonnanceur."""

    def __init__(self, vivant=True):
        self._vivant = vivant

    def poll(self):
        return None if self._vivant else 0

    def mourir(self):
        self._vivant = False


class BaseJobs(unittest.TestCase):
    def setUp(self):
        A._JOBS.clear()

    tearDown = setUp

    def _occuper(self, cle, vivant=True):
        p = FauxProc(vivant)
        A._JOBS[cle] = p
        return p


class TestExtractionParallele(BaseJobs):
    def test_rien_en_cours(self):
        self.assertIsNone(A._place_disponible("L1/extraction", "extraction"))

    def test_deux_lots_s_extraient_de_front(self):
        self._occuper("L1/extraction")
        self.assertIsNone(A._place_disponible("L2/extraction", "extraction"))

    def test_plafond_respecte(self):
        with mock.patch.object(A, "MAX_EXTRACTIONS", 2):
            self._occuper("L1/extraction")
            self._occuper("L2/extraction")
            refus = A._place_disponible("L3/extraction", "extraction")
            self.assertIsNotNone(refus)
            self.assertIn("plafond", refus)

    def test_sous_le_plafond_ca_passe(self):
        with mock.patch.object(A, "MAX_EXTRACTIONS", 3):
            self._occuper("L1/extraction")
            self._occuper("L2/extraction")
            self.assertIsNone(A._place_disponible("L3/extraction", "extraction"))

    def test_meme_lot_meme_genre_refuse(self):
        self._occuper("L1/extraction")
        refus = A._place_disponible("L1/extraction", "extraction")
        self.assertIn("ce job tourne déjà", refus)

    def test_un_job_susi_ne_bloque_pas_une_extraction(self):
        """La saisie occupe SUSI ; l'extraction ne l'approche pas."""
        self._occuper("L1/saisie")
        self.assertIsNone(A._place_disponible("L2/extraction", "extraction"))


class TestJobsSusiExclusifs(BaseJobs):
    def test_saisie_seule_passe(self):
        self.assertIsNone(A._place_disponible("L1/saisie", "saisie"))

    def test_deux_saisies_interdites(self):
        self._occuper("L1/saisie")
        refus = A._place_disponible("L2/saisie", "saisie")
        self.assertIn("SUSI ne supporte pas le parallélisme", refus)

    def test_verification_pendant_saisie_interdite(self):
        """Les deux touchent SUSI : exclusion mutuelle, pas seulement entre pairs."""
        self._occuper("L1/saisie")
        self.assertIsNotNone(A._place_disponible("L1/verification", "verification"))

    def test_saisie_pendant_extraction_autorisee(self):
        self._occuper("L1/extraction")
        self._occuper("L2/extraction")
        self.assertIsNone(A._place_disponible("L3/saisie", "saisie"))

    def test_extractions_au_plafond_n_empechent_pas_la_saisie(self):
        with mock.patch.object(A, "MAX_EXTRACTIONS", 1):
            self._occuper("L1/extraction")
            self.assertIsNone(A._place_disponible("L2/saisie", "saisie"))


class TestJobsFantomes(BaseJobs):
    def test_processus_mort_elague(self):
        p = self._occuper("L1/extraction")
        p.mourir()
        self.assertEqual(A._elaguer_jobs_morts(), ["L1/extraction"])
        self.assertEqual(A._JOBS, {})

    def test_place_reservee_sans_processus_est_elaguee(self):
        """Cas du redémarrage : l'entrée reste sans processus derrière."""
        A._JOBS["L1/extraction"] = None
        self.assertEqual(A._elaguer_jobs_morts(), ["L1/extraction"])

    def test_job_vivant_jamais_elague(self):
        self._occuper("L1/extraction")
        self.assertEqual(A._elaguer_jobs_morts(), [])
        self.assertIn("L1/extraction", A._JOBS)

    def test_fantome_ne_bloque_plus_apres_purge(self):
        """Le cercle vicieux à éviter : un fantôme interdisait la relance."""
        with mock.patch.object(A, "MAX_EXTRACTIONS", 1):
            p = self._occuper("L1/extraction")
            p.mourir()
            A.liberer_verrou_orphelin()
            self.assertIsNone(A._place_disponible("L2/extraction", "extraction"))

    def test_liberation_idempotente(self):
        A._JOBS["L1/extraction"] = None
        A.liberer_verrou_orphelin()
        self.assertEqual(A.liberer_verrou_orphelin(), [])

    def test_jobs_actifs_masque_les_morts(self):
        self._occuper("L1/extraction")
        self._occuper("L2/extraction", vivant=False)
        self.assertEqual(A.jobs_actifs(), ["L1/extraction"])


class TestReconciliationStatuts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.vieux_lots = A.LOTS
        A.LOTS = Path(self.tmp.name)

    def tearDown(self):
        A.LOTS = self.vieux_lots
        self.tmp.cleanup()

    def _statut(self, lot, genre, contenu):
        d = A.LOTS / lot / "jobs"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"{genre}.status.json"
        f.write_text(json.dumps(contenu), encoding="utf-8")
        return f

    def test_en_cours_devient_interrompu(self):
        f = self._statut("LOT_A", "extraction", {"statut": "en_cours", "demarre": "x"})
        self.assertEqual(A.reconcilier_statuts_au_demarrage(), ["LOT_A/extraction"])
        d = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(d["statut"], "interrompu")
        self.assertIn("relancer", d["note"])

    def test_termine_intact(self):
        f = self._statut("LOT_B", "extraction", {"statut": "termine", "code": 0})
        self.assertEqual(A.reconcilier_statuts_au_demarrage(), [])
        self.assertEqual(json.loads(f.read_text(encoding="utf-8"))["statut"], "termine")

    def test_plusieurs_lots(self):
        self._statut("LOT_A", "extraction", {"statut": "en_cours"})
        self._statut("LOT_B", "saisie", {"statut": "en_cours"})
        self._statut("LOT_C", "extraction", {"statut": "termine", "code": 0})
        self.assertEqual(sorted(A.reconcilier_statuts_au_demarrage()),
                         ["LOT_A/extraction", "LOT_B/saisie"])

    def test_statut_illisible_ignore_sans_planter(self):
        (A.LOTS / "LOT_X" / "jobs").mkdir(parents=True)
        (A.LOTS / "LOT_X" / "jobs" / "extraction.status.json").write_text("pas du json")
        self.assertEqual(A.reconcilier_statuts_au_demarrage(), [])


if __name__ == "__main__":
    unittest.main()
