# -*- coding: utf-8 -*-
"""§238b 遡り便: 楽天競馬の残っているページ(2010〜)から、公式 ZIP と同じ形の行を作って Supabase へ入れる。

  py -3.12 rakuten_backfill.py --year-month 2022-10 --dry          # 取って整えるだけ(out/ に JSON)
  py -3.12 rakuten_backfill.py --year-month 2022-10                # 投入(SUPABASE_URL / SUPABASE_SERVICE_KEY)
  py -3.12 rakuten_backfill.py --horse-check 2022-11-01            # 馬 ID の照合だけ(⛔書かない)
  py -3.12 rakuten_backfill.py --date 2026-09-21 --votes-only      # §238c 前日の票数だけ(1 日 1 場 1 ページ)
  py -3.12 rakuten_backfill.py --year-month 2022-11 --votes-only   # 票数だけの遡り(公式のある月でも取る)

順= カレンダー(その月の開催日と RACEID)→ 払戻の日ページ(1 日 1 場)→ 成績のレースページ → upsert。
入れ先= nar_races / nar_runs / nar_race_payouts / nar_race_votes / nar_horse_ext_ids(楽天の馬 ID)。
進み具合は nar_meta 'rakuten_backfill:v1'。

⛔守ること
  ・1 秒 1 ページ・並列なし(--gap で伸ばせる。縮められない)。生 HTML は保存しない(--dry の整形済み JSON だけ)。
  ・公式のある日(OFFICIAL_FROM 以降)は取らない・上書きしない。同じ月を何度流しても行は増えない(upsert)。
  ・鍵はコードに書かない(環境変数)。連絡先も環境変数 COLLECTOR_CONTACT から User-Agent に付ける。
  ・楽天に無い列(回り・生年月日・血統・馬主・生産者)は**送らない**(null で公式の値を消さないため)。
終了コード: 0 正常(時間切れの途中終了も 0= 次の回に続きを取る)/ 1 投入失敗 / 2 前提の読み取りに失敗
"""
import argparse
import calendar
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import rakuten_parse as rp

JST = dt.timezone(dt.timedelta(hours=9))
BASE = "https://keiba.rakuten.co.jp"
CALENDAR = BASE + "/calendar/"
DIVIDEND = BASE + "/race_dividend/list/RACEID/"
PERFORMANCE = BASE + "/race_performance/list/RACEID/"
CONTACT = os.environ.get("COLLECTOR_CONTACT", "")
UA = os.environ.get("COLLECTOR_UA") or ("nar-backfill/1.0" + (f" (+{CONTACT})" if CONTACT else ""))
TIMEOUT = 30
GAP = 1.0                      # ⛔1 秒 1 ページ
OFFICIAL_FROM = "2022-11-01"   # これ以降は公式 ZIP がある= 取らない
META_KEY = "rakuten_backfill:v1"
# §238c 票数だけの便の進み具合。⛔遡り便の進み具合(META_KEY)と混ぜない(別の便・別の範囲)
VOTES_META_KEY = "rakuten_votes:v1"
SOURCE = "rakuten"
BATCH = 500
UPSERT_TRIES = 5               # ⛔投入の出直し(2026-09-22= 3 回 3 秒では足りず 10 本捨てになった)
UPSERT_WAIT = 30               # 出直しの間隔(秒)
UPSERT_TIMEOUT = 120           # 1 回の待ち(秒)

KEYS = {
    "races": ("nar_races", "track,race_date,race_no"),
    "runs": ("nar_runs", "track,race_date,race_no,runner_number"),
    "payouts": ("nar_race_payouts", "track,race_date,race_no"),
    "votes": ("nar_race_votes", "track,race_date,race_no"),
    "ext_ids": ("nar_horse_ext_ids", "source,ext_id"),
}
TABLES = ("races", "runs", "payouts", "votes", "ext_ids")
RUN_TS = dt.datetime.now(dt.timezone.utc).isoformat()

