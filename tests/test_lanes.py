# -*- coding: utf-8 -*-
"""§145 車線(発走までの残り分で 2 本に分ける)の検算。標準ライブラリだけ・通信なし。

実行: python -m unittest discover -s tests -p "test_*.py"
確かめるのは 5 つ=
  ①残り 10 分は近い車線だけ・11 分は遠い車線だけ・−8 分(発走 8 分後)は近い車線だけ
  ②2 本を合わせると 1 本のときと同じ窓を過不足なく覆う(取りこぼしも重なりも無い)
  ③--min-before を与えない呼び方は今までと同じ(既存の呼び出しを変えていない)
  ④周回が長引いて境目を割ったレースは相手に任せる(同じ分の行を 2 本作らない)。分けない周回では常に取る
  ⑤loop.py の車線ごとの引数組立= 近い車線は 60 秒・おまけ無し・遠い車線は --min-before 付き・
    分けない(既定)は 1 本で回していたときと同じ引数
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import odds_full                                                     # noqa: E402
import odds_tanfuku                                                  # noqa: E402
from loop import LANES, extra_cmds, lane_plan, odds_cmd              # noqa: E402
from odds_full import MIN_BEFORE_OFF, out_of_lane                    # noqa: E402

NOW = 12 * 60                      # 12:00
FAR = {"before": 40, "after": 8, "min_before": 10}      # 遠い車線(120 秒ごと)
HOT = {"before": 10, "after": 8, "min_before": MIN_BEFORE_OFF}   # 近い車線(60 秒ごと)


def race(delta, no):
    """発走まで delta 分のレース 1 行(公式の表記に合わせて post_time は 'HHMM')。"""
    pm = NOW + delta
    return {"track": "高知", "race_no": no, "post_time": f"{pm // 60:02d}{pm % 60:02d}"}


def picked(mod, lane, races):
    """その車線が取るレース番号。odds_tanfuku / odds_full どちらの選び方も同じであることを見る。"""
    got = mod.pick_targets(races, [], NOW, lane["before"], lane["after"], 200, lane["min_before"])
    return [t["race_no"] for t in got]


class PickTargetsLaneTest(unittest.TestCase):
    """①③ 境目の 3 通りと、車線を分けない呼び方"""

    def setUp(self):
        # 番号 = 発走までの残り分が分かるように付ける(41→1 番, 40→2 番 …)
        self.by_delta = {41: 1, 40: 2, 11: 3, 10: 4, 0: 5, -8: 6, -9: 7}
        self.races = [race(d, n) for d, n in self.by_delta.items()]

    def test_boundary_10_11_minus8(self):
        for mod in (odds_tanfuku, odds_full):
            far, hot = picked(mod, FAR, self.races), picked(mod, HOT, self.races)
            self.assertIn(self.by_delta[10], hot, mod.__name__ + ": 残り 10 分が近い車線に入っていない")
            self.assertNotIn(self.by_delta[10], far, mod.__name__ + ": 残り 10 分が遠い車線にも入っている")
            self.assertIn(self.by_delta[11], far, mod.__name__ + ": 残り 11 分が遠い車線に入っていない")
            self.assertNotIn(self.by_delta[11], hot, mod.__name__ + ": 残り 11 分が近い車線にも入っている")
            self.assertIn(self.by_delta[-8], hot, mod.__name__ + ": 発走 8 分後が近い車線に入っていない")
            self.assertNotIn(self.by_delta[-8], far, mod.__name__ + ": 発走 8 分後が遠い車線にも入っている")

    def test_outside_window_is_dropped(self):
        for mod in (odds_tanfuku, odds_full):
            both = picked(mod, FAR, self.races) + picked(mod, HOT, self.races)
            self.assertNotIn(self.by_delta[41], both, mod.__name__ + ": 40 分より前を取っている")
            self.assertNotIn(self.by_delta[-9], both, mod.__name__ + ": 8 分後より後を取っている")
            self.assertIn(self.by_delta[40], both)

    def test_two_lanes_cover_the_same_window(self):
        """② 残り −8〜40 分を 1 分ずつ並べ、どのレースもちょうど 1 本の車線が取る"""
        races = [race(d, d + 100) for d in range(-9, 42)]
        for mod in (odds_tanfuku, odds_full):
            far, hot = set(picked(mod, FAR, races)), set(picked(mod, HOT, races))
            one = set(t["race_no"] for t in mod.pick_targets(races, [], NOW, 40, 8, 200))
            self.assertEqual(far & hot, set(), mod.__name__ + ": 2 本の車線が同じレースを取っている")
            self.assertEqual(far | hot, one, mod.__name__ + ": 2 本を合わせても 1 本のときと同じにならない")

    def test_default_is_unchanged(self):
        """③ --min-before を渡さない呼び方= 今までと同じ(遠いも近いも 1 本で取る)"""
        for mod in (odds_tanfuku, odds_full):
            old = [t["race_no"] for t in mod.pick_targets(self.races, [], NOW, 40, 8, 40)]
            new = [t["race_no"] for t in mod.pick_targets(self.races, [], NOW, 40, 8, 40, MIN_BEFORE_OFF)]
            self.assertEqual(old, new)
            self.assertEqual(old, [self.by_delta[d] for d in (-8, 0, 10, 11, 40)])


class OutOfLaneTest(unittest.TestCase):
    """④ 周回の途中で境目を割ったレースは相手に任せる"""

    def target(self, delta):
        return {"track": "高知", "race_no": 1, "post": NOW + delta}

    def test_crossed_the_line_during_the_pass(self):
        # 周回のはじめは残り 11 分(遠い車線が取る)。取りに行くころに 12:01 になっていれば残り 10 分= 相手のもの
        self.assertFalse(out_of_lane(self.target(11), 10, NOW))
        self.assertTrue(out_of_lane(self.target(11), 10, NOW + 1))
        self.assertTrue(out_of_lane(self.target(11), 10, NOW + 5))

    def test_off_by_default(self):
        # 車線を分けない周回では、どれだけ長引いても外さない(⛔今までの動きを変えない)
        for cur in (NOW, NOW + 30, NOW + 120):
            self.assertFalse(out_of_lane(self.target(11), MIN_BEFORE_OFF, cur))


class LoopLaneTest(unittest.TestCase):
    """⑤ loop.py が組み立てる引数"""

    def test_periods(self):
        self.assertEqual(lane_plan("all")["every"], 120)
        self.assertEqual(lane_plan("far")["every"], 120)
        self.assertEqual(lane_plan("hot")["every"], 60)
        self.assertEqual(sorted(LANES), ["all", "far", "hot"])

    def test_default_lane_is_todays_command(self):
        # ⛔1 本で回していたときと 1 文字も変えない
        self.assertEqual(odds_cmd("odds_tanfuku.py", lane_plan("all")),
                         ["odds_tanfuku.py", "--before", "40", "--after", "8", "--limit", "40"])

    def test_far_hands_the_near_ones_over(self):
        self.assertEqual(odds_cmd("odds_full.py", lane_plan("far")),
                         ["odds_full.py", "--before", "40", "--after", "8", "--limit", "40",
                          "--min-before", "10"])

    def test_hot_is_the_near_window_without_a_handover(self):
        cmd = odds_cmd("odds_full.py", lane_plan("hot"))
        self.assertEqual(cmd, ["odds_full.py", "--before", "10", "--after", "8", "--limit", "40"])
        self.assertNotIn("--min-before", cmd)

    def test_hot_has_no_extras(self):
        hot = lane_plan("hot")
        for n in range(1, 16):
            self.assertEqual(extra_cmds(hot, n), [], f"近い車線の周回 {n} におまけが混ざっている")

    def test_far_keeps_the_extras_as_they_are(self):
        far = lane_plan("far")
        self.assertEqual(extra_cmds(far, 1), [["results_today.py"], ["post_time_refresh.py"]])
        self.assertEqual(extra_cmds(far, 2), [["results_today.py"], ["sales_rakuten.py", "--max-tracks", "1"]])
        self.assertEqual(extra_cmds(far, 3), [["results_today.py"]])
        self.assertEqual(extra_cmds(far, 6), extra_cmds(lane_plan("all"), 6))

    def test_explicit_flags_win(self):
        plan = lane_plan("hot", every=30, before=12, after=4, min_before=2)
        self.assertEqual((plan["every"], plan["before"], plan["after"], plan["min_before"]), (30, 12, 4, 2))


if __name__ == "__main__":
    unittest.main()
