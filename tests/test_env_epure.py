"""Épuration de l'environnement avant `claude -p`, et son exception.

Sur le Mac, une session Claude parente pollue l'environnement et court-circuite
l'auth Trousseau (401 « OAuth access token is invalid »). En conteneur il n'y a
pas de session parente, mais le jeton qui authentifie le CLI porte justement un
nom que la purge attrape : sans exception, tous les jobs échoueraient sur le
serveur.
"""

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import app.app as A  # noqa: E402


class TestEnvEpure(unittest.TestCase):
    def _epure(self, env, preserver=frozenset()):
        with mock.patch.dict(A.os.environ, env, clear=True), \
             mock.patch.object(A, "ENV_A_PRESERVER", set(preserver)):
            return A._env_epure()

    def test_variables_neutres_conservees(self):
        r = self._epure({"PATH": "/usr/bin", "HOME": "/root", "TZ": "UTC"})
        self.assertEqual(set(r), {"PATH", "HOME", "TZ"})

    def test_variables_polluantes_retirees(self):
        r = self._epure({
            "PATH": "/usr/bin",
            "CLAUDE_CODE_SSE_PORT": "1", "ANTHROPIC_API_KEY": "k",
            "BAGGAGE": "b", "AI_AGENT": "1", "SENTRY_DSN": "d",
        })
        self.assertEqual(set(r), {"PATH"})

    def test_jeton_preserve_explicitement(self):
        """Le cas du conteneur : le jeton doit survivre à la purge."""
        r = self._epure(
            {"PATH": "/usr/bin", "CLAUDE_CODE_OAUTH_TOKEN": "tok",
             "CLAUDE_CODE_SSE_PORT": "1"},
            preserver={"CLAUDE_CODE_OAUTH_TOKEN"})
        self.assertEqual(r.get("CLAUDE_CODE_OAUTH_TOKEN"), "tok")
        self.assertNotIn("CLAUDE_CODE_SSE_PORT", r)

    def test_preservation_est_un_nom_exact_pas_un_prefixe(self):
        r = self._epure(
            {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_API_KEY_OLD": "vieux"},
            preserver={"ANTHROPIC_API_KEY"})
        self.assertIn("ANTHROPIC_API_KEY", r)
        self.assertNotIn("ANTHROPIC_API_KEY_OLD", r)

    def test_sans_exception_le_jeton_disparait(self):
        """Comportement par défaut, celui qui vaut sur le poste de Djibril."""
        r = self._epure({"CLAUDE_CODE_OAUTH_TOKEN": "tok"})
        self.assertEqual(r, {})


if __name__ == "__main__":
    unittest.main()
