import unittest
from unittest.mock import patch

from jarvis import i18n


class GlossaryOrderCacheTests(unittest.TestCase):
    def setUp(self):
        i18n._ordered_glossary.cache_clear()

    def test_longest_first_and_stable_ties(self):
        entries = (("ab", "first"), ("longer", "long"), ("cd", "second"))
        self.assertEqual(i18n._ordered_glossary(entries), (entries[1], entries[0], entries[2]))
        self.assertIs(i18n._ordered_glossary(entries), i18n._ordered_glossary(entries))

    def test_translation_observes_in_place_changes_and_replacement(self):
        glossary = {"unique-cache-token": "first"}
        with patch.dict(i18n.GLOSSARY, {"en": glossary}):
            self.assertEqual(i18n.tr("unique-cache-token", "en"), "first")
            glossary["unique-cache-token"] = "second"
            self.assertEqual(i18n.tr("unique-cache-token", "en"), "second")
            i18n.GLOSSARY["en"] = {"unique-cache-token": "third"}
            self.assertEqual(i18n.tr("unique-cache-token", "en"), "third")
            i18n.GLOSSARY["en"].clear()
            self.assertEqual(i18n.tr("unique-cache-token", "en"), "unique-cache-token")

    def test_cache_is_bounded_after_many_dictionary_revisions(self):
        for index in range(100):
            i18n._ordered_glossary((("phrase", str(index)),))
        self.assertLessEqual(i18n._ordered_glossary.cache_info().currsize, 14)


if __name__ == "__main__":
    unittest.main()
