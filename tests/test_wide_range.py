# -*- coding: utf-8 -*-
"""§245 ワイドの「下限<br>-上限」を両方持つ。標準ライブラリだけ・通信なし。

実行: python -m unittest discover -s tests -p "test_*.py"
確かめるのは 4 つ=
  ①公式のワイド人気順(2026-09-22 門別 12R の写し)から 組・下限・人気・上限 が取れる
  ②保存形は [a,b,下限,rank,上限]= 先頭 4 つは今までと同じ(古い読み手はそのまま読める)
  ③上限の無い券種(馬連など)は今までどおり [a,b,odds,rank](長さ 4)
  ④上限だけが変わっても指紋 h が変わる(積み表に残る)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from odds_full import combos_h, combos_json, has_odds, parse_ranking, tracks_summary    # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "odds_wide_monbetsu_20260922_12r.html"


class WideRangeTest(unittest.TestCase):
    def setUp(self):
        self.page = FIX.read_text(encoding="utf-8")
        self.rows, self.dropped = parse_ranking(self.page)

    def test_parse_has_low_and_high(self):
        self.assertEqual(self.dropped, 0)
        self.assertTrue(self.rows)
        first = self.rows[0]
        self.assertEqual(first[0], (2, 7))
        self.assertEqual(first[1], 1.3)
        self.assertEqual(first[2], 1)
        self.assertEqual(first[3], 1.6)
        for r in self.rows:
            self.assertEqual(len(r), 4)
            self.assertLessEqual(r[1], r[3])

    def test_json_shape_keeps_first_four(self):
        js = combos_json("wide", self.rows)
        self.assertEqual(js[0], [2, 7, 1.3, 1, 1.6])
        for row in js:
            self.assertEqual(len(row), 5)
        self.assertTrue(has_odds(self.rows))

    def test_other_kinds_unchanged(self):
        got = [((1, 2), 6.2, 1), ((3, 4), 12.5, 2)]
        self.assertEqual(combos_json("umaren", got), [[1, 2, 6.2, 1], [3, 4, 12.5, 2]])
        self.assertEqual(combos_json("umatan", [((1, 2), 6.2, 1, None)]), [[1, 2, 6.2, 1]])

    def test_high_changes_fingerprint(self):
        a = [((2, 7), 1.3, 1, 1.6)]
        b = [((2, 7), 1.3, 1, 1.7)]
        self.assertNotEqual(combos_h("wide", a), combos_h("wide", b))

    def test_tracks_summary(self):
        races = [{"track": "門別", "race_no": 1}, {"track": "門別", "race_no": 2}, {"track": "高知", "race_no": 1}]
        self.assertEqual(tracks_summary(races), "門別 2R・高知 1R")
        self.assertEqual(tracks_summary([]), "(開催なし)")


if __name__ == "__main__":
    unittest.main()
