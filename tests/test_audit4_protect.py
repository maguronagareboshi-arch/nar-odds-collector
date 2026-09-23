"""監査 #4: DB に結果がある走を、結果の無い行の null で上書きしない(偽の DB 読み。⛔通信なし)"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (os.path.join(HERE, "..", "pipeline"), os.path.join(HERE, "..")):
    sys.path.insert(0, p)

import load_nar_official as L  # noqa: E402


def _dedup():
    run_no = {"track": "浦和", "race_date": "2026-08-20", "race_no": 1, "runner_number": 1, "horse_name": "A",
              "finish": None, "finish_note": None, "time_raw": None, "time_sec": None, "margin": None,
              "last3f": None, "popularity": None, "jockey": "騎手"}
    run_yes = dict(run_no, runner_number=2, finish=1, time_raw="1:30.0")
    race = {"track": "浦和", "race_date": "2026-08-20", "race_no": 1, "race_last3f": None, "furlongs": [],
            "corners": [], "weather": None, "going": None, "distance_m": 1400}
    future = dict(run_no, race_date="2999-01-01")
    return {"runs": {("浦和", "2026-08-20", 1, 1): run_no, ("浦和", "2026-08-20", 1, 2): run_yes,
                     ("浦和", "2999-01-01", 1, 1): future},
            "races": {("浦和", "2026-08-20", 1): race}, "payouts": {}, "horses": {}}


class Protect(unittest.TestCase):
    def test_drops_null_result_cols_when_db_has_result(self):
        d = _dedup()
        asked = []

        def fetch(url, key, dates, log=print):
            asked.append(sorted(dates))
            return {("浦和", "2026-08-20", 1, 1)}
        n_run, n_race = L.guard_before_upsert("u", "k", d, log=lambda *_: None, fetch=fetch)
        self.assertEqual(asked, [["2026-08-20"]])               # 先の日は読まない
        r = d["runs"][("浦和", "2026-08-20", 1, 1)]
        for c in L.RUN_RESULT_COLS:
            self.assertNotIn(c, r)
        self.assertEqual(r["jockey"], "騎手")                    # 出馬表側は送る
        self.assertEqual(d["runs"][("浦和", "2026-08-20", 1, 2)]["finish"], 1)   # 結果ありの行は触らない
        race = d["races"][("浦和", "2026-08-20", 1)]
        self.assertNotIn("race_last3f", race)
        self.assertNotIn("furlongs", race)
        self.assertEqual(race["distance_m"], 1400)
        self.assertEqual((n_run, n_race), (1, 1))

    def test_no_db_result_keeps_cols(self):
        d = _dedup()
        L.guard_before_upsert("u", "k", d, log=lambda *_: None, fetch=lambda *a, **k: set())
        self.assertIn("finish", d["runs"][("浦和", "2026-08-20", 1, 1)])
        self.assertIn("race_last3f", d["races"][("浦和", "2026-08-20", 1)])

    def test_read_failure_stops_upsert(self):
        d = _dedup()
        orig, sent = L.db_result_keys, []
        L.db_result_keys = lambda *a, **k: (_ for _ in ()).throw(OSError("down"))
        orig_up = L.upsert
        L.upsert = lambda *a, **k: sent.append(1) or (201, "")
        try:
            rc = L.upsert_all("u", "k", d, log=lambda *_: None)
        finally:
            L.db_result_keys, L.upsert = orig, orig_up
        self.assertEqual(rc, 1)
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
