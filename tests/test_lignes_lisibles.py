"""Parsing du flux stream-json de `claude -p` en lignes affichables."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.app import _lignes_lisibles  # noqa: E402


def evt(**kw) -> str:
    return json.dumps(kw)


class TestLignesLisibles(unittest.TestCase):
    def test_vide(self):
        self.assertEqual(_lignes_lisibles(""), [])

    def test_init_de_session(self):
        lignes = _lignes_lisibles(evt(type="system", subtype="init",
                                      session_id="abcdef1234567890",
                                      tools=["Read", "Bash", "Write"]))
        self.assertEqual(len(lignes), 1)
        self.assertIn("abcdef12", lignes[0])
        self.assertIn("3 outils", lignes[0])

    def test_texte_assistant(self):
        lignes = _lignes_lisibles(evt(
            type="assistant",
            message={"content": [{"type": "text", "text": "Lecture du lot\n\nDeux commandes"}]}))
        self.assertEqual(lignes, ["  Lecture du lot", "  Deux commandes"])

    def test_sous_agent(self):
        lignes = _lignes_lisibles(evt(
            type="assistant",
            message={"content": [{"type": "tool_use", "name": "Task",
                                  "input": {"subagent_type": "bdc-lecteur",
                                            "description": "commandes 1 à 5"}}]}))
        self.assertIn("bdc-lecteur", lignes[0])
        self.assertIn("commandes 1 à 5", lignes[0])

    def test_bash_tronque(self):
        lignes = _lignes_lisibles(evt(
            type="assistant",
            message={"content": [{"type": "tool_use", "name": "Bash",
                                  "input": {"command": "x" * 200}}]}))
        self.assertTrue(lignes[0].startswith("→ bash :"))
        self.assertLessEqual(len(lignes[0]), 100)

    def test_write_montre_le_chemin(self):
        lignes = _lignes_lisibles(evt(
            type="assistant",
            message={"content": [{"type": "tool_use", "name": "Write",
                                  "input": {"file_path": "extraits/cmd_01.json"}}]}))
        self.assertIn("extraits/cmd_01.json", lignes[0])

    def test_erreur_outil_signalee(self):
        lignes = _lignes_lisibles(evt(
            type="user",
            message={"content": [{"type": "tool_result", "is_error": True,
                                  "content": "boom"}]}))
        self.assertEqual(lignes, ["  ! erreur d'outil"])

    def test_resultat_final(self):
        lignes = _lignes_lisibles(evt(type="result", subtype="success",
                                      duration_ms=964000, total_cost_usd=0.4231))
        self.assertIn("terminé", lignes[0])
        self.assertIn("964 s", lignes[0])
        self.assertIn("0.4231", lignes[0])

    def test_ligne_non_json_conservee(self):
        """Un message d'erreur du CLI n'est pas du JSON : il doit rester visible."""
        lignes = _lignes_lisibles("Invalid MCP configuration")
        self.assertEqual(lignes, ["Invalid MCP configuration"])

    def test_json_tronque_conserve(self):
        lignes = _lignes_lisibles('{"type": "assistant", "message":')
        self.assertEqual(len(lignes), 1)

    def test_flux_complet_dans_l_ordre(self):
        flux = "\n".join([
            evt(type="system", subtype="init", session_id="s" * 16, tools=[]),
            evt(type="assistant", message={"content": [
                {"type": "tool_use", "name": "Task",
                 "input": {"subagent_type": "bdc-lecteur", "description": "cmd 1-5"}}]}),
            evt(type="assistant", message={"content": [{"type": "text", "text": "Consolidation"}]}),
            evt(type="result", subtype="success", duration_ms=1000),
        ])
        lignes = _lignes_lisibles(flux)
        self.assertEqual(len(lignes), 4)
        self.assertIn("session", lignes[0])
        self.assertIn("bdc-lecteur", lignes[1])
        self.assertIn("Consolidation", lignes[2])
        self.assertIn("terminé", lignes[3])

    def test_lignes_blanches_ignorees(self):
        self.assertEqual(_lignes_lisibles("\n\n  \n"), [])


if __name__ == "__main__":
    unittest.main()
