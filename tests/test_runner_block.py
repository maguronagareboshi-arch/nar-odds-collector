# -*- coding: utf-8 -*-
"""走行機ごと弾かれたら別の走行機へ渡す(2026-09-23 16:11〜17:28 の 404)の検算。標準ライブラリだけ・通信なし。

実行: python -m unittest discover -s tests -p "test_*.py"
確かめるのは 6 つ=
  ①全失敗(FETCH_BLOCKED)が 2 周以上・180 秒以上続いたときだけ確かめに行く(近い車線 60 秒ごと= 4 周目)
  ②1 本でも取れた/発売前(rc 0)・投入失敗(rc 1)・子が無い周は数え直す
  ③確かめのページも取れない= 印を置いて BLOCK_EXIT で抜ける
  ④確かめのページが取れる= 抜けずに数え直す(オッズのページだけの失敗で走行機を替えない)
  ⑤ほかの車線が置いた印を見たら、待っている間でも抜ける。⛔起動前の古い印は見ない
  ⑥全失敗の rc は投入失敗(1)と別の値・2 つのスクリプトで同じ値
"""
import datetime as dt
import os
import sys
import tempfile
import time as real_time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import loop                                                          # noqa: E402
import odds_full                                                     # noqa: E402
import odds_tanfuku                                                  # noqa: E402
from loop import BLOCK_AFTER, BLOCK_EXIT, BlockWatch, block_flag_seen  # noqa: E402
from odds_full import FETCH_BLOCKED                                  # noqa: E402

B = FETCH_BLOCKED


class Clock(dt.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 23, 16, 11, tzinfo=tz)


class FakeTime:
    """monotonic・time・sleep を 1 本の針で進める(本当には待たない)。"""

    def __init__(self):
        self.t = 0.0
        self.base = real_time.time()

    def monotonic(self):
        return self.t

    def time(self):
        return self.base + self.t

    def sleep(self, s):
        self.t += s


class WatchTest(unittest.TestCase):
    def test_hot_lane_trips_on_fourth_cycle(self):
        w = BlockWatch()
        self.assertEqual([w.feed([B, B], t) for t in (0, 60, 120, 180)], [False, False, False, True])

    def test_needs_two_cycles_even_after_long_wait(self):
        w = BlockWatch()
        self.assertFalse(w.feed([B], 0))
        self.assertTrue(w.feed([B], 1800))          # 朝の車線= 30 分ごとの 2 周目

    def test_any_other_rc_resets(self):
        for other in ([0, B], [1, B], [B, 1], []):
            w = BlockWatch()
            w.feed([B, B], 0)
            w.feed([B, B], 120)
            self.assertFalse(w.feed(other, 180), other)
            self.assertFalse(w.feed([B, B], 240), other)    # 数え直し= 1 周目
            self.assertFalse(w.feed([B, B], 300), other)    # 2 周目でも 60 秒

    def test_rc_values(self):
        self.assertNotIn(B, (0, 1, 2))
        self.assertIs(odds_tanfuku.FETCH_BLOCKED, odds_full.FETCH_BLOCKED)


class LoopTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.flag = os.path.join(self.dir.name, "blocked")
        self.clock = FakeTime()

    def tearDown(self):
        self.dir.cleanup()

    def run_main(self, child, probe_result, lane="hot"):
        argv = ["loop.py", "--until", "2200", "--lane", lane, "--block-flag", self.flag]
        with patch.object(loop.dt, "datetime", Clock), patch.object(loop.sys, "argv", argv), \
                patch.object(loop, "time", self.clock), patch.object(loop, "bounded_child", side_effect=child), \
                patch.object(loop, "probe", return_value=probe_result) as pr:
            return loop.main(), pr.call_count

    def test_blocked_runner_leaves_flag_and_exits(self):
        rc, probes = self.run_main(lambda *a: B, "HTTPError: HTTP Error 404: Not Found")
        self.assertEqual(rc, BLOCK_EXIT)
        self.assertEqual(probes, 1)
        self.assertTrue(Path(self.flag).exists())
        self.assertGreaterEqual(self.clock.t, BLOCK_AFTER)

    def test_probe_ok_keeps_running(self):
        calls = {"n": 0}

        def child(*a):
            calls["n"] += 1
            return None if calls["n"] > 40 else B   # 40 本で実行枠の終わり(None)= 正常終了
        rc, probes = self.run_main(child, None)
        self.assertEqual(rc, 0)
        self.assertGreaterEqual(probes, 2)          # 数え直して、また 180 秒後に確かめる
        self.assertFalse(Path(self.flag).exists())

    def test_other_lane_flag_ends_long_wait(self):
        calls = {"n": 0}

        def child(*a):
            calls["n"] += 1
            if calls["n"] == 1:
                return 0
            return None
        # 朝の車線(1800 秒待つ)の待ちの途中で、ほかの車線が印を置く
        real_sleep = self.clock.sleep

        def sleep(s):
            real_sleep(s)
            if self.clock.t >= 30 and not Path(self.flag).exists():
                Path(self.flag).write_text("x", encoding="utf-8")
                os.utime(self.flag, (self.clock.time(), self.clock.time()))
        self.clock.sleep = sleep
        rc, _ = self.run_main(child, None, lane="morning")
        self.assertEqual(rc, BLOCK_EXIT)
        self.assertLess(self.clock.t, 60)           # 30 分待たずに抜けた
        self.assertEqual(calls["n"], 1)

    def test_stale_flag_is_ignored(self):
        Path(self.flag).write_text("old", encoding="utf-8")
        old = real_time.time() - 3600
        os.utime(self.flag, (old, old))
        self.assertFalse(block_flag_seen(self.flag, real_time.time()))
        self.assertTrue(block_flag_seen(self.flag, old - 1))
        self.assertFalse(block_flag_seen(os.path.join(self.dir.name, "none"), 0))


if __name__ == "__main__":
    unittest.main()
