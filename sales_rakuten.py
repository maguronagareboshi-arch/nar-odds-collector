# -*- coding: utf-8 -*-
"""当日の券種別「総票数」を払戻ページから読み、Supabase の表 nar_sales へ保存する。標準ライブラリだけ。

締切前の票数はどこにも公開されていない(実測)。出るのはレースが終わったあとで、しかも払戻より
だいぶ遅れて、その日の早いレースから順に入る。一度出た数字はもう動かない(実測)ので、
開催中に読んでおけば、後のレースを見るときに「その日の実際の売れ方」が分かる。

  python sales_rakuten.py              # 今日(JST)・読み頃の場を 1 つ
  python sales_rakuten.py --dry-run    # 取得と解析だけ(投入しない)
環境変数: SUPABASE_URL / SUPABASE_SERVICE_KEY。COLLECTOR_UA で User-Agent を上書きできる。
終了コード: 0 正常(対象なし・取得失敗も 0=次の回に任せる)/ 1 投入失敗 / 2 前提の読み取りに失敗

⛔取得元の robots.txt が Crawl-Delay: 60 を出している= 続けて取らない(既定 60 秒以上あける・
   同じ場は 30 分に 1 度まで)。間隔の記録は .state_sales.json(リポジトリには入れない)。
⛔総票数が 0 か空のレースは書かない(未確定・中止)。
⛔最終レースから時間が経った場は読まない(後のレースがもう無いので、当日読む意味が無い)。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

from odds_full import BABA, JST, http_get, load_env, log, post_minutes, sb_get, upsert

BASE = "https://keiba.rakuten.co.jp/race_dividend/list/RACEID"
TABLE = "nar_sales"
CONFLICT = "track,race_date,race_no"
STATE = Path(__file__).resolve().parent / ".state_sales.json"
MIN_GAP_SEC = 60        # 取得元の robots.txt の Crawl-Delay
PER_TRACK_MIN = 30      # 同じ場を読み直す最小間隔(分)
AFTER_LAST_MIN = 120    # 最終レースからこれを過ぎた場は読まない(分)

# ページの券種名 → 列名(9 券種)
TICKETS = {"単勝": "win", "複勝": "place", "枠複": "bracket_quinella", "枠単": "bracket_exacta",
           "馬複": "quinella", "馬単": "exacta", "ワイド": "wide", "三連複": "trio", "三連単": "trifecta"}
VOTE_RE = re.compile(r'<th scope="row">([^<]+)</th>\s*<td>([\d,]+)</td>')


# ---------------------------------------------------------------- 取得の間隔(記録)

def state_load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def state_save(st):
    try:
        STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log(f"  ⚠間隔の記録を書けない({type(e).__name__})= 次の回は間隔を守れないかもしれない")


def wait_gap(st, gap):
    """前回の取得から gap 秒あくまで待つ(記録が無ければ待たない)。"""
    last = st.get("last_http")
    if not last:
        return
    rest = gap - (time.time() - float(last))
    if rest > 0:
        log(f"  取得の間隔をあける: {rest:.0f}秒")
        time.sleep(rest)


# ---------------------------------------------------------------- ページの解析

def _votes(block):
    """「総票数」「返還票数」の小さい表 → ({列名: 票数}, 読めなかった券種名)"""
    out, dropped = {}, []
    for name, num in VOTE_RE.findall(block):
        col = TICKETS.get(name.strip())
        if col:
            out[col] = int(num.replace(",", ""))
        else:
            dropped.append(name.strip())
    return out, dropped


def parse_day(page):
    """1 日ぶんのページ → {レース番号: {'votes': {...}, 'refunds': {...}}}。票数の無いレースは入らない。"""
    out = {}
    parts = re.split(r'<h3 class="headline"><span>■</span>(\d+)R', page)
    for i in range(1, len(parts) - 1, 2):
        no = int(parts[i])
        m = re.search(r"総票数([\s\S]*?)返還票数([\s\S]*?)</table>\s*</td>", parts[i + 1])
        if not m:
            continue
        votes, d1 = _votes(m.group(1))
        refunds, d2 = _votes(m.group(2))
        if d1 or d2:
            log(f"  ⚠{no}R: 読めない券種名 {sorted(set(d1 + d2))}(その券種だけ落ちる)")
        if votes and sum(votes.values()) > 0:
            out[no] = {"votes": {k: votes.get(k, 0) for k in TICKETS.values()},
                       "refunds": {k: refunds.get(k, 0) for k in TICKETS.values()}}
    return out


def fetch_day(date, track):
    """その日その場の全レースが 1 ページで返る。⛔題名に場名と日付が無ければ捨てる(場コード違いを飲まない)。"""
    used = f"{BASE}/{date.strftime('%Y%m%d')}{int(BABA[track]):02d}00000001"
    page = http_get(used).decode("utf-8", "replace")
    m = re.search(r"<title>([^<]+)</title>", page)
    title = m.group(1) if m else ""
    if track not in title or date.strftime("%Y/%m/%d") not in title:
        raise RuntimeError(f"題名が合わない(場コード疑い): {title[:60]}")
    return parse_day(page), used


# ---------------------------------------------------------------- 読む場を選ぶ

def pick_tracks(races, have, st, now_min, now_epoch, limit):
    """当日の場から「読み頃」を選ぶ。返り値は (選んだ場, 見送りの理由)。

    読み頃 = 最終レースから AFTER_LAST_MIN 以内 / 全レース保存済みでない /
             前に読んでから PER_TRACK_MIN 分以上たっている。待たせた順に並べる。
    """
    by = {}
    for r in races:
        track, no = r.get("track"), r.get("race_no")
        pm = post_minutes(r.get("post_time"))
        if track in BABA and no is not None and pm is not None:
            by.setdefault(track, []).append((int(no), pm))
    out, skip = [], []
    for track, rs in by.items():
        last_post = max(pm for _, pm in rs)
        if now_min - last_post > AFTER_LAST_MIN:
            skip.append(f"{track}=最終から{now_min - last_post}分")
            continue
        if len(have.get(track, set())) >= len(rs):
            skip.append(f"{track}=全{len(rs)}レース保存済み")
            continue
        ago = (now_epoch - float(st.get("tracks", {}).get(track, 0))) / 60.0
        if ago < PER_TRACK_MIN:
            skip.append(f"{track}=前回から{ago:.0f}分")
            continue
        out.append((ago, track))
    out.sort(reverse=True)
    return [t for _, t in out][:limit], skip


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env")
    ap.add_argument("--url")
    ap.add_argument("--key")
    ap.add_argument("--date")
    ap.add_argument("--max-tracks", type=int, default=1, help="1 回に読む場の数(既定 1)")
    ap.add_argument("--gap", type=int, default=MIN_GAP_SEC, help="取得の最小間隔(秒)")
    a = ap.parse_args()
    if a.env:
        load_env(a.env)
    url = (a.url or os.environ.get("SUPABASE_URL", "")).rstrip("/")
    key = a.key or os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い")
        return 2
    now = dt.datetime.now(JST)
    date = dt.date.fromisoformat(a.date) if a.date else now.date()
    iso = date.isoformat()
    try:
        races = sb_get(url, key, f"nar_races?select=track,race_no,post_time&race_date=eq.{iso}"
                                 "&order=track.asc,race_no.asc")
        saved = sb_get(url, key, f"{TABLE}?select=track,race_no&race_date=eq.{iso}")
    except Exception as e:
        log(f"nar_races / {TABLE} の読み取りに失敗: {type(e).__name__}: {str(e)[:200]}")
        return 2
    have = {}
    for r in saved:
        if r.get("race_no") is not None:
            have.setdefault(r.get("track"), set()).add(int(r["race_no"]))
    st = state_load()
    picked, skip = pick_tracks(races, have, st, now.hour * 60 + now.minute, time.time(), a.max_tracks)
    log(f"{iso} 当日のレース {len(races)} / 保存済み {sum(len(v) for v in have.values())} レース / "
        f"読む場 {picked if picked else 'なし'}" + (f"(見送り: {', '.join(skip)})" if skip else ""))
    if not picked:
        return 0
    rows = []
    for track in picked:
        wait_gap(st, a.gap)
        try:
            day, used = fetch_day(date, track)
        except Exception as e:
            st["last_http"] = time.time()
            state_save(st)
            log(f"  取得失敗 {track}: {type(e).__name__}: {str(e)[:140]}")
            continue
        st["last_http"] = time.time()
        st.setdefault("tracks", {})[track] = time.time()
        state_save(st)
        fresh = sorted(no for no in day if no not in have.get(track, set()))
        log(f"  {track}: 票数のあるレース {sorted(day)} / このうち未保存 {fresh if fresh else 'なし'}  {used}")
        for no in fresh:
            v = day[no]
            rows.append({"track": track, "race_date": iso, "race_no": no,
                         "votes": v["votes"], "refunds": v["refunds"],
                         "updated_at": dt.datetime.now(dt.timezone.utc).isoformat()})
            log(f"    {no}R 単勝 {v['votes'].get('win', 0):,} 票 / 9 券種の合計 {sum(v['votes'].values()):,} 票")
    if a.dry_run:
        log(f"dry-run: 投入しない({len(rows)} 行)")
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
    code = main()
    log(f"終了 rc={code} ({time.time() - t0:.0f}s)")
    sys.exit(code)
