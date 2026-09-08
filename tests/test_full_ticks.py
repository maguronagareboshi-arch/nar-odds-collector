# -*- coding: utf-8 -*-
"""全点保存の指紋(h)の検算。標準ライブラリだけ・通信なし。

実行: python -m unittest discover -s tests -p "test_*.py"
確かめるのは 3 つ=
  ①同じ中身なら同じ h・1 組でもオッズや人気が変われば違う h(人気順の券種と枠連の両方)
  ②h は投入する JSON と同じ直列化の md5(16進 32 桁)
  ③前の周回と同じ h は積まない・違えば積む・最終は同じ h でも 1 回だけ積む(もう積んだ最終は積まない)
  ④全部のオッズが 0.0 の板は「板ではない」= has_odds が偽(一部だけ 0.0 は真)
"""
import datetime as dt
import hashlib
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from odds_full import combos_h, combos_json, has_odds, rows_for_kind    # noqa: E402

# parse_ranking の返り [(組, オッズ, 人気)] と parse_waku の返り [[枠, 枠, オッズ]]
GOT = [((1, 2), 6.2, 1), ((3, 4), 12.5, 2), ((2, 5), 30.0, 3)]
WAKU = [[1, 1, 8.4], [1, 2, 6.2], [2, 3, 15.0]]
TARGET = {"track": "高知", "race_no": 5, "baba": "31", "post": 900, "delta": 20}
DATE = dt.date(2026, 9, 8)
NOW = "2026-09-08T05:00:00+00:00"
KEY = ("高知", 5, "umaren")


class CombosHTest(unittest.TestCase):
    def test_same_input_same_h(self):
        self.assertEqual(combos_h("umaren", GOT), combos_h("umaren", list(GOT)))
        self.assertEqual(combos_h("wakuren", WAKU), combos_h("wakuren", [list(r) for r in WAKU]))

    def test_one_odds_changed_changes_h(self):
        moved = [(GOT[0][0], 6.3, GOT[0][2])] + GOT[1:]
        self.assertNotEqual(combos_h("umaren", GOT), combos_h("umaren", moved))
        wmoved = [[1, 1, 8.5]] + WAKU[1:]
        self.assertNotEqual(combos_h("wakuren", WAKU), combos_h("wakuren", wmoved))

    def test_popularity_changed_changes_h(self):
        # 人気だけ入れ替わっても「中身が変わった」= 積む(公式の整数をそのまま写しているため)
        swapped = [(GOT[0][0], 6.2, 2), (GOT[1][0], 12.5, 1)] + GOT[2:]
        self.assertNotEqual(combos_h("umaren", GOT), combos_h("umaren", swapped))

    def test_h_is_md5_of_the_json_we_send(self):
        body = json.dumps(combos_json("umaren", GOT), ensure_ascii=False)
        self.assertEqual(combos_h("umaren", GOT), hashlib.md5(body.encode("utf-8")).hexdigest())
        self.assertRegex(combos_h("umaren", GOT), "^[0-9a-f]{32}$")


class HasOddsTest(unittest.TestCase):
    """開催中止のあと、3連単のページだけが「最終・全組 0.0・人気順 1〜N」を返し続けた(2026-09-08 実測)。
    組数は全通りと一致するので検算は通ってしまう= オッズ側で見分ける。"""

    def test_all_zero_is_not_a_board(self):
        self.assertIs(has_odds([((3, 8, 7), 0.0, 1), ((3, 7, 8), 0.0, 2)]), False)

    def test_one_real_odds_is_a_board(self):
        # ⛔一部の組だけ 0.0(その組は売っていない)は今までどおり通す
        self.assertIs(has_odds([((3, 8, 7), 0.0, 1), ((3, 7, 8), 12.5, 2)]), True)

    def test_empty_is_not_a_board(self):
        self.assertIs(has_odds([]), False)

    def test_waku_rows_are_read_the_same_way(self):
        # 枠連の行は [枠, 枠, オッズ] で オッズの位置が違う(人気順は (組, オッズ, 人気))
        self.assertIs(has_odds([[1, 1, 0.0], [1, 2, 0.0]]), False)
        self.assertIs(has_odds(WAKU), True)


class RowsForKindTest(unittest.TestCase):
    def build(self, prev, is_final=False):
        return rows_for_kind(TARGET, DATE, "umaren", GOT, is_final, NOW, prev)

    def test_new_content_is_stacked(self):
        row, tick = self.build({})
        self.assertEqual(row["h"], combos_h("umaren", GOT))
        self.assertIs(row["is_final"], False)
        self.assertIsNotNone(tick)
        self.assertEqual(tick["observed_at"], row["observed_at"])
        self.assertIs(tick["f"], False)
        self.assertEqual(tick["combos"], row["combos"])

    def test_tick_has_the_columns_of_the_stacking_table(self):
        _row, tick = self.build({})
        self.assertEqual(sorted(tick), sorted(["track", "race_date", "race_no", "kind",
                                               "observed_at", "f", "h", "combos"]))
        self.assertEqual(tick["race_date"], "2026-09-08")

    def test_same_content_is_not_stacked(self):
        prev = {KEY: combos_h("umaren", GOT)}
        row, tick = self.build(prev)
        self.assertIsNone(tick)
        self.assertEqual(row["h"], prev[KEY])          # 最新の行(上書き)はいつも書く

    def test_final_is_stacked_even_when_same(self):
        row, tick = self.build({KEY: combos_h("umaren", GOT)}, is_final=True)
        self.assertIsNotNone(tick)
        self.assertIs(tick["f"], True)
        self.assertIs(row["is_final"], True)

    def test_final_already_stacked_is_not_stacked_again(self):
        # 1 券種でも取れない周があるとそのレースは発走 +8 分まで毎周取りに来る(pick_targets が外すのは
        # 7 券種そろって最終のレースだけ)。前の周で最終として積んだ券種は、同じ中身なら積まない。
        prev_h, prev_final = {KEY: combos_h("umaren", GOT)}, {KEY}
        row, tick = rows_for_kind(TARGET, DATE, "umaren", GOT, True, NOW, prev_h, prev_final)
        self.assertIsNone(tick)
        self.assertIs(row["is_final"], True)          # 最新の行(上書き)は毎周書く
        # ⛔積んだ後でも中身が動けば積む(最終の訂正を落とさない)
        moved = [(GOT[0][0], 6.3, GOT[0][2])] + GOT[1:]
        _row, tick2 = rows_for_kind(TARGET, DATE, "umaren", moved, True, NOW, prev_h, prev_final)
        self.assertIsNotNone(tick2)

    def test_another_race_fingerprint_is_not_reused(self):
        _row, tick = self.build({("高知", 6, "umaren"): combos_h("umaren", GOT)})
        self.assertIsNotNone(tick)


if __name__ == "__main__":
    unittest.main()
