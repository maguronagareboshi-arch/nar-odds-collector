"""監査 #19: 本当の失敗だけ rc≠0(偽の値で。本物のサイト・DB は叩かない)"""
import csv
import io
import os
import sys
import unittest
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import nar_official_csv as noc  # noqa: E402
import odds_full  # noqa: E402
import odds_tanfuku  # noqa: E402


def _zip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, (head, rows) in files.items():
            s = io.StringIO()
            w = csv.writer(s)
            w.writerow(head)
            w.writerows(rows)
            z.writestr(name, s.getvalue().encode("utf-8"))
    return buf.getvalue()


RACE_HEAD = ["競馬場", "競走年月日", "レース番号", "発走時刻", "距離"]
HORSE_HEAD = ["競馬場", "競走年月日", "レース番号", "馬番", "馬名", "着順", "人気", "生年月日"]


class Columns(unittest.TestCase):
    def test_ok(self):
        z = _zip({"x_racelist.csv": (RACE_HEAD, [["浦和", "2026/09/23", "1", "10:00", "1400"]]),
                  "x_horselist.csv": (HORSE_HEAD, [["浦和", "2026/09/23", "1", "1", "A", "1", "1", "2022/04/01"]])})
        doc = noc.normalize_archive(z, kind="race", scope="daily", source_url="u", observed_at="t")
        self.assertEqual(len(doc["horses"]), 1)

    def test_renamed_column_raises(self):
        head = [c if c != "着順" else "確定着順" for c in HORSE_HEAD]
        z = _zip({"x_racelist.csv": (RACE_HEAD, [["浦和", "2026/09/23", "1", "10:00", "1400"]]),
                  "x_horselist.csv": (head, [["浦和", "2026/09/23", "1", "1", "A", "1", "1", "2022/04/01"]])})
        with self.assertRaises(noc.ArchiveColumnsError) as cm:
            noc.normalize_archive(z, kind="race", scope="daily", source_url="u", observed_at="t")
        self.assertNotIsInstance(cm.exception, ValueError)   # 「開催なし」扱いにされない

    def test_empty_file_not_checked(self):
        z = _zip({"x_racelist.csv": (RACE_HEAD, [["浦和", "2026/09/23", "1", "10:00", "1400"]]),
                  "x_payback.csv": (["何か"], [])})
        noc.normalize_archive(z, kind="race", scope="daily", source_url="u", observed_at="t")


class AllFailed(unittest.TestCase):
    def test_tanfuku(self):
        self.assertTrue(odds_tanfuku.all_failed(0, 3, 0))
        self.assertFalse(odds_tanfuku.all_failed(0, 0, 3))    # 発売前だけ= 正常な空
        self.assertFalse(odds_tanfuku.all_failed(0, 2, 1))
        self.assertFalse(odds_tanfuku.all_failed(1, 2, 0))

    def test_full(self):
        base = {"ok": 0, "ng": 0, "empty": 0, "absent": 0, "reject": 0, "final": 0, "same": 0}
        self.assertTrue(odds_full.all_failed(dict(base, ng=7)))
        self.assertFalse(odds_full.all_failed(dict(base, empty=7)))
        self.assertFalse(odds_full.all_failed(dict(base, ng=5, absent=2)))
        self.assertFalse(odds_full.all_failed(dict(base, ok=1, ng=6)))


if __name__ == "__main__":
    unittest.main()
