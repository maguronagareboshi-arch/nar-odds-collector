# -*- coding: utf-8 -*-
"""5重勝(オッズパークのセレクト5重勝・競馬)の対象 5 レースの 3連単・馬単を、1 本目の発走 45〜25 分前に写す(2026-11-22)。

なぜ: 5重勝の当たり口数を「勝ち馬の売れ方」から見積もると、30 分前は単勝より 3連単の方がぶれが小さい(0.76 対 1.30)。
  ふだんの車線(far/hot)は各レースの発走 40 分前からしか組のオッズを取らない= 5重勝を買う時刻(1 本目の前)に
  後ろの 4 レースの値が無い。ここはその穴だけを埋める。
何をするか: ① オッズパークの 5重勝の一覧(JResultList.do・1 日 1 回・手元に覚える)から場と 5 レースを読む
  ② 1 本目の発走まで 45〜25 分の場だけ、5 レースの 3連単・馬単のページを取り ③ nar_odds_full_ticks に積む。
  ⛔nar_odds_full(最新 1 行・画面が読む)は書かない。⛔単勝・複勝は朝の車線が取っている(ここでは取らない)。
負荷: 1 場 5 レース × 2 ページ × 窓の中の周回(10 分ごとで 2〜3 回)。1 日 5 場で 150 回ほど。

  python odds_w5.py [--dry-run] [--env .env] [--at HHMM]   # --at は試験用(その時刻として窓を判定)
loop.py の w5 車線が 600 秒ごとに呼ぶ(--before などの車線の引数は受けて捨てる)。
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import odds_full as of

LOTO_URL = "https://www.oddspark.com/loto/JResultList.do?kaisaiBi="
# オッズパークの場の番号 → 公式・nar_races の場名(5重勝のある場だけ)
JO = {"03": "帯広ば", "11": "盛岡", "12": "水沢", "42": "笠松", "51": "園田", "52": "姫路", "61": "佐賀"}
W5_PAGES = [("Odds3LenTan", ("sanrentan",)), ("OddsUmLenTan", ("umatan",))]
WIN_FROM = 45      # 1 本目の発走の何分前から
WIN_TO = 25        # 何分前まで
CACHE = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())


def parse_loto(html):
    """一覧の頁 → [{jo, races}](競馬の 5 レースの組だけ)。⛔ばんえい・競馬の印は見出しの ico-horse。"""
    out = []
    for hd, tb in re.findall(r'<h3>(.*?)</h3>\s*<table class="tb70 w100pr">(.*?)</table>', html, re.S):
        rn = re.findall(r'joCode=(\d+)&amp;raceNo=(\d+)', tb)
        if "ico-horse" not in hd or len(rn) != 5:
            continue
        out.append({"jo": rn[0][0], "races": [int(r) for _, r in rn]})
    return out


def cards_of(day):
    """その日の 5重勝の組。手元に覚える(空なら覚えずに次の周で読み直す)。"""
    p = CACHE / f"w5_cards_{day:%Y%m%d}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    html = of.http_get(LOTO_URL + f"{day:%Y%m%d}", {"User-Agent": "Mozilla/5.0"}).decode("cp932", "ignore")
    cards = []
    for c in parse_loto(html):
        tr = JO.get(c["jo"])
        if tr is None:
            of.log(f"  場の番号 {c['jo']} は知らない= 飛ばす")
            continue
        cards.append({"track": tr, "races": c["races"]})
    if cards:
        p.write_text(json.dumps(cards, ensure_ascii=False), encoding="utf-8")
    return cards


def in_window(cards, races, now_min, wfrom=WIN_FROM, wto=WIN_TO):
    """1 本目の発走まで wfrom〜wto 分の組 → [(組, {R: 発走の分})]。発走時刻の分からない組は外す。"""
    post = {(r.get("track"), int(r["race_no"])): of.post_minutes(r.get("post_time"))
            for r in races if r.get("race_no") is not None}
    out = []
    for c in cards:
        pm = {n: post.get((c["track"], n)) for n in c["races"]}
        first = pm.get(c["races"][0])
        if first is None:
            continue
        if wto <= first - now_min <= wfrom:
            out.append((c, pm))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--env")
    ap.add_argument("--at", help="試験用: この時刻(HHMM)として窓を判定")
    for x in ("--before", "--after", "--limit", "--min-before"):   # 車線の引数(使わない)
        ap.add_argument(x)
    a = ap.parse_args()
    if a.env:
        of.load_env(a.env)
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        of.log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い")
        return 2
    now = dt.datetime.now(of.JST)
    now_min = int(a.at[:2]) * 60 + int(a.at[2:]) if a.at else now.hour * 60 + now.minute
    try:
        cards = cards_of(now.date())
    except Exception as e:
        of.log(f"5重勝の一覧が読めない(次の周で読み直す): {type(e).__name__}: {str(e)[:120]}")
        return 0
    try:
        races = of.sb_get(url, key, f"nar_races?select=track,race_no,post_time&race_date=eq.{now.date().isoformat()}")
    except Exception as e:
        of.log(f"nar_races の読み取りに失敗: {type(e).__name__}: {str(e)[:200]}")
        return 2
    hits = in_window(cards, races, now_min)
    names = " / ".join(f"{c['track']} {c['races'][0]}〜{c['races'][-1]}R" for c in cards)
    of.log(f"{now.date()} 5重勝 {len(cards)} 組({names})・窓の中 {len(hits)} 組")
    if not hits:
        return 0
    # 前の周の指紋(同じ中身は積まない)= 手元に覚える
    hp = CACHE / f"w5_h_{now.date():%Y%m%d}.json"
    saved = json.loads(hp.read_text(encoding="utf-8")) if hp.exists() else {}
    prev = {}
    for k, v in saved.items():
        tr, n, kind = k.split("|")
        prev[(tr, int(n), kind)] = v
    ticks = []
    for c, pm in hits:
        for i, n in enumerate(c["races"]):
            if i:
                time.sleep(of.SLEEP)
            t = {"track": c["track"], "race_no": n, "baba": of.BABA[c["track"]], "post": pm.get(n)}
            _, st, head, _, tk = of.fetch_race(now.date(), t, prev_h=prev, pages=W5_PAGES)
            of.log(f"  {c['track']} {n}R 発走まで {None if pm.get(n) is None else pm[n] - now_min} 分・{head} 頭・"
                   f"取れた {st['ok']} / 発売前 {st['empty']} / 失敗 {st['ng']} / 検算落ち {st['reject']} / 同じ {st['same']}")
            ticks.extend(tk)
    of.log(f"全点 +{len(ticks)} 行")
    if a.dry_run or not ticks:
        if a.dry_run:
            of.log("dry-run: 投入しない")
        return 0
    status, msg = of.insert(url, key, of.FULL_TICKS, ticks)
    if status == 409:
        of.log(f"⚠同じ時刻の行が既にある(二重には入らない): {msg[:120]}")
    elif status >= 300 or status == 0:
        of.log(f"投入失敗 status={status} {msg}")
        return 1
    for tk in ticks:
        prev[(tk["track"], tk["race_no"], tk["kind"])] = tk["h"]
    hp.write_text(json.dumps({f"{k[0]}|{k[1]}|{k[2]}": v for k, v in prev.items()}, ensure_ascii=False), encoding="utf-8")
    of.log(f"投入 {len(ticks)} 行 -> {of.FULL_TICKS}")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    of.log(f"終了 rc={rc} ({time.time() - t0:.0f}s)")
    sys.exit(rc)
