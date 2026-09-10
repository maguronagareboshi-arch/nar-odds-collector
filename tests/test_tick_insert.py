# -*- coding: utf-8 -*-
"""nar_odds_ticks の insert が「同じレースの同じ分は 2 本目を捨てる」形で送られること。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import odds_tanfuku as ot  # noqa: E402


class TickInsert(unittest.TestCase):
    def test_ticks_ignore_duplicates(self):
        req = ot.insert_request("https://x.example", "k", ot.TICKS, [{"t": "15:58"}], conflict=ot.TICK_KEY)
        self.assertEqual(req.full_url, "https://x.example/rest/v1/nar_odds_ticks?on_conflict=track,race_date,race_no,t")
        self.assertEqual(req.get_header("Prefer"), "return=minimal,resolution=ignore-duplicates")

    def test_plain_insert_is_unchanged(self):
        req = ot.insert_request("https://x.example", "k", "other", [{"a": 1}])
        self.assertEqual(req.full_url, "https://x.example/rest/v1/other")
        self.assertEqual(req.get_header("Prefer"), "return=minimal")


if __name__ == "__main__":
    unittest.main()
