# -*- coding: utf-8 -*-
"""地方競馬公式サイト(keiba.go.jp TodayRaceInfo)の当日の単勝・複勝オッズを Supabase の表 nar_race_odds へ保存する。
標準ライブラリだけ。取得・保存の道具は odds_full.py と共有。

  python odds_tanfuku.py              # 今日(JST)の「発走 80 分前〜10 分後」のレース(最大 20)
  python odds_tanfuku.py --dry-run    # 取得と解析だけ(投入しない)
環境変数: SUPABASE_URL / SUPABASE_SERVICE_KEY。COLLECTOR_UA で User-Agent を上書きできる。
終了コード: 0 正常 / 1 投入失敗 / 2 前提の読み取りに失敗

行の形: {track, race_date, race_no, observed_at, is_final, runners:[{n,w,pl,ph}], history:[{t,w,f?}], updated_at}
"""
import argparse
import datetime as dt
import os
import re
import sys
import time
import urllib.parse

from odds_full import BABA, JST, http_get, load_env, log, post_minutes, sb_get, upsert, _num, _text

ODDS_URL = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/OddsTanFuku"
SLEEP = 1.0
TABLE = "nar_race_odds"
CONFLICT = "track,race_date,race_no"


def odds_url(date, race_no, baba):
    q = urllib.parse.urlencode({"k_raceDate": date.strftime("%Y/%m/%d"), "k_raceNo": race_no, "k_babaCode": baba})
    return f"{ODDS_URL}?{q}"


def header_cols(thead):
    """見出しから列位置を決める(colspan を数える)。取れない見出しは既定値。"""
    cols = {"umaban": None, "win": None, "place": None, "place_span": 1}
    idx = 0
    for m in re.finditer(r"<th([^>]*)>(.*?)</th>", thead, re.S):
        attrs, label = m.group(1), _text(m.group(2))
        sp = re.search(r'colspan\s*=\s*["\']?(\d+)', attrs)
        span = int(sp.group(1)) if sp else 1
        if "馬番" in label and cols["umaban"] is None:
            cols["umaban"] = idx
        elif "単勝" in label and cols["win"] is None:
            cols["win"] = idx
        elif "複勝" in label and cols["place"] is None:
            cols["place"], cols["place_span"] = idx, span
        idx += span
    if cols["umaban"] is None or cols["win"] is None:
        cols = {"umaban": 1, "win": 3, "place": 4, "place_span": 2}
    if cols["place"] is None:
        cols["place"], cols["place_span"] = cols["win"] + 1, 2
    return cols