# 冪等(何度流しても行が増えない)の土台= upsert の on_conflict が**主キーと同じ**であること。
# 入れ先の主キー(2026-09-21 に本番で確認・新しい 2 表は sql/rakuten_backfill_20260921.sql の宣言)。
PRIMARY_KEYS = {
    "nar_races": "track,race_date,race_no",
    "nar_runs": "track,race_date,race_no,runner_number",
    "nar_race_payouts": "track,race_date,race_no",
    "nar_race_votes": "track,race_date,race_no",
    "nar_horse_ext_ids": "source,ext_id",
    "nar_meta": "key",
}


def conflict_mismatches():
    """upsert の on_conflict と主キーが違う所を返す(空= 全部そろっている)。"""
    bad = []
    for name, (table, conflict) in KEYS.items():
        want = PRIMARY_KEYS.get(table)
        if want != conflict:
            bad.append(f"{name}: {table} on_conflict={conflict} / 主キー={want}")
    if PRIMARY_KEYS["nar_meta"] != "key":
        bad.append("nar_meta: 進み具合の upsert は key でぶつける")
    return bad


# ---------------------------------------------------------------- 月の一覧(便を月ごとに分ける)

def expand_months(text, first="2014-01", last="2022-10"):
    """'2014-01..2022-10' か '2022-10,2022-09' → 新しい月から順の一覧。

    ⛔範囲の外(公式 ZIP のある 2022-11 以降・下限より前)は落とす。同じ月は 1 度だけ。
    """
    out = []
    for part in str(text or "").replace("　", " ").replace(" ", "").split(","):
        if not part:
            continue
        if ".." in part:
            a, b = part.split("..", 1)
            lo, hi = min(a, b), max(a, b)
            y, m = (int(x) for x in lo.split("-"))
            while f"{y:04d}-{m:02d}" <= hi:
                out.append(f"{y:04d}-{m:02d}")
                y, m = (y + 1, 1) if m == 12 else (y, m + 1)
        else:
            out.append(part)
    seen, kept = set(), []
    for ym in out:
        if not re.fullmatch(r"\d{4}-\d{2}", ym):
            raise SystemExit(f"⛔月の書き方が違う: {ym}(YYYY-MM)")
        if ym < first or ym > last or ym in seen:
            continue
        seen.add(ym)
        kept.append(ym)
    if not kept:
        raise SystemExit(f"⛔取れる月が無い({first} 〜 {last} の中で指定する)")
    return sorted(kept, reverse=True)


def pending_months(months, meta_value):
    """まだ終わっていない月 → [(月, わけ), ...]。⛔nar_meta の done が True の月だけ落とす。

    わけ= 'まだ' 進み具合が無い / '途中(YYYY-MM-DD から)' 途中で止まった。
    """
    got = meta_value or {}
    out = []
    for ym in months:
        m = got.get(ym) or {}
        if m.get("done") is True:
            continue
        stopped = m.get("stopped_at")
        out.append((ym, f"途中({stopped} から)" if stopped else ("やり直し" if m else "まだ")))
    return out


def pick_tables(votes_only):
    """upsert する表。⛔票数だけの便は nar_race_votes しか書かない(公式の払戻・成績を上書きしない)。"""
    return ("votes",) if votes_only else TABLES


def meta_key(votes_only):
    """進み具合を書く nar_meta の鍵。⛔遡り便と票数の便で別の鍵にする。"""
    return VOTES_META_KEY if votes_only else META_KEY


