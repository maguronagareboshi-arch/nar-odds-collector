# -*- coding: utf-8 -*-
"""当日の確定結果と払戻を、地方競馬公式の日次 ZIP(keiba.go.jp DataDownload)から読んで Supabase へ upsert する。
2 分おきのループ(loop.py)から呼ばれる。公式 ZIP には発走の 6〜9 分後に結果と払戻が載る(2026-09-05 実測・7 レース)。

  python results_today.py                  # 今日(JST)。結果の入ったレースの集合が前回と同じなら何もしない
  python results_today.py --dry-run        # 取得と数えるだけ(書かない)
  python results_today.py --env <.env>     # ローカル試験(既定は環境変数 SUPABASE_URL / SUPABASE_SERVICE_KEY)
  python results_today.py --force          # 前回と同じでも書く

⛔読み方(nar_official_csv.py)と書き方(load_nar_official.py)は本体と**同じファイル**の写し。直すときは両方を同じ内容に。
⛔書くのは「結果の入ったレース」の行だけ(nar_races / nar_runs / nar_race_payouts)。出馬表だけのレースと
  馬テーブル(nar_horses)は本体の 20 分おきの便に任せる(同じ写し方なので競合しない・冪等)。
⛔取り止めのレースだけは例外で、着順が無くても **nar_races の 1 行だけ**書く(印を当日のうちに入れるため)。
⛔ZIP のバイト列は毎回変わる(中にタイムスタンプ)ので、ハッシュでなく「結果の入ったレースの集合+払戻の行数」で前回と比べる。
終了コード: 0 正常(取得できない・開催なしも 0= 次の周に任せる)/ 1 書き込み失敗
"""
import argparse
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import urllib.request

from nar_official_csv import UA, digest_bytes, download_url, normalize_archive
from load_nar_official import build_dedup, load_env, upsert_all

JST = dt.timezone(dt.timedelta(hours=9))
STATE = Path(".results_state.json")      # ジョブの作業フォルダに置く(同じジョブの中だけ持ち越す)


def download_archive(url, timeout=90):
    """nar_official_csv.download_archive と同じ判定を urllib で(⛔このリポは標準ライブラリだけ= requests が無い。
    2026-09-05 実測: クラウドで ModuleNotFoundError)。ZIP でなければ ValueError。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/zip,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        payload = r.read()
        final = str(r.geturl())
    if len(payload) < 4 or payload[:2] != b"PK":
        raise ValueError("NAR download did not return a ZIP archive")
    return payload, final


def log(msg):
    print(f"[results {dt.datetime.now(JST):%H:%M:%S}] {msg}", flush=True)


def rows_to_write(dedup, fin, off):
    """書く行を選ぶ。fin(着順の入ったレース)は 3 表とも、off(取り止めのレース)は **races だけ**。

    取り止めの回には着順が付かないので fin に入らない。ここで足さないと当日は 1 行も書かれず、
    印が入るのは翌朝の便だけになる。runs は「出馬表だけの行」なので今までどおり本体の便に任せる。
    ⛔馬テーブル(horses)は書かない。fin / off = {(競馬場, レース番号)}。
    dedup の鍵 = (競馬場, 競走年月日, レース番号[, 馬番])。
    """
    want_races = set(fin) | set(off)
    out = {}
    for table, rows in dedup.items():
        if table == "horses":
            continue
        want = want_races if table == "races" else set(fin)
        out[table] = {k: v for k, v in rows.items() if (k[0], int(k[2])) in want}
    out["horses"] = {}
    return out


def read_state():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--env")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--date", help="JST YYYY-MM-DD(既定は今日)")
    a = ap.parse_args()
    if a.env:
        load_env(a.env)
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not a.dry_run and (not url or not key):
        log("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い"); return 0
    today = a.date or dt.datetime.now(JST).date().isoformat()

    t0 = time.time()
    try:
        payload, final = download_archive(download_url("race", scope="daily", race_date=today))
        doc = normalize_archive(payload, kind="race", scope="daily", source_url=final,
                                observed_at=dt.datetime.now(dt.timezone.utc).isoformat())
    except ValueError as e:                       # ZIP でない応答= 開催なし or エラーページ
        log(f"公式が ZIP を返さない({str(e)[:80]})"); return 0
    except Exception as e:
        log(f"取得失敗 {type(e).__name__}: {str(e)[:120]}"); return 0

    races = doc.get("races") or []
    if today not in {r.get("race_date") for r in races}:
        log("当日のレースが無い"); return 0
    horses = doc.get("horses") or []
    fin = sorted({(h["track"], int(h["race_no"])) for h in horses if str(h.get("finish") or "").strip()})
    pays = len(doc.get("payouts") or [])
    # 取り止めのレース(公式 payback の「組番なし・返還」か「全部空」)。着順も払戻も増えないので、
    # ⛔ここを合図に入れないと「変化なし」で弾かれ、当日は印が入らない
    off = sorted({(r["track"], int(r["race_no"])) for r in races
                  if r.get("cancelled") and r.get("race_date") == today})
    sig = {"date": today, "fin": [list(k) for k in fin], "pays": pays, "off": [list(k) for k in off]}
    prev = read_state()
    if (not a.force and prev.get("date") == today and prev.get("fin") == sig["fin"]
            and prev.get("pays") == pays and (prev.get("off") or []) == sig["off"]):
        log(f"変化なし(結果あり {len(fin)}R・取り止め {len(off)}R・払戻 {pays} 行・{time.time() - t0:.1f}s)"); return 0
    new = [k for k in fin if list(k) not in (prev.get("fin") or [])] if prev.get("date") == today else fin
    log(f"取得 {digest_bytes(payload)[:8]} レース {len(races)}・結果あり {len(fin)}R(新着 {len(new)})・払戻 {pays} 行")
    if not fin and not off:
        STATE.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8"); return 0

    dedup, stats = build_dedup([(doc.get("source_observed_at") or "", "daily", doc)])
    out = rows_to_write(dedup, fin, off)
    n = {t: len(v) for t, v in out.items()}
    log(f"書く行= races {n['races']} / runs {n['runs']} / payouts {n['payouts']}"
        + (f"・取り止め {len(off)}" if off else "") + (f"・新着 {new}" if new else ""))
    if a.dry_run:
        return 0
    rc = upsert_all(url, key, out, batch=1000, log=log)
    if rc:
        log("書き込み失敗(次の周で取り直す)"); return 1
    STATE.write_text(json.dumps(sig, ensure_ascii=False), encoding="utf-8")
    log(f"完了 {time.time() - t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
