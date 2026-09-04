import os
import unittest


class TestAuthBasique(unittest.TestCase):
    def setUp(self):
        os.environ["TLMC_UTILISATEUR"] = "testu"
        os.environ["TLMC_MOT_DE_PASSE"] = "testmdp"

    def test_sans_auth_401_avec_auth_200(self):
        from fastapi.testclient import TestClient
        import app as application
        client = TestClient(application.app)
        self.assertEqual(client.get("/").status_code, 401)
        self.assertEqual(client.get("/", auth=("testu", "mauvais")).status_code, 401)
        self.assertEqual(client.get("/", auth=("testu", "testmdp")).status_code, 200)


if __name__ == "__main__":
    unittest.main()