def log(msg):
    print(f"[{dt.datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ---------------------------------------------------------------- 通信(1 秒 1 ページ)

RETRY_CODES = (429, 503)       # 「ちょっと待って」= 60 秒あけて出直す
RETRY_WAIT = 60
RETRY_MAX = 5


class Fetcher:
    """取得の窓口。間隔を必ずあけ、取った枚数を数える(⛔並列にしない)。

    429 / 503 が返ったら 60 秒待って出直す(最大 5 回)。待った回数は最後に報告する。
    """

    def __init__(self, gap=GAP, budget=None, sleep=time.sleep):
        self.gap = max(GAP, float(gap))
        self.budget = budget
        self.count = 0
        self.retries = 0
        self.waited = 0
        self.last = 0.0
        self._sleep = sleep

    def _wait(self):
        rest = self.gap - (time.time() - self.last)
        if rest > 0:
            self._sleep(rest)

    def _once(self, url, data):
        if self.budget is not None and self.count >= self.budget:
            raise RuntimeError(f"取得の上限 {self.budget} ページに達した")
        self._wait()
        req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read()
                status = r.status
        finally:
            self.last = time.time()
            self.count += 1
        if status != 200:
            raise RuntimeError(f"HTTP{status} {url}")
        return body.decode("utf-8", "replace")

    def get(self, url, data=None):
        for attempt in range(RETRY_MAX + 1):
            try:
                return self._once(url, data)
            except urllib.error.HTTPError as e:
                if e.code not in RETRY_CODES or attempt >= RETRY_MAX:
                    raise
                self.retries += 1
                self.waited += RETRY_WAIT
                log(f"  HTTP{e.code}= {RETRY_WAIT} 秒あけて出直す({attempt + 1}/{RETRY_MAX})")
                self._sleep(RETRY_WAIT)
                self.last = time.time()
        raise RuntimeError("到達しない")


def fetch_calendar(fetcher, year, month):
    data = urllib.parse.urlencode({"tYear": int(year), "tMonth": int(month)}).encode()
    return rp.parse_calendar(fetcher.get(CALENDAR, data))


# ---------------------------------------------------------------- Supabase(読み・書き)

def sb_get(url, key, path):
    req = urllib.request.Request(f"{url}/rest/v1/{path}", headers={
        "apikey": key, "Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


def upsert(url, key, table, conflict, rows, sleep=time.sleep):
    """⛔投入は 5 回まで・30 秒あけて出直す(2026-09-22 の事故)。

    9/22 に 10 本を同時に流して本番が重くなり、1 か月ぶん取り終えた後の最初の投入が
    「read operation timed out」= 3 秒あけて 3 回では足りず、10 本とも捨てになった。
    出直すのは **時間切れ(HTTP0)と 5xx** だけ。4xx は出直しても直らないのでそのまま返す。
    """
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?on_conflict={conflict}", data=body, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal", "User-Agent": UA})
    for attempt in range(UPSERT_TRIES):
        last = attempt == UPSERT_TRIES - 1
        try:
            with urllib.request.urlopen(req, timeout=UPSERT_TIMEOUT) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code >= 500 and not last:
                log(f"  ⚠{table} の投入をやり直す {attempt + 1}/{UPSERT_TRIES}"
                    f"(HTTP{e.code}・{UPSERT_WAIT} 秒待つ)")
                sleep(UPSERT_WAIT); continue
            return e.code, msg
        except Exception as e:                       # noqa: BLE001(通信は何が来ても再試行)
            if not last:
                log(f"  ⚠{table} の投入をやり直す {attempt + 1}/{UPSERT_TRIES}"
                    f"({type(e).__name__}・{UPSERT_WAIT} 秒待つ)")
                sleep(UPSERT_WAIT); continue
            return 0, str(e)


def load_env(path):
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


# ---------------------------------------------------------------- 公式の形に整える

# 楽天に無い列は**送らない**(null で公式の値を消さないため)。races/runs でここに載せていない列だけ送る。
RACE_COLS = ("post_time", "race_name", "surface", "distance_m", "weather", "going", "field_size",
             "condition", "prize_yen", "race_last4f", "race_last3f", "furlongs", "corners")
RUN_COLS = ("gate", "horse_name", "sex", "age", "jockey", "trainer", "carried_weight", "weight_mark",
            "body_weight", "body_weight_change", "finish", "finish_note", "time_raw", "time_sec",
            "margin", "last3f", "popularity")


def race_row(track, race_date, race_no, race):
    row = {"track": track, "race_date": race_date, "race_no": race_no,
           "source": SOURCE, "updated_at": RUN_TS}
    for col in RACE_COLS:
        row[col] = race.get(col)
    return row


def run_rows(track, race_date, race_no, runs):
    out = []
    for r in runs:
        row = {"track": track, "race_date": race_date, "race_no": race_no,
               "runner_number": r["runner_number"], "updated_at": RUN_TS}
        for col in RUN_COLS:
            row[col] = r.get(col)
        out.append(row)
    return out


def payout_row(track, race_date, race_no, payouts):
    return {"track": track, "race_date": race_date, "race_no": race_no,
            "payouts": payouts, "updated_at": RUN_TS}


def ext_id_row(race_date, run):
    """楽天の馬 ID を捨てずに残す 1 行(nar_horse_ext_ids)。⛔nar_runs には列を足さない。

    ID は先頭の 0 が落ちて 9 桁で出ることがあるので 10 桁に 0 詰めする。3〜6 桁目は登録の年らしい
    数字で**生年ではない**(2022-11-01 大井 154 頭で公式の生年と 0/154 一致)= そのまま入れるだけ。
    """
    raw = run.get("horse_id")
    if not raw or not str(raw).isdigit():
        return None
    ext = str(raw).zfill(10)
    year = int(ext[2:6]) if ext[2:6].isdigit() else None
    return {"source": SOURCE, "ext_id": ext, "horse_name": run.get("horse_name"),
            "birth_year": year, "first_seen": race_date, "last_seen": race_date,
            "updated_at": RUN_TS}


def merge_ext_ids(rows):
    """同じ ID が月内に何度も出るので 1 行に畳む(見た日の幅を広げる)。"""
    out = {}
    for r in rows:
        cur = out.get(r["ext_id"])
        if cur is None:
            out[r["ext_id"]] = r
            continue
        cur["first_seen"] = min(cur["first_seen"], r["first_seen"])
        cur["last_seen"] = max(cur["last_seen"], r["last_seen"])
    return list(out.values())


def votes_row(track, race_date, race_no, votes, refunds):
    return {"track": track, "race_date": race_date, "race_no": race_no,
            "votes": votes, "refunds": refunds, "source": SOURCE, "updated_at": RUN_TS}


# ---------------------------------------------------------------- 1 日 1 場を集める

def collect_day(fetcher, day, want_perf=True, votes_only=False):
    """1 日 1 場 → (5 表の行, 数えたもの)。払戻の日ページ 1 枚 + 成績のレース枚数ぶん取る。

    ⛔votes_only(§238c)= 払戻の日ページ 1 枚だけ取り、**票数の行しか作らない**。払戻は公式 ZIP に
      あるので上書きしない(票数は 2022-11 以降の公式 ZIP に無いので、ここでしか取れない)。
    """
    track, date, rid = day["track"], day["race_date"], day["raceid"]
    bag = {k: [] for k in TABLES}
    div = rp.parse_dividend_day(fetcher.get(DIVIDEND + rid))
    stat = {"races": 0, "runs": 0, "perf_missing": 0, "dropped": []}
    for no in sorted(div):
        d = div[no]
        stat["dropped"] += d.get("dropped") or []
        if not votes_only:
            bag["payouts"].append(payout_row(track, date, no, d["payouts"]))
        if d["votes"]:
            bag["votes"].append(votes_row(track, date, no, d["votes"], d["refunds"]))
        if votes_only or not want_perf:
            continue
        try:
            page = fetcher.get(PERFORMANCE + rp.race_id_for(rid, no))
        except Exception as e:                       # noqa: BLE001(1 レース落ちても日は続ける)
            stat["perf_missing"] += 1
            log(f"    成績が取れない {track} {date} {no}R: {type(e).__name__}: {str(e)[:100]}")
            continue
        p = rp.parse_performance(page, track)
        if not p["runs"]:
            stat["perf_missing"] += 1
            continue
        bag["races"].append(race_row(track, date, no, p["race"]))
        bag["runs"] += run_rows(track, date, no, p["runs"])
        bag["ext_ids"] += [x for x in (ext_id_row(date, r) for r in p["runs"]) if x]
        stat["races"] += 1
        stat["runs"] += len(p["runs"])
    return bag, stat


# ---------------------------------------------------------------- 欠け率の表

def missing_table(bag):
    """行の列ごとの「値が無い率」。表= [(表名, 列名, 行数, 欠け数, 率)]。"""
    out = []
    for name in TABLES:
        rows = bag[name]
        if not rows:
            continue
        cols = sorted({c for r in rows for c in r})
        for col in cols:
            if col in ("updated_at", "source"):
                continue
            miss = sum(1 for r in rows if r.get(col) in (None, "", [], {}))
            out.append((name, col, len(rows), miss, miss / len(rows)))
    return out


def print_missing(table):
    log("列の欠け率(値が無い行の割合)")
    for name, col, n, miss, rate in table:
        log(f"  {name:8s} {col:20s} {n:>7,} 行 欠け {miss:>7,} ({rate * 100:5.1f}%)")


# ---------------------------------------------------------------- 進み具合(nar_meta)

def save_meta(url, key, ym, summary, key_name=META_KEY):
    try:
        cur = sb_get(url, key, f"nar_meta?select=value&key=eq.{urllib.parse.quote(key_name)}")
        value = (cur[0]["value"] if cur else {}) or {}
    except Exception as e:                           # noqa: BLE001
        log(f"  ⚠nar_meta を読めない({type(e).__name__})= 今回の月だけ書く")
        value = {}
    value[ym] = summary
    st, msg = upsert(url, key, "nar_meta", "key",
                     [{"key": key_name, "value": value, "updated_at": RUN_TS}])
    if st >= 300 or st == 0:
        log(f"  ⚠nar_meta の記録に失敗 status={st} {msg}")


# ---------------------------------------------------------------- 馬 ID の照合(⛔書かない)

def horse_check(fetcher, url, key, date, tracks=None, cache=None):
    """公式と重なる 1 日を楽天から取り、公式の nar_runs と列ごとに突き合わせる(⛔読むだけ)。

    cache= 一度取った整形済みの行を置くファイル(同じ日を数え直すときに取り直さないため)。
    """
    rows = []
    if cache and Path(cache).exists():
        rows = json.loads(Path(cache).read_text(encoding="utf-8"))
        log(f"{date}: 取り直さずに {cache} の {len(rows)} 頭を使う")
    else:
        y, m, _ = date.split("-")
        days = [d for d in fetch_calendar(fetcher, int(y), int(m)) if d["race_date"] == date]
        if tracks:
            days = [d for d in days if d["track"] in tracks]
        log(f"{date} の開催 {[d['track'] for d in days]}")
        for day in days:
            div = rp.parse_dividend_day(fetcher.get(DIVIDEND + day["raceid"]))
            for no in sorted(div):
                page = fetcher.get(PERFORMANCE + rp.race_id_for(day["raceid"], no))
                for r in rp.parse_performance(page, day["track"])["runs"]:
                    rows.append({"track": day["track"], "race_no": no, **r})
        if cache:
            Path(cache).write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    cols = ("horse_name", "sex", "age", "jockey", "trainer", "carried_weight", "weight_mark",
            "body_weight", "body_weight_change", "finish", "time_raw", "margin", "last3f", "popularity")
    official = sb_get(url, key, "nar_runs?select=track,race_no,runner_number,birth_date,"
                                + ",".join(cols) + f"&race_date=eq.{date}")
    off = {(o["track"], o["race_no"], o["runner_number"]): o for o in official}
    both = year_ok = 0
    same = {c: 0 for c in cols}
    misses = []
    for r in rows:
        o = off.get((r["track"], r["race_no"], r["runner_number"]))
        if not o:
            misses.append(("公式に無い", r["track"], r["race_no"], r["runner_number"], r["horse_name"]))
            continue
        both += 1
        for c in cols:
            a, b = r.get(c), o.get(c)
            if _eq(a, b):
                same[c] += 1
            else:
                misses.append((c, r["track"], r["race_no"], r["runner_number"], f"楽天 {a!r} / 公式 {b!r}"))
        hid = r.get("horse_id") or ""
        if len(hid) == 10 and o.get("birth_date") and hid[2:6] == o["birth_date"][:4]:
            year_ok += 1
    log(f"楽天 {len(rows)} 頭 / 公式 {len(official)} 頭 / 同じ(場,R,馬番) {both} 頭")
    log(f"  馬 ID 10 桁の 3〜6 桁目が公式の生年と一致 {year_ok}/{both}")
    for c in cols:
        log(f"  {c:20s} 一致 {same[c]:>5,}/{both:,} ({same[c] / both * 100:5.1f}%)" if both else c)
    shown = {}
    for kind, tr, no, num, what in misses:
        shown.setdefault(kind, []).append(f"{tr} {no}R {num}番 {what}")
    for kind, examples in shown.items():
        log(f"  ⚠{kind}: {len(examples)} 件 / 例 " + " ; ".join(examples[:3]))
    return 0


def _eq(a, b):
    """楽天の値と公式の値が同じか。数字は数として比べる(公式は数字を文字で返す列がある)。"""
    if a is None or a == "":
        return b is None or b == ""
    if b is None or b == "":
        return False
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        return str(a) == str(b)


# ---------------------------------------------------------------- 本体

def month_days(fetcher, ym, since=None, skip_from=OFFICIAL_FROM, votes_only=False, today=None):
    """その月の「日×場」。⛔公式 ZIP のある日(skip_from 以降)は落とす。since 以降だけ残す(続きから)。

    ⛔votes_only(§238c)= 票数は公式 ZIP に無いので skip_from を外す(公式のある月でも取る)。
    ⛔今日より後の日は取らない(結果がまだ無い)。
    """
    y, m = (int(x) for x in ym.split("-"))
    if votes_only:
        skip_from = None
    if skip_from and f"{y:04d}-{m:02d}-01" >= skip_from:
        raise SystemExit(f"⛔{ym} は公式 ZIP のある期間({skip_from} 以降)= 取らない")
    last = f"{y:04d}-{m:02d}-{calendar.monthrange(y, m)[1]:02d}"
    if skip_from and last >= skip_from:
        log(f"⛔{skip_from} 以降は公式 ZIP があるので取らない({ym} の後半を落とす)")
    hi = today or f"{dt.datetime.now(JST).date()}"
    return [d for d in fetch_calendar(fetcher, y, m)
            if (skip_from is None or d["race_date"] < skip_from) and d["race_date"] <= hi
            and (since is None or d["race_date"] >= since)]


def date_days(fetcher, date, votes_only=False, skip_from=OFFICIAL_FROM):
    """1 日ぶんの「日×場」(§238c の毎日便)。⛔票数だけのときは公式のある日でも取る。"""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(date or "")):
        raise SystemExit(f"⛔日付の書き方が違う: {date}(YYYY-MM-DD)")
    if not votes_only and date >= skip_from:
        raise SystemExit(f"⛔{date} は公式 ZIP のある期間({skip_from} 以降)= --votes-only でだけ取れる")
    y, m, _d = date.split("-")
    return [d for d in fetch_calendar(fetcher, int(y), int(m)) if d["race_date"] == date]


