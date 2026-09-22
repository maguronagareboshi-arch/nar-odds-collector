# -*- coding: utf-8 -*-
"""§238c 票数だけの便(--date / --votes-only)の検算。標準ライブラリだけ・**通信なし**(取得は差し替える)。

実行: python -m unittest discover -s tests -p "test_*.py"
確かめるのは 5 つ=
  ①--votes-only は nar_race_votes だけ書く(払戻・成績・馬 ID は書かない= 公式を上書きしない)
  ②票数だけのときは払戻の日ページ 1 枚しか取らない(成績のレースページを取りに行かない)
  ③票数の無いレースは行を作らない(⛔0 で埋めない)
  ④--date= その日の「日×場」だけ返す。公式のある日は --votes-only のときだけ通す
  ⑤月ぶんは票数だけなら公式のある月でも通す・今日より後の日は取らない・進み具合の鍵は別
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rakuten_backfill as rb                                        # noqa: E402
import rakuten_parse as rp                                           # noqa: E402

DAY = {"track": "大井", "race_date": "2026-09-21", "raceid": "202609210401"}
DIV = {
    1: {"payouts": [{"t": "win", "c": "3", "y": 240, "p": 1}],
        "votes": {"win": 1000, "place": 500}, "refunds": {"win": 0}, "dropped": []},
    2: {"payouts": [{"t": "win", "c": "5", "y": 800, "p": 3}],
        "votes": {}, "refunds": {}, "dropped": ["謎券"]},     # ⛔票数が無い= 行を作らない
}
CAL = [DAY,
       {"track": "高知", "race_date": "2026-09-21", "raceid": "202609213101"},
       {"track": "大井", "race_date": "2026-09-22", "raceid": "202609220401"}]


class FakeFetcher:
    """取った URL を覚えるだけの窓口(⛔本物の通信はしない)。"""

    def __init__(self):
        self.urls = []

    def get(self, url, data=None):
        self.urls.append(url)
        return ""


class Patch(unittest.TestCase):
    def setUp(self):
        self._div, self._cal = rp.parse_dividend_day, rp.parse_calendar
        rp.parse_dividend_day = lambda _html: DIV
        rp.parse_calendar = lambda _html: CAL

    def tearDown(self):
        rp.parse_dividend_day, rp.parse_calendar = self._div, self._cal


class VotesOnly(Patch):
    def test_pick_tables(self):
        self.assertEqual(rb.pick_tables(True), ("votes",))
        self.assertEqual(rb.pick_tables(False), rb.TABLES)
        self.assertIn("payouts", rb.TABLES)

    def test_meta_key(self):
        self.assertEqual(rb.meta_key(True), "rakuten_votes:v1")
        self.assertEqual(rb.meta_key(False), rb.META_KEY)
        self.assertNotEqual(rb.meta_key(True), rb.meta_key(False))

    def test_collect_day_writes_votes_only(self):
        f = FakeFetcher()
        bag, stat = rb.collect_day(f, DAY, want_perf=False, votes_only=True)
        self.assertEqual(len(f.urls), 1)                      # 払戻の日ページ 1 枚だけ
        self.assertTrue(f.urls[0].startswith(rb.DIVIDEND))
        self.assertEqual(bag["payouts"], [])
        self.assertEqual(bag["runs"], [])
        self.assertEqual(bag["races"], [])
        self.assertEqual(bag["ext_ids"], [])
        self.assertEqual(len(bag["votes"]), 1)                # 票数の無い 2R は作らない
        v = bag["votes"][0]
        self.assertEqual((v["track"], v["race_date"], v["race_no"]), ("大井", "2026-09-21", 1))
        self.assertEqual(v["votes"], {"win": 1000, "place": 500})
        self.assertEqual(v["source"], "rakuten")
        self.assertEqual(stat["races"], 0)

    def test_collect_day_normal_still_has_payouts(self):
        f = FakeFetcher()
        bag, _stat = rb.collect_day(f, DAY, want_perf=False, votes_only=False)
        self.assertEqual(len(bag["payouts"]), 2)              # 今までの便は払戻を作る(⛔潰さない)
        self.assertEqual(len(bag["votes"]), 1)


class Days(Patch):
    def test_date_days(self):
        days = rb.date_days(FakeFetcher(), "2026-09-21", votes_only=True)
        self.assertEqual([d["track"] for d in days], ["大井", "高知"])

    def test_date_days_needs_votes_only_after_official(self):
        with self.assertRaises(SystemExit):
            rb.date_days(FakeFetcher(), "2026-09-21", votes_only=False)
        with self.assertRaises(SystemExit):
            rb.date_days(FakeFetcher(), "2026-9-1", votes_only=True)

    def test_month_days_votes_only_passes_official_period(self):
        days = rb.month_days(FakeFetcher(), "2026-09", votes_only=True, today="2026-09-21")
        self.assertEqual([d["race_date"] for d in days],
                         ["2026-09-21", "2026-09-21"])        # 翌日(今日より後)は取らない

    def test_month_days_without_votes_only_refuses_official_period(self):
        with self.assertRaises(SystemExit):
            rb.month_days(FakeFetcher(), "2026-09", votes_only=False)


def io_bytes(b):
    import io
    return io.BytesIO(b)


class Retry(unittest.TestCase):
    """⛔2026-09-22 の事故= 1 か月ぶん取り終えた後の投入が時間切れ 3 回で捨てになった。

    5 回・30 秒あけて出直す。出直すのは時間切れ(HTTP0)と 5xx だけ(4xx はそのまま返す)。
    """

    def setUp(self):
        self.real = rb.urllib.request.urlopen

    def tearDown(self):
        rb.urllib.request.urlopen = self.real

    def _run(self, urlopen):
        slept = []
        rb.urllib.request.urlopen = urlopen
        st, msg = rb.upsert("https://x", "k", "nar_race_votes", "track", [{"a": 1}],
                            sleep=slept.append)
        return st, msg, slept

    def test_numbers(self):
        self.assertEqual((rb.UPSERT_TRIES, rb.UPSERT_WAIT), (5, 30))

    def test_timeout_then_ok(self):
        calls = [0]

        class Res:
            status = 201

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_a):
                return False

        def urlopen(_req, timeout=None):
            self.assertEqual(timeout, rb.UPSERT_TIMEOUT)
            calls[0] += 1
            if calls[0] < 3:
                raise OSError("The read operation timed out")
            return Res()

        st, _msg, slept = self._run(urlopen)
        self.assertEqual(st, 201)
        self.assertEqual(slept, [30, 30])                 # 2 回待って 3 回目で通った

    def test_timeout_all_five(self):
        def urlopen(_req, timeout=None):
            raise OSError("The read operation timed out")

        st, msg, slept = self._run(urlopen)
        self.assertEqual(st, 0)                           # ログの「HTTP0 …timed out」と同じ形
        self.assertIn("timed out", msg)
        self.assertEqual(slept, [30, 30, 30, 30])         # 5 回試して 4 回待つ

    def test_4xx_does_not_retry(self):
        def urlopen(_req, timeout=None):
            raise rb.urllib.error.HTTPError("u", 400, "Bad Request", {}, io_bytes(b"no"))

        st, _msg, slept = self._run(urlopen)
        self.assertEqual(st, 400)
        self.assertEqual(slept, [])                       # ⛔4xx は出直しても直らない

    def test_5xx_retries(self):
        calls = [0]

        class Res:
            status = 204

            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *_a):
                return False

        def urlopen(_req, timeout=None):
            calls[0] += 1
            if calls[0] == 1:
                raise rb.urllib.error.HTTPError("u", 503, "busy", {}, io_bytes(b"busy"))
            return Res()

        st, _msg, slept = self._run(urlopen)
        self.assertEqual((st, slept), (204, [30]))


class Pending(unittest.TestCase):
    """⛔失敗月の一覧= nar_meta 'rakuten_votes:v1' の done でない月(--list-months --pending)。"""

    META = {
        "2025-01": {"done": True, "votes": 1200},
        "2025-02": {"done": False, "stopped_at": "2025-02-18"},
        "2025-03": {"done": True},
    }

    def test_pending_months(self):
        got = rb.pending_months(["2025-01", "2025-02", "2025-03", "2025-04"], self.META)
        self.assertEqual([ym for ym, _ in got], ["2025-02", "2025-04"])
        self.assertEqual(dict(got)["2025-02"], "途中(2025-02-18 から)")
        self.assertEqual(dict(got)["2025-04"], "まだ")

    def test_no_meta_means_all(self):
        got = rb.pending_months(["2025-01", "2025-02"], {})
        self.assertEqual([ym for ym, _ in got], ["2025-01", "2025-02"])
        self.assertEqual(rb.pending_months([], None), [])

    def test_done_true_only(self):
        # done が無い・None・"true"(文字列)は「終わっていない」側= ⛔勝手に済ませない
        got = rb.pending_months(["a", "b", "c"],
                                {"a": {"done": None}, "b": {"done": "true"}, "c": {"done": True}})
        self.assertEqual([ym for ym, _ in got], ["a", "b"])


if __name__ == "__main__":
    unittest.main()
