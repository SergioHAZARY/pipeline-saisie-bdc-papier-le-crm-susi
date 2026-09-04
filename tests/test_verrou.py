"""Verrou de jobs : il doit protéger un job réel, jamais survivre à sa mort.

Le runbook préconise « relancer le job » pour réparer un statut orphelin — mais
tant que le verrou reste pris, la relance est refusée et la réparation
impossible. C'est ce cercle vicieux que ces tests verrouillent.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.app as A  # noqa: E402


class FauxProc:
    """Imite le minimum de subprocess.Popen utilisé par le job runner."""

    def __init__(self, vivant=True):
        self._vivant = vivant

    def poll(self):
        return None if self._vivant else 1

    def mourir(self):
        self._vivant = False


class TestVerrouOrphelin(unittest.TestCase):
    def setUp(self):
        A._JOB_ACTIF = None
        A._PROC_ACTIF = None
        if A._VERROU.locked():
            try:
                A._VERROU.release()
            except RuntimeError:
                pass

    tearDown = setUp

    def test_verrou_libre_au_repos(self):
        self.assertFalse(A._job_vraiment_vivant())
        self.assertIsNone(A.liberer_verrou_orphelin())

    def test_job_vivant_n_est_pas_libere(self):
        A._VERROU.acquire()
        A._JOB_ACTIF = "LOT/extraction"
        A._PROC_ACTIF = FauxProc(vivant=True)
        self.assertTrue(A._job_vraiment_vivant())
        self.assertIsNone(A.liberer_verrou_orphelin())
        self.assertTrue(A._VERROU.locked())

    def test_processus_mort_libere_le_verrou(self):
        A._VERROU.acquire()
        A._JOB_ACTIF = "LOT/extraction"
        proc = FauxProc(vivant=True)
        A._PROC_ACTIF = proc
        proc.mourir()
        self.assertEqual(A.liberer_verrou_orphelin(), "LOT/extraction")
        self.assertFalse(A._VERROU.locked())
        self.assertIsNone(A._JOB_ACTIF)

    def test_verrou_pris_sans_processus_est_orphelin(self):
        """Cas réel : le serveur a redémarré, la référence au processus est perdue."""
        A._VERROU.acquire()
        A._JOB_ACTIF = "LOT/extraction"
        A._PROC_ACTIF = None
        self.assertEqual(A.liberer_verrou_orphelin(), "LOT/extraction")
        self.assertFalse(A._VERROU.locked())

    def test_liberation_idempotente(self):
        A._VERROU.acquire()
        A._JOB_ACTIF = "LOT/extraction"
        A._PROC_ACTIF = None
        A.liberer_verrou_orphelin()
        self.assertIsNone(A.liberer_verrou_orphelin())


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
