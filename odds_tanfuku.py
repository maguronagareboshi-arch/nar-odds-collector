# -*- coding: utf-8 -*-
"""地方競馬公式サイト(keiba.go.jp TodayRaceInfo)の当日の単勝・複勝オッズを Supabase の表 nar_race_odds へ保存する。
標準ライブラリだけ。取得・保存の道具は odds_full.py と共有。

  python odds_tanfuku.py              # 今日(JST)の「発走 80 分前〜10 分後」のレース(最大 20)
  python odds_tanfuku.py --dry-run    # 取得と解析だけ(投入しない)
環境変数: SUPABASE_URL / SUPABASE_SERVICE_KEY。COLLECTOR_UA で User-Agent を上書きできる。
終了コード: 0 正常 / 1 投入失敗 / 2 前提の読み取りに失敗

行の形: {track, race_date, race_no, observed_at, is_final, runners:[{n,w,pl,ph}], history:[{t,w,f?}], updated_at}
2 分刻みの行: {track, race_date, race_no, t(取得時刻), asof(ページに書かれた時刻), f, w, p}
"""
import argparse
import datetime as dt
import os
import re
import sys
import time
import urllib.parse

from odds_full import (BABA, JST, MIN_BEFORE_OFF, http_get, load_env, log, now_minute, out_of_lane,
                       post_minutes, sb_get, upsert, _num, _text)

ODDS_URL = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo/OddsTanFuku"
SLEEP = 1.0
TABLE = "nar_race_odds"
CONFLICT = "track,race_date,race_no"
TICKS = "nar_odds_ticks"       # 2 分刻み(insert only・1 巡回 1 行)
HIST_GAP_MIN = 15              # history(約 20 分粒度)へ追記する最小間隔(分)
# 見出し「単勝・複勝 オッズ （18:53 現在）」の時刻。全角/半角のカッコと空白の揺れを許す。
# 最終オッズの見出しは「（最終）」で時刻が入らない= その回は None(取得時刻 t は別に残る)
ASOF_RE = re.compile(r"(\d{1,2}):(\d{2})\s*現在")


def _minutes_between(a, b):
    """'HH:MM' 2 つの差(分)。読めなければ None。日付をまたぐ場合は当日内の前後関係で扱う。"""
    try:
        ha, ma = int(a[:2]), int(a[3:5])
        hb, mb = int(b[:2]), int(b[3:5])
    except (TypeError, ValueError):
        return None
    d = (hb * 60 + mb) - (ha * 60 + ma)
    return d if d >= 0 else d + 24 * 60


TICK_KEY = "track,race_date,race_no,t"   # 同じレースの同じ分は 1 行(表の一意索引 nar_odds_ticks_minute_key と同じ列)


def insert_request(url, key, table, rows, conflict=None):
    """PostgREST の insert の Request。conflict(列名の並び)を与えると、同じ鍵の行が既にあれば 2 本目を捨てる
    (ON CONFLICT DO NOTHING)。便の引き継ぎで前の便の最後の周と次の便の最初の周が同じ分に入ることがあり、
    そのとき 2 本目を作らないため(実測 2026-09-04〜09-10 に 22 組)。"""
    import json as _json
    import urllib.request as _req
    body = _json.dumps(rows, ensure_ascii=False).encode("utf-8")
    path = f"{url}/rest/v1/{table}"
    prefer = "return=minimal"
    if conflict:
        path += f"?on_conflict={conflict}"
        prefer += ",resolution=ignore-duplicates"
    return _req.Request(path, data=body, method="POST",
                        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                                 "Prefer": prefer})


