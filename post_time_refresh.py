# -*- coding: utf-8 -*-
"""地方競馬公式サイト(keiba.go.jp TodayRaceInfo/RaceList)の当日のレース一覧を読み、
発走時刻が変わっていたら Supabase の表 nar_races の post_time を書き換える。標準ライブラリだけ。

発走時刻は当日ずれることがある(実測 2026-09-03: ある場の 7R が 18:02 → 18:04)。
公式の一覧は直っているのに保存側が古いままだと、発走前後の窓で取るオッズも「あと何分」もその分ずれる。

  python post_time_refresh.py             # 今日(JST)の、当日レースのある場を全部
  python post_time_refresh.py --dry-run   # 取得と突合だけ(書き換えない)
環境変数: SUPABASE_URL / SUPABASE_SERVICE_KEY。COLLECTOR_UA で User-Agent を上書きできる。
終了コード: 0 正常(取得できない場があっても 0=次の回に任せる)/ 1 書き換え失敗 / 2 前提の読み取りに失敗

⛔書き換えるのは「同じ場・同じレース番号で時刻だけ違う行」だけ。
⛔ページのレース番号の集合が保存側と食い違う場(欠番・増減)は、ログを出して何も書かない。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from odds_full import BABA, JST, UA, http_get, load_env, log, sb_get, _text

LIST_URL = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/RaceList"
SLEEP = 1.0            # 1リクエストの間隔(秒)
TABLE = "nar_races"
RACE_RE = re.compile(r"^(\d{1,2})R$")
TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def list_url(date, baba):
    q = urllib.parse.urlencode({"k_raceDate": date.strftime("%Y/%m/%d"), "k_babaCode": baba})
    return f"{LIST_URL}?{q}"


def _cells(tr):
    return [_text(x) for x in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.S)]


def parse_list(page):
    """レース一覧ページ → {レース番号: 'HHMM'}。見出しから列の位置を決める。読めなければ空。

    表の入れ子で <table> の境目が当てにならないので、表を切り出さずに
    「見出しの行」で列位置を決め、そのあと全部の <tr> から該当する形の行だけ拾う。
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", page, re.S)
    ci = cj = None
    for tr in rows:
        c = _cells(tr)
        if "競走" in c and "発走時刻" in c:
            ci, cj = c.index("競走"), c.index("発走時刻")
            break
    if ci is None:
        return {}
    out = {}
    for tr in rows:
        c = _cells(tr)
        if len(c) <= max(ci, cj):
            continue
        mr, mt = RACE_RE.match(c[ci]), TIME_RE.match(c[cj])
        if not (mr and mt):
            continue
        no = int(mr.group(1))
        if no in out:                      # 同じ番号が二度出る作りなら信用しない
            return {}
        out[no] = f"{int(mt.group(1)):02d}{mt.group(2)}"
    return out


def patch(url, key, table, filters, body):
    """PostgREST の PATCH(絞り込みは filters の key=value をそのまま並べる)。"""
    q = "&".join(f"{k}={urllib.parse.quote(v, safe='.')}" for k, v in filters)
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?{q}", data=data, method="PATCH",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Prefer": "return=minimal", "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)


def diffs_for(track, saved, page):
    """保存側 {no: 'HHMM'} と ページ側 {no: 'HHMM'} → 書き換える行。集合が違えば None(触らない)。"""
    if not page:
        return None, "一覧が読めない"
    if set(page) != set(saved):
        only_p = sorted(set(page) - set(saved))
        only_s = sorted(set(saved) - set(page))
        return None, f"レース番号が食い違う(ページだけ {only_p} / 保存だけ {only_s})"
    return [(no, saved[no], page[no]) for no in sorted(saved) if saved[no] != page[no]], ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env")
    ap.add_argument("--url")
    ap.add_argument("--key")
    ap.add_argument("--date")
    a = ap.parse_args()
    if a.env:
        load_env(a.env)
    url = (a.url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
    key = a.key or os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い")
        return 2
    date = dt.date.fromisoformat(a.date) if a.date else dt.datetime.now(JST).date()
    try:
        races = sb_get(url, key, f"{TABLE}?select=track,race_no,post_time&race_date=eq.{date.isoformat()}"
                                 "&order=track.asc,race_no.asc")
    except Exception as e:
        log(f"{TABLE} の読み取りに失敗: {type(e).__name__}: {str(e)[:200]}")
        return 2
    saved = {}
    for r in races:
        no, pt = r.get("race_no"), r.get("post_time")
        if r.get("track") in BABA and no is not None and pt:
            saved.setdefault(r["track"], {})[int(no)] = str(pt)
    log(f"{date} 当日の場 {len(saved)}(保存レース {sum(len(v) for v in saved.values())})")
    if not saved:
        return 0
    changed, rc = 0, 0
    for i, track in enumerate(sorted(saved)):
        if i:
            time.sleep(SLEEP)
        try:
            page = http_get(list_url(date, BABA[track])).decode("utf-8", "replace")
        except Exception as e:
            log(f"  取得失敗 {track}: {type(e).__name__}: {str(e)[:120]}(触らない)")
            continue
        rows, why = diffs_for(track, saved[track], parse_list(page))
        if rows is None:
            log(f"  ⚠{track}: {why}= 触らない")
            continue
        if not rows:
            continue
        for no, old, new in rows:
            log(f"  {track} {no}R: {old}→{new}")
            changed += 1
            if a.dry_run:
                continue
            st, msg = patch(url, key, TABLE, [("race_date", f"eq.{date.isoformat()}"),
                                              ("track", f"eq.{track}"), ("race_no", f"eq.{no}")],
                            {"post_time": new})
            if st >= 300 or st == 0:
                log(f"    書き換え失敗 status={st} {msg}")
                rc = 1
    log(f"発走時刻の差 {changed} 件" + ("(dry-run: 書き換えない)" if a.dry_run else ""))
    return rc


if __name__ == "__main__":
    t0 = time.time()
    code = main()
    log(f"終了 rc={code} ({time.time() - t0:.0f}s)")
    sys.exit(code)
