# -*- coding: utf-8 -*-
"""odds_w5.py(5重勝の組の 3連単・馬単)の検算。通信なし。
実行: python -m unittest discover -s tests -p "test_*.py"
  ①一覧の頁から競馬の 5 レースの組だけ読む ②窓= 1 本目の発走 45〜25 分前だけ ③w5 車線は odds_w5.py だけを 600 秒ごと
  ④fetch_race の pages を渡さなければ今までどおり 6 ページ
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import odds_full                      # noqa: E402
import odds_w5                        # noqa: E402
from loop import lane_plan, odds_cmd  # noqa: E402

HTML = ('<h3><span class="ico-horse"></span>水沢</h3>\n<table class="tb70 w100pr">'
        + ''.join(f'<a href="x?joCode=12&amp;raceNo={n}">' for n in range(8, 13)) + '</table>'
        + '<h3><span class="ico-bike"></span>他</h3>\n<table class="tb70 w100pr">'
        + ''.join(f'<a href="x?joCode=99&amp;raceNo={n}">' for n in range(1, 6)) + '</table>')


class W5(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(odds_w5.parse_loto(HTML), [{"jo": "12", "races": [8, 9, 10, 11, 12]}])

    def test_window(self):
        cards = [{"track": "水沢", "races": [8, 9, 10, 11, 12]}]
        races = [{"track": "水沢", "race_no": n, "post_time": f"{14 + (n - 8) // 2:02d}{(n % 2) * 30:02d}"} for n in range(8, 13)]
        first = odds_full.post_minutes(races[0]["post_time"])
        for before, want in [(46, 0), (45, 1), (30, 1), (25, 1), (24, 0), (-5, 0)]:
            self.assertEqual(len(odds_w5.in_window(cards, races, first - before)), want, before)

    def test_lane(self):
        p = lane_plan("w5")
        self.assertEqual((p["every"], p["scripts"], p["extras"]), (600, ("odds_w5.py",), False))
        self.assertEqual(odds_cmd("odds_w5.py", p)[0], "odds_w5.py")
        self.assertEqual(lane_plan("morning")["scripts"], ("odds_tanfuku.py",))
        self.assertEqual(lane_plan("far")["scripts"], ("odds_tanfuku.py", "odds_full.py"))

    def test_pages_default(self):
        import inspect
        self.assertIsNone(inspect.signature(odds_full.fetch_race).parameters["pages"].default)
        self.assertEqual([p for p, _ in odds_w5.W5_PAGES], ["Odds3LenTan", "OddsUmLenTan"])


if __name__ == "__main__":
    unittest.main()
