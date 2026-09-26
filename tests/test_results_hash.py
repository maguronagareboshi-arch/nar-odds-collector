# -*- coding: utf-8 -*-
"""results_today の変化判定= レースごとの中身のハッシュ。標準ライブラリだけ・通信なし。

2026-09-26 高知 3R: 最初は 3 着までしか無く、後から 4 着以下とタイムが載っても「結果のあるレースの集合+払戻の行数」
が同じなので書き直されなかった。中身(着順・タイム・着差・上り)が変わればそのレースだけ書き直すことを確かめる。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from results_today import changed_races, race_hashes  # noqa: E402

D = "2026-09-26"


def run(no, finish=None, time_raw=None, ts="t1"):
    return {"track": "高知", "race_date": D, "race_no": 3, "runner_number": no, "finish": finish,
            "time_raw": time_raw, "margin": None, "last3f": None, "updated_at": ts}


def out(runs, snap="s1", ts="t1"):
    return {"races": {("高知", D, 3): {"track": "高知", "race_date": D, "race_no": 3, "source_snapshot_hash": snap, "updated_at": ts},
                      ("高知", D, 4): {"track": "高知", "race_date": D, "race_no": 4, "source_snapshot_hash": snap, "updated_at": ts}},
            "runs": {("高知", D, 3, r["runner_number"]): r for r in runs},
            "payouts": {("高知", D, 3): {"track": "高知", "race_date": D, "race_no": 3, "payouts": [{"t": "単勝", "c": "1", "y": 150, "p": 1}],
                                          "source_snapshot_hash": snap, "updated_at": ts}},
            "horses": {}}


class T(unittest.TestCase):
    def test_top3_then_full(self):
        a = race_hashes(out([run(1, 1), run(2, 2), run(3, 3), run(4)]))
        b = race_hashes(out([run(1, 1, "1:30.0"), run(2, 2), run(3, 3), run(4, 4)]))
        self.assertEqual(changed_races(b, a), {("高知", 3)})   # 3R だけ変化・4R は同じ

    def test_timestamps_ignored(self):
        a = race_hashes(out([run(1, 1)], snap="s1", ts="t1"))
        b = race_hashes(out([run(1, 1, ts="t2")], snap="s2", ts="t2"))
        self.assertEqual(changed_races(b, a), set())

    def test_no_prev(self):
        a = race_hashes(out([run(1, 1)]))
        self.assertEqual(changed_races(a, None), {("高知", 3), ("高知", 4)})


if __name__ == "__main__":
    unittest.main()
