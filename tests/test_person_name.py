# -*- coding: utf-8 -*-
"""2026-10-03 監査: 騎手・調教師の「1 字+半角空白+1 字」を全角空白に戻す。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from rakuten_parse import person  # noqa: E402


class Person(unittest.TestCase):
    def test_two_chars(self):
        self.assertEqual(person("林 豊"), "林　豊")

    def test_long_name_kept(self):
        self.assertEqual(person("佐藤 太郎"), "佐藤 太郎")

    def test_already_zenkaku(self):
        self.assertEqual(person("林　豊"), "林　豊")

    def test_empty(self):
        self.assertEqual(person(""), "")
        self.assertIsNone(person(None))


if __name__ == "__main__":
    unittest.main()
