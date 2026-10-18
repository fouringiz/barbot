"""Classification regression cases for the current classify module."""
import unittest
import os

os.environ.update(BOT_TOKEN="123456:offline-test", SHEET_ID="offline", GOOGLE_CREDS_JSON="{}")
from classify import classify


class ClassificationTests(unittest.TestCase):
    def test_existing_need_type_cases(self):
        cases = {
            "dolin bitter": "бар",
            "+ тостер (купи тостер я не могу норм завтракать дома)": "оборудование",
            "prosseco x3": "бар",
            "lesbonito (wine) x2": "бар",
            "PROSECCO x3": "бар",
            "нужен просеко": "бар",
            "Dolin vermouth": "бар",
            "bitters, orgeat, lime juice": "бар",
            "маракуйя и кокосовое молоко": "бар",
            "нужны бокалы для вина": "оборудование",
            "wine glasses x6": "оборудование",
            "сломался тостер": "поломка",
            "не работает coffee machine": "поломка",
            "coffee grinder": "оборудование",
            "туалетная бумага": "расходник",
            "office paper": "расходник",
            "салфетки и перчатки": "расходник",
            "sir piderson x1": "расходник",
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(classify(text), expected)


if __name__ == "__main__":
    unittest.main()
