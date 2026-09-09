# -*- coding: utf-8 -*-
"""§137 取り止めの印(公式 payback)と告知文(当日メニュー)の検算。標準ライブラリだけ・通信なし。

実行: python -m unittest discover -s tests -p "test_*.py"
確かめるのは 5 つ=
  ①組番が1つも無く払戻金に 100 がある = refund(発売後に取り止め・全額返還)
  ②組番も払戻金も全部空 = nosale(発売前に取り止め)
  ③走ったレース(着順あり/組番あり)は印なし・payback 行の無いレースは鍵ごと返さない
  ④告知文= 「第7競走以降」→7・開催そのものの中止→1・発走時刻の変更→書かない・告知なし→書かない
  ⑤当日の便が書く行= 取り止めのレースは **races だけ**足す(着順が無いので今までは 1 行も書かれなかった)
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nar_official_csv import race_cancel_marks                       # noqa: E402
from post_time_refresh import cancel_from, parse_list, warning_text  # noqa: E402
from results_today import rows_to_write                              # noqa: E402

TRACK = "笠松"
YMD = "20260908"
DATE = "2026-09-08"
# 発売後に取り止め= 券種の払戻金だけが 100 で埋まる(2026-09-08 の 7R〜10R の実測)
REFUND = {"単勝払戻金（円）": "100", "複勝払戻金1（円）": "100", "馬複払戻金（円）": "100",
          "馬単払戻金（円）": "100", "ワイド払戻金1（円）": "100",
          "３連複払戻金（円）": "100", "３連単払戻金（円）": "100"}
# 売れて確定した回(同じ日の 1R)
SOLD = {"単勝組番": "9", "単勝払戻金（円）": "140", "単勝人気": "1",
        "複勝組番1": "9", "複勝払戻金1（円）": "100", "複勝人気1": "2"}


def pb(no, **cols):
    row = {"競馬場": TRACK, "競走年月日": YMD, "レース番号": str(no)}
    row.update(cols)
    return row


def hz(no, finish):
    return {"track": TRACK, "race_date": DATE, "race_no": no, "finish": finish}


def key(no):
    return (TRACK, DATE, no)


class TestRaceCancelMarks(unittest.TestCase):
    def test_refund_and_nosale(self):
        """①② 組番なし+100=返還 / 組番も払戻金も空=発売前"""
        marks = race_cancel_marks([pb(7, **REFUND), pb(8)], [])
        self.assertEqual(marks[key(7)], "refund")
        self.assertEqual(marks[key(8)], "nosale")

    def test_ran_races_are_not_marked(self):
        """③ 走った回には印を付けない(組番がある・着順がある)"""
        marks = race_cancel_marks([pb(1, **SOLD), pb(2, **REFUND)], [hz(1, 1), hz(2, 3)])
        self.assertIsNone(marks[key(1)])         # 組番があれば売れて確定している
        self.assertIsNone(marks[key(2)])         # 着順が1頭でもあれば走っている(⛔印を付けない)

    def test_missing_payback_row_is_untouched(self):
        """③ payback 行の無いレースは鍵ごと返さない= 先の日を null で上書きしない"""
        marks = race_cancel_marks([pb(7, **REFUND)], [])
        self.assertIn(key(7), marks)
        self.assertNotIn(key(9), marks)
        self.assertEqual(race_cancel_marks([], [hz(1, 1)]), {})

    def test_unknown_shape_is_not_guessed(self):
        """③ 組番が無いのに 100 でない払戻金= 形が分からない= 印なし(⛔推定しない)"""
        marks = race_cancel_marks([pb(3, **{"単勝払戻金（円）": "540"})], [])
        self.assertIsNone(marks[key(3)])

    def test_conflicting_rows_are_not_marked(self):
        """③ 同じレースの payback 行が食い違うときは印なし(安全側)"""
        self.assertIsNone(race_cancel_marks([pb(7, **REFUND), pb(7, **SOLD)], [])[key(7)])
        self.assertIsNone(race_cancel_marks([pb(7, **SOLD), pb(7, **REFUND)], [])[key(7)])

    def test_bad_date_is_skipped(self):
        """日付が読めない行は飛ばす(1行で月次全体を落とさない)"""
        self.assertEqual(race_cancel_marks([{"競馬場": TRACK, "競走年月日": "", "レース番号": "1"}], []), {})

    def test_is_idempotent(self):
        """同じ入力なら何度読んでも同じ答え"""
        rows, horses = [pb(1, **SOLD), pb(7, **REFUND), pb(8)], [hz(1, 1)]
        self.assertEqual(race_cancel_marks(rows, horses), race_cancel_marks(rows, horses))


class TestWarningText(unittest.TestCase):
    HTML = (Path(__file__).resolve().parent / "fixtures" / "racelist_earlywarning.html").read_text(encoding="utf-8")

    def test_reads_the_notice(self):
        """④ 当日メニューの告知を1行で取り、「第7競走以降」を読む"""
        text = warning_text(self.HTML)
        self.assertIn("取り止めとなりました", text)
        self.assertIn("馬場コンディション不良", text)
        self.assertEqual(cancel_from(text), 7)

    def test_whole_day(self):
        """④ 「第N競走以降」が無く開催そのものの中止= 1R から"""
        self.assertEqual(cancel_from("本日の笠松競馬は、降雨のため開催を中止します。"), 1)
        self.assertEqual(cancel_from("9/8（火）の全競走を取り止めます。"), 1)

    def test_other_notices_are_not_written(self):
        """④ 取り止めの語が無い告知(発走時刻の変更など)には何も書かない"""
        self.assertIsNone(cancel_from("本日の第7競走以降の発走時刻を10分繰り下げます。"))
        self.assertIsNone(cancel_from(""))
        self.assertIsNone(cancel_from(None))
        self.assertIsNone(warning_text("<html><body><p>お知らせはありません</p></body></html>"))

    def test_race_list_still_parses(self):
        """告知のある日でも発走時刻の表は今までどおり読める(§137 で壊していない)"""
        got = parse_list(self.HTML)
        self.assertEqual(len(got), 10)
        self.assertEqual(got[1], "1155")
        self.assertEqual(got[10], "1655")


class TestRowsToWrite(unittest.TestCase):
    """⑤ 当日の便(2 分おき)が書く行。2026-09-08 の笠松と同じ形= 1〜6R が確定・7〜10R が取り止め"""

    @staticmethod
    def dedup():
        return {
            "races": {(TRACK, DATE, no): {"race_no": no} for no in range(1, 11)},
            "runs": {(TRACK, DATE, no, i): {} for no in range(1, 11) for i in (1, 2)},
            "payouts": {(TRACK, DATE, no): {} for no in range(1, 7)},
            "horses": {("ウマ",): {}},
        }

    def test_cancelled_races_are_written_the_same_day(self):
        out = rows_to_write(self.dedup(), [(TRACK, n) for n in range(1, 7)],
                            [(TRACK, n) for n in range(7, 11)])
        self.assertEqual(sorted(k[2] for k in out["races"]), list(range(1, 11)))   # 7〜10R も入る
        self.assertEqual(sorted({k[2] for k in out["runs"]}), list(range(1, 7)))   # ⛔runs は結果のある回だけ
        self.assertEqual(sorted(k[2] for k in out["payouts"]), list(range(1, 7)))
        self.assertEqual(out["horses"], {})                                        # ⛔馬テーブルは書かない

    def test_without_cancelled_nothing_changes(self):
        out = rows_to_write(self.dedup(), [(TRACK, n) for n in range(1, 7)], [])
        self.assertEqual(sorted(k[2] for k in out["races"]), list(range(1, 7)))

    def test_whole_day_cancelled(self):
        """丸一日の取り止め(着順が 1 つも無い日)でも races は書く"""
        out = rows_to_write(self.dedup(), [], [(TRACK, n) for n in range(1, 11)])
        self.assertEqual(sorted(k[2] for k in out["races"]), list(range(1, 11)))
        self.assertEqual(out["runs"], {})


if __name__ == "__main__":
    unittest.main()
