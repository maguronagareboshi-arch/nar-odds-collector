# -*- coding: utf-8 -*-
"""§238b 楽天競馬のページの解析の検算。標準ライブラリだけ・通信なし。

fixture = tests/fixtures/rakuten/*.html(2022-10 と 2014-01 の各 1 レースぶんを手元で 1 回だけ取った写し)。
実行: py -3.12 -m unittest discover -s tests -p "test_*.py"

確かめるのは 6 つ=
  ①カレンダー= その月の「日×場」と 18 桁の RACEID を拾える(⛔回/日を自分で組み立てない)
  ②払戻= 公式 nar_race_payouts と同じ {"t","c","y","p"} の形・組番は順の無い券種が小さい順
  ③総票数/返還票数= 9 券種ぜんぶ数字で取れる
  ④成績= レースの条件と全着順が公式の列と同じ書式(タイムは数字だけ・着差は '2.1/2'・馬場/天候は公式の語)
  ⑤corners= 公式と同じ [{"name","order"}] で、本体の pipeline/facts.py の規則で**100% 読める**
  ⑥2014-01 の古いページも同じ解析で読める(ハロンタイムと上がりはこちらにはある)
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rakuten_parse as rp                                           # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures" / "rakuten"


def fixture(name):
    return (FIX / name).read_bytes().decode("utf-8")


# ⛔本体 pipeline/facts.py corner_ranks() と**同じ規則**の写し(ここは通信も import も無いテストなので手で置く)。
# 数字= 馬番 / `,` `-` `=` 空白は区切り / `( )` は併走= 先頭の順位を共有し次は頭数ぶん飛ぶ / 読めない字は捨てる。
def corner_ranks(order):
    s = str(order or "").strip()
    if not s:
        return None
    out, rank, i = {}, 1, 0
    while i < len(s):
        ch = s[i]
        if ch in ",-= 　":
            i += 1
            continue
        if ch == "(":
            end = s.find(")", i)
            if end < 0:
                return None
            nums = []
            for x in s[i + 1:end].split(","):
                x = x.strip()
                if not x.isdigit() or int(x) <= 0:
                    return None
                nums.append(int(x))
            if not nums:
                return None
            for n in nums:
                out.setdefault(n, rank)
            rank += len(nums)
            i = end + 1
            continue
        j = i
        while j < len(s) and s[j].isdigit():
            j += 1
        if j == i:
            return None
        out.setdefault(int(s[i:j]), rank)
        rank += 1
        i = j
    return out or None


class Calendar(unittest.TestCase):
    def test_days_and_raceids(self):
        days = rp.parse_calendar(fixture("calendar_202210.html"))
        self.assertEqual(len(days), 119)
        self.assertEqual(days[0], {"track": "帯広ば", "race_date": "2022-10-01",
                                   "raceid": "202210010304130100"})
        for d in days:
            self.assertRegex(d["raceid"], r"^\d{18}$")
            self.assertEqual(d["raceid"][:8], d["race_date"].replace("-", ""))
        # 場の名前は公式(nar_races.track)と同じ綴り。2022-10 は水沢と姫路が開催なしなので 13 場
        official = {"帯広ば", "門別", "盛岡", "水沢", "浦和", "船橋", "大井", "川崎",
                    "金沢", "笠松", "名古屋", "園田", "姫路", "高知", "佐賀"}
        got = {d["track"] for d in days}
        self.assertEqual(got - official, set())
        self.assertEqual(len(got), 13)

    def test_race_id_for(self):
        self.assertEqual(rp.race_id_for("202210013129080300", 11), "202210013129080311")


class Dividend(unittest.TestCase):
    def setUp(self):
        self.day = rp.parse_dividend_day(fixture("dividend_kochi_20221001.html"))

    def test_races(self):
        self.assertEqual(sorted(self.day), list(range(1, 12)))
        self.assertEqual(self.day[1]["dropped"], [])

    def test_payout_shape(self):
        got = self.day[1]["payouts"]
        self.assertIn({"t": "win", "c": "5", "y": 240, "p": 2}, got)
        self.assertIn({"t": "place", "c": "6", "y": 120, "p": 1}, got)
        self.assertIn({"t": "trifecta", "c": "5-6-4", "y": 860, "p": 1}, got)
        # 順の無い券種は小さい順(公式 canonical_combination と同じ)
        self.assertIn({"t": "trio", "c": "4-5-6", "y": 180, "p": 1}, got)
        self.assertIn({"t": "wide", "c": "4-5", "y": 140, "p": 2}, got)
        # 発売の無い券種('-')は行にしない
        self.assertEqual([p for p in got if p["t"].startswith("bracket")], [])
        self.assertEqual(got, sorted(got, key=lambda p: (p["t"], p["c"])))

    def test_votes(self):
        v, r = self.day[1]["votes"], self.day[1]["refunds"]
        self.assertEqual(sorted(v), sorted(rp.TICKETS.values()))
        self.assertEqual(v["win"], 21518)
        self.assertEqual(v["trifecta"], 86255)
        self.assertEqual(v["bracket_exacta"], 0)
        self.assertEqual(sum(r.values()), 0)

    def test_old_page(self):
        old = rp.parse_dividend_day(fixture("dividend_kawasaki_20140102.html"))
        self.assertEqual(sorted(old), list(range(1, 13)))
        self.assertTrue(old[1]["payouts"])
        self.assertEqual(sorted(old[1]["votes"]), sorted(rp.TICKETS.values()))


class Performance(unittest.TestCase):
    def setUp(self):
        self.new = rp.parse_performance(fixture("perf_kochi_20221001_1r.html"), "高知")
        self.old = rp.parse_performance(fixture("perf_kawasaki_20140102_1r.html"), "川崎")

    def test_race_2022(self):
        r = self.new["race"]
        self.assertEqual(r["post_time"], "1530")          # 公式と同じ 'HHMM'
        self.assertEqual(r["surface"], "ダート")
        self.assertEqual(r["distance_m"], 800)
        self.assertEqual(r["weather"], "晴")
        self.assertEqual(r["going"], "不良")               # 公式の語
        self.assertEqual(r["field_size"], 6)
        self.assertEqual(r["condition"], "サラブレッド系　2歳")   # 数字は公式と同じ半角
        self.assertEqual(r["prize_yen"], [2400000, 840000, 480000, 360000, 240000])
        self.assertIsNone(r["direction"])                  # ⛔楽天に無い= 埋めない
        self.assertEqual(r["furlongs"], [])                # 高知はハロンタイムの表が無い

    def test_runs_2022(self):
        first = self.new["runs"][0]
        self.assertEqual(first["runner_number"], 5)
        self.assertEqual(first["gate"], 5)
        self.assertEqual(first["horse_name"], "ダレカノカゼノアト")
        self.assertEqual(first["horse_id"], "1520220233")
        self.assertEqual((first["sex"], first["age"]), ("牝", 2))
        self.assertEqual(first["carried_weight"], 54.0)
        self.assertEqual(first["body_weight"], 485)
        self.assertEqual(first["finish"], 1)
        self.assertEqual(first["time_raw"], "505")         # 公式は数字だけ
        self.assertEqual(first["time_sec"], 50.5)
        self.assertIsNone(first["margin"])                 # 1 着の着差は公式も空
        self.assertEqual(first["last3f"], 37.8)
        self.assertEqual(first["popularity"], 2)
        self.assertEqual(self.new["runs"][1]["margin"], "2.1/2")   # '２　１／２' → 公式の書式
        self.assertEqual([r["runner_number"] for r in self.new["runs"]], [5, 6, 4, 1, 2, 3])

    def test_race_2014(self):
        r = self.old["race"]
        self.assertEqual(r["post_time"], "1120")
        self.assertEqual(r["distance_m"], 1400)
        self.assertEqual(r["going"], "稍重")
        self.assertEqual(r["furlongs"], [13.3, 12.5, 13.9, 11.8, 13.1, 14.8, 13.8])
        self.assertEqual((r["race_last4f"], r["race_last3f"]), (53.5, 41.7))
        self.assertEqual(r["field_size"], 8)

    def test_runs_2014(self):
        r = self.old["runs"][1]
        self.assertEqual(r["horse_name"], "リコーアイナハイナ")
        self.assertEqual(r["body_weight"], 433)
        self.assertEqual(r["body_weight_change"], 1)       # '433<br>+1'
        self.assertEqual(r["time_raw"], "1334")            # '1:33.2' の次の馬= 1:33.4
        self.assertEqual(r["time_sec"], 93.4)
        self.assertEqual(r["margin"], "1")
        self.assertEqual(self.old["runs"][0]["body_weight_change"], -8)


class MarksAndSex(unittest.TestCase):
    """⛔公式は減量の記号を「負担重量」に、楽天は「騎手名」に付ける。公式と同じ列へ移すこと。
    ⛔公式のセン馬は 'セン'、楽天は 'セ'。(2022-11-01 大井 1R= 公式と重なる日で答え合わせした 1 レース)"""

    def setUp(self):
        self.runs = rp.parse_performance(fixture("perf_ooi_20221101_1r.html"), "大井")["runs"]
        self.by = {r["runner_number"]: r for r in self.runs}

    def test_mark_moves_to_weight_mark(self):
        self.assertEqual(self.by[3]["weight_mark"], "▲")
        self.assertEqual(self.by[3]["jockey"], "田中洸")        # 記号は騎手名に残さない
        self.assertEqual(self.by[3]["carried_weight"], 51.0)
        self.assertEqual(self.by[1]["weight_mark"], "△")
        self.assertEqual(self.by[1]["jockey"], "新原周")
        self.assertIsNone(self.by[9]["weight_mark"])
        self.assertEqual(sum(1 for r in self.runs if r["weight_mark"]), 3)

    def test_official_row_matches(self):
        # 公式 nar_runs(2022-11-01 大井 1R 1番)の実際の行と同じ値になる
        r = self.by[1]
        self.assertEqual(r["horse_name"], "サブノスカイ")
        self.assertEqual((r["sex"], r["age"]), ("牝", 5))
        self.assertEqual((r["body_weight"], r["body_weight_change"]), (450, 19))
        self.assertEqual((r["finish"], r["time_raw"], r["time_sec"]), (13, "1467", 106.7))
        self.assertEqual((r["margin"], r["last3f"], r["popularity"]), ("4", 42.2, 13))
        self.assertEqual((r["jockey"], r["trainer"]), ("新原周", "上杉昌"))

    def test_sen(self):
        self.assertEqual(rp.parse_performance(fixture("perf_ooi_20221101_1r.html"), "大井")["runs"][0]["sex"],
                         "牝")
        self.assertEqual(rp.parse_runs("<div id=\"oddsField\"><tr data-grouping=\"1\">"
                                       "<td class=\"number\">1</td><td class=\"state\">セ5 /鹿毛</td>"
                                       "</tr></table>")[0]["sex"], "セン")


class Corners(unittest.TestCase):
    """⛔本体 pipeline/facts.py の規則で 100% 読めること(読めない並びが 1 つでもあれば落ちる)。"""

    def test_shape(self):
        new = rp.parse_performance(fixture("perf_kochi_20221001_1r.html"), "高知")["race"]["corners"]
        old = rp.parse_performance(fixture("perf_kawasaki_20140102_1r.html"), "川崎")["race"]["corners"]
        self.assertEqual(new, [{"name": "３コーナー", "order": "5,1,6,4,3,2"},
                               {"name": "４コーナー", "order": "5,6,1,4,3,2"}])
        self.assertEqual([c["name"] for c in old], ["１角", "２角", "３角", "４角"])
        self.assertEqual(old[1]["order"], "2,3,1,8,(4,6,7),5")

    def test_facts_can_read_all(self):
        total = ok = 0
        for name, track, size in (("perf_kochi_20221001_1r.html", "高知", 6),
                                  ("perf_kawasaki_20140102_1r.html", "川崎", 8)):
            for c in rp.parse_performance(fixture(name), track)["race"]["corners"]:
                total += 1
                got = corner_ranks(c["order"])
                self.assertIsNotNone(got, f"{name} {c}")
                self.assertEqual(len(got), size, f"{name} {c}")     # 並びに全頭いる
                self.assertEqual(min(got.values()), 1)
                ok += 1
        self.assertEqual(total, 6)
        self.assertEqual(ok, total)                                  # 100%

    def test_order_is_digits_and_separators_only(self):
        for name, track in (("perf_kochi_20221001_1r.html", "高知"),
                            ("perf_kawasaki_20140102_1r.html", "川崎")):
            for c in rp.parse_performance(fixture(name), track)["race"]["corners"]:
                self.assertRegex(c["order"], r"^[0-9,()\-= ]+$")


class Formats(unittest.TestCase):
    def test_margin(self):
        for raw, want in (("２　１／２", "2.1/2"), ("１／２", "1/2"), ("７", "7"),
                          ("アタマ", "アタマ"), ("クビ", "クビ"), ("", None), ("大差", "大差")):
            self.assertEqual(rp.norm_margin(raw), want, raw)

    def test_time(self):
        for raw, digits, sec in (("1:33.2", "1332", 93.2), ("50.5", "505", 50.5),
                                 ("2:06.1", "2061", 126.1), ("", None, None), ("-", None, None)):
            self.assertEqual(rp.norm_time(raw), digits, raw)
            self.assertEqual(rp.time_to_sec(raw), sec, raw)

    def test_combination(self):
        self.assertEqual(rp.combination("trio", "6-4-5"), "4-5-6")
        self.assertEqual(rp.combination("trifecta", "5-6-4"), "5-6-4")
        self.assertIsNone(rp.combination("quinella", "5"))
        self.assertIsNone(rp.combination("quinella", "5-5"))
        self.assertIsNone(rp.combination("win", "-"))


if __name__ == "__main__":
    unittest.main()
