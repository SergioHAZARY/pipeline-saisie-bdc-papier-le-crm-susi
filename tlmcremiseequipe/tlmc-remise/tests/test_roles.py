import os
import unittest


class TestRoles(unittest.TestCase):
    def setUp(self):
        os.environ["TLMC_UTILISATEURS"] = (
            "chef:mdpadmin:admin,edit1:mdpedit:editeur,vue1:mdpvue:viewer")
        os.environ.pop("TLMC_MOT_DE_PASSE", None)
        from fastapi.testclient import TestClient
        import app as application
        self.client = TestClient(application.app)

    def test_viewer_lecture_seule(self):
        self.assertEqual(self.client.get("/", auth=("vue1", "mdpvue")).status_code, 200)
        self.assertEqual(
            self.client.post("/traiter/drive", data={"lien": "x"},
                             auth=("vue1", "mdpvue")).status_code, 403)

    def test_editeur_sans_correction(self):
        self.assertEqual(
            self.client.post("/lot/x/maj", data={}, auth=("edit1", "mdpedit"),
                             follow_redirects=False).status_code, 404)  # passe l'auth
        self.assertEqual(
            self.client.post("/lot/x/autocorriger", auth=("edit1", "mdpedit")).status_code, 403)
        self.assertEqual(
            self.client.post("/lot/x/isole/1/maj", data={}, auth=("edit1", "mdpedit")).status_code, 403)

    def test_admin_tout(self):
        self.assertEqual(
            self.client.post("/lot/x/autocorriger", auth=("chef", "mdpadmin"),
                             follow_redirects=False).status_code, 303)

    def test_mauvais_mdp(self):
        self.assertEqual(self.client.get("/", auth=("vue1", "faux")).status_code, 401)


if __name__ == "__main__":
    unittest.main()
