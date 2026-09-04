import unittest

from tlmc.ocr import cle_coherente, tourner_180


class TestOrientation(unittest.TestCase):
    def test_cle_coherente(self):
        # chèque réel p11 lot 02012026FR : clé 51
        self.assertTrue(cle_coherente("0339390 062016706908 003084086000", "51"))
        self.assertFalse(cle_coherente("0339390 906204706908 003084086000", "51"))
        self.assertIsNone(cle_coherente("0339390 9062047?6908 003084086000", "51"))
        self.assertIsNone(cle_coherente("0339390 062016706908 003084086000", ""))

    def test_tourner_180_involutif(self):
        import cv2
        import numpy as np
        img = np.zeros((20, 40, 3), np.uint8)
        img[0, 0] = (255, 255, 255)
        png = cv2.imencode(".png", img)[1].tobytes()
        tourne = cv2.imdecode(np.frombuffer(tourner_180(png), np.uint8), cv2.IMREAD_COLOR)
        self.assertEqual(tuple(tourne[19, 39]), (255, 255, 255))
        self.assertEqual(tuple(tourne[0, 0]), (0, 0, 0))


if __name__ == "__main__":
    unittest.main()


class TestNormalisation(unittest.TestCase):
    def test_quatrieme_zone_parasite(self):
        from tlmc.ocr import normaliser_zones
        self.assertEqual(normaliser_zones("2527184 038801000490 811520405285 6"),
                         "2527184 038801000490 811520405285")
        self.assertEqual(normaliser_zones("2527184 038801000490 811520405285"),
                         "2527184 038801000490 811520405285")
        self.assertEqual(normaliser_zones("2527184 0388 811520405285 6"), "2527184 0388 811520405285 6")