def parse_odds(page):
    """単複ページ → (runners, is_final)。表が無ければ (None, False)。"""
    m = re.search(r'<table[^>]*class="[^"]*odd_popular_table_02[^"]*"[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return None, False
    table = m.group(1)
    title = re.search(r'class="odd_title"[^>]*>(.*?)</h4>', page, re.S)
    is_final = "最終" in _text(title.group(1)) if title else False
    thead = re.search(r"<thead[^>]*>(.*?)</thead>", table, re.S)
    cols = header_cols(thead.group(1) if thead else "")
    body = re.search(r"<tbody[^>]*>(.*?)</tbody>", table, re.S)
    need = max(cols["umaban"], cols["win"], cols["place"] + cols["place_span"] - 1)
    runners, seen = [], set()
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body.group(1) if body else table, re.S):
        tds = [_text(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(tds) <= need:
            continue
        n = _num(tds[cols["umaban"]])
        if n is None or int(n) in seen:
            continue
        seen.add(int(n))
        pi = cols["place"]
        if cols["place_span"] >= 2:
            pl, ph = _num(tds[pi]), _num(tds[pi + 1])
        else:
            parts = re.split(r"[-〜~]", tds[pi])
            pl = _num(parts[0])
            ph = _num(parts[1]) if len(parts) > 1 else None
        runners.append({"n": int(n), "w": _num(tds[cols["win"]]), "pl": pl, "ph": ph})
    nums = [x["n"] for x in runners]
    if nums != sorted(nums) or nums != list(range(1, len(nums) + 1)):
        return None, is_final
    return (runners or None), is_final


def pick_targets(races, finals, now_min, before, after, limit):
    done = {(r.get("track"), int(r.get("race_no"))) for r in finals if r.get("is_final")}
    out = []
    for r in races:
        track, no = r.get("track"), r.get("race_no")
        baba = BABA.get(track)
        pm = post_minutes(r.get("post_time"))
        if baba is None or no is None or pm is None or (track, int(no)) in done:
            continue
        delta = pm - now_min
        if delta > before or delta < -after:
            continue
        out.append({"track": track, "race_no": int(no), "baba": baba, "post": pm, "delta": delta})
    out.sort(key=lambda x: (x["post"], x["track"]))
    return out[:limit]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env")
    ap.add_argument("--url")
    ap.add_argument("--key")
    ap.add_argument("--date")
    ap.add_argument("--before", type=int, default=80)
    ap.add_argument("--after", type=int, default=10)
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()
    if args.env:
        load_env(args.env)
    url = (args.url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
    key = args.key or os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い")
        return 2
    now = dt.datetime.now(JST)
    date = dt.date.fromisoformat(args.date) if args.date else now.date()
    now_min = now.hour * 60 + now.minute
    try:
        races = sb_get(url, key, f"nar_races?select=track,race_no,post_time&race_date=eq.{date.isoformat()}"
                                 "&order=track.asc,race_no.asc")
        finals = sb_get(url, key, f"{TABLE}?select=track,race_no,is_final,history&race_date=eq.{date.isoformat()}")
    except Exception as e:
        log(f"nar_races / {TABLE} の読み取りに失敗: {type(e).__name__}: {str(e)[:200]}")
        return 2
    hist_by = {(r.get("track"), int(r.get("race_no"))): (r.get("history") or [])
               for r in finals if r.get("race_no") is not None}
    targets = pick_targets(races, finals, now_min, args.before, args.after, args.limit)
    log(f"{date} 当日のレース {len(races)} / 対象 {len(targets)}(発走 {args.before} 分前〜{args.after} 分後・"
        f"最終済み {sum(1 for r in finals if r.get('is_final'))} は除く)")
    if not targets:
        return 0
    rows, ok, ng, empty = [], 0, 0, 0
    for i, t in enumerate(targets):
        if i:
            time.sleep(SLEEP)
        try:
            page = http_get(odds_url(date, t["race_no"], t["baba"])).decode("utf-8", "replace")
        except Exception as e:
            ng += 1
            log(f"  取得失敗 {t['track']} {t['race_no']}R: {type(e).__name__}: {str(e)[:120]}")
            continue
        runners, is_final = parse_odds(page)
        if not runners or all(x["w"] is None for x in runners):
            empty += 1
            log(f"  発売前/表なし {t['track']} {t['race_no']}R(発走まで {t['delta']} 分)")
            continue
        ok += 1
        # 履歴= 単勝だけを時刻付きで最大 12 件(超えたら中間を捨て、最初と直近を残す)
        hist = list(hist_by.get((t["track"], t["race_no"]), []))
        entry = {"t": dt.datetime.now(JST).strftime("%H:%M"),
                 "w": {str(x["n"]): x["w"] for x in runners if x["w"] is not None}}
        if is_final:
            entry["f"] = 1
        hist.append(entry)
        while len(hist) > 12:
            hist.pop(1)
        now_utc = dt.datetime.now(dt.timezone.utc).isoformat()
        rows.append({"track": t["track"], "race_date": date.isoformat(), "race_no": t["race_no"],
                     "observed_at": now_utc, "is_final": is_final, "runners": runners, "history": hist,
                     "updated_at": now_utc})
        low = min((x["w"] for x in runners if x["w"] is not None), default=None)
        log(f"  {t['track']} {t['race_no']}R {len(runners)}頭 単勝最低 {low}{' (最終)' if is_final else ''}")
    log(f"取得 成功 {ok} / 失敗 {ng} / 発売前 {empty} / 最終化 {sum(1 for r in rows if r['is_final'])}")
    if args.dry_run:
        log("dry-run: 投入しない")
        return 0
    if not rows:
        return 0
    status, msg = upsert(url, key, TABLE, CONFLICT, rows)
    if status >= 300 or status == 0:
        log(f"投入失敗 status={status} {msg}")
        return 1
    log(f"投入 {len(rows)} 行 -> {TABLE}")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    log(f"終了 rc={rc} ({time.time() - t0:.0f}s)")
    sys.exit(rc)