def resume_from(url, key, ym, key_name=META_KEY):
    """nar_meta の進み具合 → その月をどこから続けるか(終わっていれば None)。"""
    try:
        cur = sb_get(url, key, f"nar_meta?select=value&key=eq.{urllib.parse.quote(key_name)}")
    except Exception as e:                           # noqa: BLE001
        log(f"  ⚠nar_meta を読めない({type(e).__name__})= 月の頭から")
        return None
    got = ((cur[0]["value"] if cur else {}) or {}).get(ym) or {}
    return got.get("stopped_at")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-month", help="YYYY-MM")
    ap.add_argument("--date", metavar="YYYY-MM-DD", help="§238c その 1 日だけ(毎日便)")
    ap.add_argument("--votes-only", action="store_true",
                    help="§238c 払戻の日ページだけ取り、nar_race_votes だけ書く(公式のある日でも取る)")
    ap.add_argument("--list-months", metavar="RANGE",
                    help="'2014-01..2022-10' か カンマ区切り → 月の一覧を GitHub Actions の出力の形で印刷")
    ap.add_argument("--pending", action="store_true",
                    help="--list-months に足す= nar_meta の進み具合で done でない月だけ残す(失敗月の一覧)")
    ap.add_argument("--horse-check", metavar="YYYY-MM-DD", help="馬 ID の照合だけ(⛔書かない)")
    ap.add_argument("--dry", action="store_true", help="投入しない(整形済み JSON を out/ に置く)")
    ap.add_argument("--max-seconds", type=int, default=19800, help="これを過ぎたら途中でやめる(既定 5.5 時間)")
    ap.add_argument("--gap", type=float, default=GAP, help="取得の間隔(秒・既定 1.0。縮まらない)")
    ap.add_argument("--max-pages", type=int, help="取得の上限ページ数(手元の検証用)")
    ap.add_argument("--perf-days", type=int, help="成績を取る「日×場」の数の上限(手元の検証用)")
    ap.add_argument("--tracks", help="--horse-check で場を絞る(カンマ区切り)")
    ap.add_argument("--cache", help="--horse-check の整形済みの行の置き場(あれば取り直さない)")
    ap.add_argument("--since", metavar="YYYY-MM-DD", help="この日から続ける(既定は nar_meta の続き)")
    ap.add_argument("--out", default="out", help="--dry のときの置き場")
    ap.add_argument("--env")
    ap.add_argument("--url")
    ap.add_argument("--key")
    a = ap.parse_args()
    if a.env:
        load_env(a.env)
    url = (a.url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
    key = a.key or os.environ.get("SUPABASE_SERVICE_KEY", "")
    if a.list_months:
        # ⛔票数だけの便は範囲が逆= 公式 ZIP に票数が無い 2022-11 以降〜当月
        months = (expand_months(a.list_months, first=OFFICIAL_FROM[:7],
                                last=f"{dt.datetime.now(JST):%Y-%m}")
                  if a.votes_only else expand_months(a.list_months))
        if a.pending:
            if not url or not key:
                log("⛔--pending は SUPABASE_URL / SUPABASE_SERVICE_KEY が要る(進み具合を読む)")
                return 2
            mkey = meta_key(a.votes_only)
            try:
                cur = sb_get(url, key, f"nar_meta?select=value&key=eq.{urllib.parse.quote(mkey)}")
            except Exception as e:                   # noqa: BLE001
                log(f"⛔nar_meta '{mkey}' を読めない({type(e).__name__})")
                return 2
            rest = pending_months(months, (cur[0]["value"] if cur else {}) or {})
            log(f"終わっていない月 {len(rest)} 本 / 指定 {len(months)} 本(鍵 {mkey})")
            for ym, why in rest:
                log(f"  {ym}  {why}")
            months = [ym for ym, _ in rest]
            if not months:
                print("months=[]")
                log("⛔終わっていない月は無い= 流すものが無い")
                return 0
        print("months=" + json.dumps(months, ensure_ascii=False))
        log(f"月の一覧 {len(months)} 本({months[0]} 〜 {months[-1]})")
        return 0
    bad = conflict_mismatches()
    if bad:
        log("⛔upsert の on_conflict が主キーと違う= 冪等にならない: " + " / ".join(bad))
        return 2
    fetcher = Fetcher(a.gap, a.max_pages)
    t0 = time.time()

    if a.horse_check:
        if not url or not key:
            log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い(照合は公式の行を読む)"); return 2
        return horse_check(fetcher, url, key, a.horse_check,
                           (a.tracks or "").split(",") if a.tracks else None, a.cache)

    if not a.year_month and not a.date:
        log("--year-month / --date / --horse-check のどれかが要る"); return 2
    if a.year_month and a.date:
        log("⛔--year-month と --date は同時に指定しない"); return 2
    if not a.dry and (not url or not key):
        log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い"); return 2

    votes_only = a.votes_only
    tables = pick_tables(votes_only)
    mkey = meta_key(votes_only)
    span = a.date or a.year_month
    if a.date:
        since, days = None, date_days(fetcher, a.date, votes_only)
    else:
        since = a.since or (resume_from(url, key, a.year_month, mkey)
                            if (url and key and not a.dry) else None)
        days = month_days(fetcher, a.year_month, since, votes_only=votes_only)
    log(f"{span}: 開催 {len(days)} 日×場" + (f"({since} から続き)" if since else "")
        + ("(票数だけ)" if votes_only else "") + f" / User-Agent={UA}")
    bag = {k: [] for k in TABLES}
    total = {"races": 0, "runs": 0, "perf_missing": 0, "days": 0, "dropped": set()}
    stopped = None
    for i, day in enumerate(days, 1):
        if time.time() - t0 > a.max_seconds:
            stopped = day["race_date"]
            log(f"時間切れ({a.max_seconds}s)= {stopped} の手前でやめる(続きは次の回)")
            break
        want_perf = (not votes_only) and (a.perf_days is None or total["days"] < a.perf_days)
        try:
            got, stat = collect_day(fetcher, day, want_perf, votes_only)
        except Exception as e:                       # noqa: BLE001(1 日落ちても月は続ける)
            log(f"  ⚠{day['track']} {day['race_date']} が取れない: {type(e).__name__}: {str(e)[:120]}")
            if "上限" in str(e):
                stopped = day["race_date"]
                break
            continue
        for k in TABLES:
            bag[k] += got[k]
        total["days"] += 1
        for k in ("races", "runs", "perf_missing"):
            total[k] += stat[k]
        total["dropped"] |= set(stat["dropped"])
        log(f"  [{i}/{len(days)}] {day['race_date']} {day['track']}: 票数 {len(got['votes'])} R"
            + ("" if votes_only else f" / 払戻 {len(got['payouts'])} R"
               f" / 成績 {stat['races']} R {stat['runs']} 頭")
            + ("" if want_perf or votes_only else "(成績は取らない)")
            + f"  取得 {fetcher.count} ページ")

    bag["ext_ids"] = merge_ext_ids(bag["ext_ids"])
    log(f"取得 {fetcher.count} ページ / {time.time() - t0:.0f}s"
        + (f" / 429・503 で待ち直した回数 {fetcher.retries}(合計 {fetcher.waited} 秒)" if fetcher.retries
           else " / 429・503 での待ち直し 0 回"))
    for k in tables:
        log(f"  {k:8s} {len(bag[k]):>8,} 行")
    if total["dropped"]:
        log(f"  ⚠読めない券種名 {sorted(total['dropped'])}")
    if total["perf_missing"]:
        log(f"  ⚠成績の取れなかったレース {total['perf_missing']} 件")
    table = missing_table(bag)
    print_missing(table)

    if a.dry:
        out = Path(a.out)
        out.mkdir(parents=True, exist_ok=True)
        for k in tables:
            (out / f"{span}_{k}.json").write_text(
                json.dumps(bag[k], ensure_ascii=False), encoding="utf-8")
        (out / f"{span}_missing.json").write_text(
            json.dumps([{"table": t, "column": c, "rows": n, "missing": m, "rate": round(r, 4)}
                        for t, c, n, m, r in table], ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"--dry: 投入しない。整形済み JSON を {out}/ に置いた(⛔生 HTML は保存していない)")
        return 0

    for k in tables:
        rows, (tbl, conflict) = bag[k], KEYS[k]
        for i in range(0, len(rows), BATCH):
            st, msg = upsert(url, key, tbl, conflict, rows[i:i + BATCH])
            if st >= 300 or st == 0:
                log(f"  {tbl}: HTTP{st} {msg} (batch {i})"); return 1
        log(f"  {tbl}: 投入 {len(rows):,} 行")
    save_meta(url, key, span, {
        "days": total["days"], "races": total["races"], "runs": total["runs"],
        "payouts": len(bag["payouts"]), "votes": len(bag["votes"]), "ext_ids": len(bag["ext_ids"]),
        "perf_missing": total["perf_missing"], "pages": fetcher.count,
        "votes_only": votes_only,
        "stopped_at": stopped, "done": stopped is None, "updated_at": RUN_TS,
    }, mkey)
    log(f"完了 {span}" + (f"(途中・続きは {stopped} から)" if stopped else ""))
    return 0


if __name__ == "__main__":
    code = main()
    log(f"終了 rc={code}")
    sys.exit(code)