def insert(url, key, table, rows, conflict=None):
    """PostgREST の insert。conflict を与えたのに表に一意索引が無ければ 400 が返るので、その時は今までどおりの
    重複解決なしの insert に落とす(行を失わない)。"""
    import urllib.error as _err
    import urllib.request as _req
    for c in ((conflict, None) if conflict else (None,)):
        try:
            with _req.urlopen(insert_request(url, key, table, rows, c), timeout=60) as r:
                return r.status, ""
        except _err.HTTPError as e:
            msg = e.read().decode()[:300]
            if c and e.code == 400:
                log(f"  {table}: 一意索引が無いので重複解決なしで入れ直す({msg[:80]})")
                continue
            return e.code, msg
        except Exception as e:
            return 0, str(e)
    return 0, "unreachable"


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
    """単複ページ → (runners, is_final, asof)。表が無ければ (None, False, None)。
    asof は見出しに書かれた「HH:MM 現在」(取得時刻とは 1〜7 分ずれる)。"""
    m = re.search(r'<table[^>]*class="[^"]*odd_popular_table_02[^"]*"[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return None, False, None
    table = m.group(1)
    title = re.search(r'class="odd_title"[^>]*>(.*?)</h4>', page, re.S)
    head = _text(title.group(1)) if title else ""
    is_final = "最終" in head
    am = ASOF_RE.search(head)
    asof = f"{int(am.group(1)):02d}:{am.group(2)}" if am else None
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
        return None, is_final, asof
    return (runners or None), is_final, asof


def pick_targets(races, finals, now_min, before, after, limit, min_before=MIN_BEFORE_OFF):
    """min_before を与えると、発走までの残りがそれ以下のレースは外す(= 相手の車線が取る)。"""
    done = {(r.get("track"), int(r.get("race_no"))) for r in finals if r.get("is_final")}
    out = []
    for r in races:
        track, no = r.get("track"), r.get("race_no")
        baba = BABA.get(track)
        pm = post_minutes(r.get("post_time"))
        if baba is None or no is None or pm is None or (track, int(no)) in done:
            continue
        delta = pm - now_min
        if delta > before or delta < -after or delta <= min_before:
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
    ap.add_argument("--min-before", type=int, default=MIN_BEFORE_OFF,
                    help="発走までの残りがこれ以下は取らない(既定=分けない。もう一方の車線に任せるとき用)")
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
    targets = pick_targets(races, finals, now_min, args.before, args.after, args.limit, args.min_before)
    note = f"・残り {args.min_before} 分以下は別の車線" if args.min_before > MIN_BEFORE_OFF else ""
    log(f"{date} 当日のレース {len(races)} / 対象 {len(targets)}(発走 {args.before} 分前〜{args.after} 分後{note}・"
        f"最終済み {sum(1 for r in finals if r.get('is_final'))} は除く)")
    if not targets:
        return 0
    rows, ticks, ok, ng, empty = [], [], 0, 0, 0
    past_lane = 0                    # 取りに行くころには相手の車線へ移っていたレース
    for i, t in enumerate(targets):
        cur = now_minute()
        if out_of_lane(t, args.min_before, cur):
            past_lane += 1
            log(f"  車線の外 {t['track']} {t['race_no']}R(発走まで {t['post'] - cur} 分= もう一方が取る)")
            continue
        if i:
            time.sleep(SLEEP)
        try:
            page = http_get(odds_url(date, t["race_no"], t["baba"])).decode("utf-8", "replace")
        except Exception as e:
            ng += 1
            log(f"  取得失敗 {t['track']} {t['race_no']}R: {type(e).__name__}: {str(e)[:120]}")
            continue
        runners, is_final, asof = parse_odds(page)
        if not runners or all(x["w"] is None for x in runners):
            empty += 1
            log(f"  発売前/表なし {t['track']} {t['race_no']}R(発走まで {t['delta']} 分)")
            continue
        ok += 1
        now_j = dt.datetime.now(JST)
        hhmm = now_j.strftime("%H:%M")
        wmap = {str(x["n"]): x["w"] for x in runners if x["w"] is not None}
        # 2 分刻み(nar_odds_ticks)= 1 巡回 1 行を足すだけ(insert only)
        tick = {"track": t["track"], "race_date": date.isoformat(), "race_no": t["race_no"], "t": hhmm,
                "asof": asof, "f": bool(is_final), "w": wmap,
                "p": {str(x["n"]): [x["pl"], x["ph"]] for x in runners if x["pl"] is not None}}
        ticks.append(tick)
        # 履歴(history)= 約 20 分粒度の約束を守る: 前の記録から 15 分以上あいたとき(または最終の初回)だけ追記。
        # 最大 12 件(超えたら中間を捨て、最初と直近を残す)
        hist = list(hist_by.get((t["track"], t["race_no"]), []))
        last = hist[-1] if hist else None
        gap = _minutes_between(last.get("t") if last else None, hhmm)
        if last is None or gap is None or gap >= HIST_GAP_MIN or (is_final and not last.get("f")):
            entry = {"t": hhmm, "w": wmap}
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
        log(f"  {t['track']} {t['race_no']}R {len(runners)}頭 単勝最低 {low}"
            f" 取得 {hhmm} / ページ {asof or '-'}{' (最終)' if is_final else ''}")
    log(f"取得 成功 {ok} / 失敗 {ng} / 発売前 {empty} / 車線の外 {past_lane} / "
        f"最終化 {sum(1 for r in rows if r['is_final'])}")
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
    # 2 分刻みは別の表へ(insert)。表がまだ無い等で落ちても単複本体は入っているので rc は 0 のまま(ログに残す)
    if ticks:
        st2, msg2 = insert(url, key, TICKS, ticks, conflict=TICK_KEY)
        if st2 >= 300 or st2 == 0:
            log(f"⚠{TICKS} の追記に失敗 status={st2} {msg2}")
        else:
            log(f"追記 {len(ticks)} 行 -> {TICKS}")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    log(f"終了 rc={rc} ({time.time() - t0:.0f}s)")
    sys.exit(rc)
