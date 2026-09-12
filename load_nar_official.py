# -*- coding: utf-8 -*-
"""本体 ステップ2: NAR公式データ(全15場)を Supabase の nar_* テーブルへ投入する。

入力: 他場\\data\\nar_official_csv\\normalized\\*.json(nar_official_csv.py が作った正規化JSON。月次+日次)
出力: nar_races / nar_runs / nar_payouts(supabase-nar-official.sql で作成済みであること)
方式: REST upsert(on_conflict=主キー・merge-duplicates)。何度流しても同じ結果(冪等)。

既定は **ドライラン**(件数と場内訳を表示するだけ)。実際に書き込むには --apply を付ける。
  py -3.12 load_nar_official.py                       # ドライラン(全ファイル)
  py -3.12 load_nar_official.py --since 202501        # 2025-01 以降の月次+日次だけ
  py -3.12 load_nar_official.py --apply               # 書き込み
  py -3.12 load_nar_official.py --apply --only races  # 1テーブルだけ
  py -3.12 load_nar_official.py --from-raw            # 生ZIPを正規化し直して読む(§38 R-0 の遡り投入・通信なし)

⚠ 正規化JSONは作られた時期で持っている列が違う(古いものには race_kind / birth_date / trainer_area が無い)。
  無い列は **送らない**(null で上書きすると入っている値を消すため)。全期間に入れ直すときは --from-raw を使う。

service key は 他場\\.env(SUPABASE_URL / SUPABASE_SERVICE_KEY)から読む。
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

SRC_DIR = Path.home() / "OneDrive" / "デスクトップ" / "他場" / "data" / "nar_official_csv" / "normalized"
RAW_DIR = SRC_DIR.parent / "raw"          # --from-raw のときの入力(生ZIP。読むだけ・絶対に書かない)
ENV_PATH = Path.home() / "OneDrive" / "デスクトップ" / "他場" / ".env"
BATCH = 500
UA = "unified-viewer-loader/1.0"
# 今回の投入で触った行の updated_at(upsert は列を明示しないと更新されないので全行に付ける。
# max(updated_at) が「最後に更新した時刻」=鮮度の監視に使う)
RUN_TS = dt.datetime.now(dt.timezone.utc).isoformat()


def load_env(path=None):
    """接続先の .env を読む。既定は 他場\\.env(現行プロジェクト)。第2プロジェクトへ入れるときは
    --env で pipeline\\.env.nar(SUPABASE_URL / SUPABASE_SERVICE_KEY)を指定する。"""
    p = Path(path) if path else ENV_PATH
    if not p.exists():
        raise SystemExit(f".env が無い: {p}")
    for raw in p.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw and not raw.startswith("#") and "=" in raw:
            k, v = raw.split("=", 1)
            os.environ[k.strip()] = v.strip().strip('"').strip("'")   # 指定 .env を優先(既存環境変数より)


def time_to_sec(raw):
    """公式の生表記を秒へ。'1312'→91.2, '2343'→154.3(ばんえい), '593'→59.3。不明は None。"""
    if raw is None:
        return None
    s = re.sub(r"[^0-9]", "", str(raw))
    if not s:
        return None
    tenth = int(s[-1]); rest = s[:-1]
    if not rest:
        return tenth / 10
    sec = int(rest[-2:]); minute = int(rest[:-2] or 0)
    return minute * 60 + sec + tenth / 10


def to_int(v):
    try:
        return int(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


def to_num(v):
    try:
        return float(v) if v is not None and str(v).strip() != "" else None
    except (TypeError, ValueError):
        return None


# 正規化JSONの版によって「有る/無い」が変わる列。無い版から来た行は、その列を送らない
# (null で上書きすると、生ZIPから入れ直した値を消してしまう)。upsert_all が列の組み合わせごとに分けて送る。
OPTIONAL_RACE_COLS = ("race_kind", "cancelled")          # §27.2 普通/特別/重賞/準重賞・§137 取り止めの印
OPTIONAL_RUN_COLS = ("birth_date", "trainer_area")       # §38 R-0 生年月日/調教師所属


def add_optional(row, src, cols):
    for col in cols:
        if col in src:
            row[col] = blank(src.get(col))
    return row


def conv_race(r, snap):
    row = {
        "track": r.get("track"), "race_date": r.get("race_date"), "race_no": to_int(r.get("race_no")),
        "post_time": r.get("post_time"), "race_name": r.get("race_name"), "surface": r.get("surface"),
        "direction": r.get("direction"), "distance_m": to_int(r.get("distance_m")), "weather": r.get("weather"),
        "going": None if r.get("going") is None else str(r.get("going")), "field_size": to_int(r.get("field_size")),
        "condition": r.get("condition"), "prize_yen": r.get("prize_yen"), "race_last4f": to_num(r.get("race_last4f")),
        "race_last3f": to_num(r.get("race_last3f")), "furlongs": r.get("furlongs"), "corners": r.get("corners"),
        "source": "nar_official_csv", "source_snapshot_hash": snap, "updated_at": RUN_TS,
    }
    add_optional(row, r, OPTIONAL_RACE_COLS)
    return row


def blank(v):
    """空文字は null に(出馬表だけのスナップショットは time_raw/margin が '' で来る)。"""
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def has_result(row):
    """着順か走破タイムか注記(取消/除外等)のどれかが入っていれば「結果あり」。"""
    return row.get("finish") is not None or row.get("time_raw") is not None or row.get("finish_note") is not None


NOTE_RE = re.compile(r"(取消|除外|中止|失格|降着)")


def conv_run(h):
    """公式は 取消/除外/中止/失格 の注記を「着差」列に入れ、日次ではその馬のタイムが '0' で来る
    (2026-08 実測: 出走取消39/競走除外26/競走中止18/失格2)。注記は finish_note に移し、'0' のタイムは null。"""
    finish = to_int(h.get("finish"))
    margin = blank(h.get("margin"))
    note = None
    if finish is None:
        raw = str(h.get("finish") or "").strip()
        note = raw or None
    if margin and NOTE_RE.search(margin):
        note, margin = margin, None
    time_raw = blank(h.get("time_raw"))
    if time_raw is not None and re.fullmatch(r"0+(\.0+)?", time_raw):
        time_raw = None
    row = {
        "track": h.get("track"), "race_date": h.get("race_date"), "race_no": to_int(h.get("race_no")),
        "runner_number": to_int(h.get("runner_number")), "gate": to_int(h.get("gate")),
        "horse_name": h.get("horse_name"), "sex": h.get("sex"), "age": to_int(h.get("age")),
        "jockey": h.get("jockey"), "trainer": h.get("trainer"),
        "carried_weight": to_num(h.get("carried_weight")), "weight_mark": (h.get("weight_mark") or None), "body_weight": to_int(h.get("body_weight")),
        "body_weight_change": to_int(h.get("body_weight_change")), "finish": finish, "finish_note": note,
        "time_raw": time_raw, "time_sec": time_to_sec(time_raw), "margin": margin,
        "last3f": to_num(h.get("last3f")), "popularity": to_int(h.get("popularity")), "updated_at": RUN_TS,
    }
    # §38 R-0(2026-08-27): 公式CSVの 生年月日/調教師所属。正規化が2列を持っていないとき(= 2026-08-27 より前に
    # 作られた正規化JSON)は **列ごと送らない**。null で送ると既に入っている値を消してしまうため(§27.2 race_kind も同じ)
    add_optional(row, h, OPTIONAL_RUN_COLS)
    return row


def conv_payout(p):
    return {
        "track": p.get("track"), "race_date": p.get("race_date"), "race_no": to_int(p.get("race_no")),
        "ticket_type": p.get("ticket_type"), "combination": str(p.get("combination")),
        "payout_per_100": to_int(p.get("official_payout_per_100")), "popularity": to_int(p.get("popularity")),
        "settlement_quality": p.get("settlement_quality"), "source_snapshot_hash": p.get("source_snapshot_hash"),
        "updated_at": RUN_TS,
    }


KEYS = {
    "races": ("nar_races", "track,race_date,race_no"),
    "runs": ("nar_runs", "track,race_date,race_no,runner_number"),
    # 2026-08-23: 払戻は 1レース1行の JSON(nar_race_payouts)。63万行→5.8万行で DB 170MB 減
    "payouts": ("nar_race_payouts", "track,race_date,race_no"),
    # 同日: 血統・馬主・生産者は馬テーブル(nar_horses)へ分離(nar_runs から列を削除)
    "horses": ("nar_horses", "horse_name"),
}
TABLES = ("races", "runs", "payouts", "horses")
HORSE_ATTRS = ("sex", "sire", "dam", "broodmare_sire", "owner", "breeder")


def conv_horse(h):
    return {"horse_name": h.get("horse_name"), **{k: blank(h.get(k)) for k in HORSE_ATTRS}, "updated_at": RUN_TS}


def group_payouts(rows):
    """組番ごとの行 {key: row} を 1レース1行 {(場,日,R): {..., payouts:[{t,c,y,p},...]}} に畳む。"""
    out = {}
    for row in rows.values():
        k = (row["track"], row["race_date"], row["race_no"])
        r = out.get(k)
        if r is None:
            r = out[k] = {"track": row["track"], "race_date": row["race_date"], "race_no": row["race_no"],
                          "payouts": [], "source_snapshot_hash": row.get("source_snapshot_hash"), "updated_at": RUN_TS}
        r["payouts"].append({"t": row["ticket_type"], "c": row["combination"], "y": row["payout_per_100"], "p": row["popularity"]})
    for r in out.values():
        r["payouts"].sort(key=lambda p: (p["t"], p["c"]))
    return out


def upsert(url, key, table, conflict, rows):
    body = json.dumps(rows, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        f"{url}/rest/v1/{table}?on_conflict={conflict}", data=body, method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "Prefer": "resolution=merge-duplicates,return=minimal", "User-Agent": UA})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status, ""
        except urllib.error.HTTPError as e:
            msg = e.read().decode()[:300]
            if e.code >= 500 and attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            return e.code, msg
        except Exception as e:
            if attempt < 2:
                time.sleep(3 * (attempt + 1)); continue
            return 0, str(e)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="実際に書き込む(既定はドライラン)")
    ap.add_argument("--since", help="YYYYMM。この月以降のファイルだけ")
    ap.add_argument("--only", choices=["races", "runs", "payouts", "horses"], help="1テーブルだけ")
    ap.add_argument("--env", help="接続先 .env のパス(既定: 他場\\.env。第2プロジェクトは pipeline\\.env.nar)")
    ap.add_argument("--batch", type=int, default=BATCH, help="1リクエストの行数(既定500)")
    ap.add_argument("--from-raw", action="store_true",
                    help="正規化JSONではなく生ZIP(raw\\*.zip)をその場で正規化し直して読む(§38 R-0 の遡り投入用・通信なし)")
    args = ap.parse_args()

    # OneDrive の競合コピー(…-DESKTOP-XXX.json)や同期途中のファイルを拾わない
    src_dir, pattern, name_re = ((RAW_DIR, "*_race_*.zip", RAW_FILE_RE) if args.from_raw
                                 else (SRC_DIR, "*_race_*.json", FILE_RE))
    files = [f for f in sorted(src_dir.glob(pattern)) if name_re.fullmatch(f.name)]
    if args.since:
        files = [f for f in files if f.name[:6] >= args.since]
    if not files:
        print(f"入力が無い: {src_dir}"); return 2
    files = latest_per_stamp(files)

    docs = []
    for f in files:
        try:
            d = renormalize(f) if args.from_raw else json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:          # 1本壊れていても他を止めない(同期途中など)
            print(f"  ! 読めないので飛ばす: {f.name} ({type(e).__name__})"); continue
        docs.append((d.get("source_observed_at") or "", f.name, d))
    if not docs:
        print("読める入力が無い"); return 2
    dedup, stats = build_dedup(docs)
    if stats["kept_stale"]:
        print(f"  (結果なしの古い行で上書きしなかった件数: {stats['kept_stale']:,})")

    print(f"入力ファイル {len(docs)} 本(スタンプごとに最新だけ) ({files[0].name[:8]} .. {files[-1].name[:8]})")
    print(summarize(dedup, stats))

    if not args.apply:
        print("ドライラン(書き込みなし)。--apply で投入。"); return 0

    load_env(args.env)
    url = os.environ.get("SUPABASE_URL", "").rstrip("/"); key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_KEY が無い"); return 2
    print(f"接続先: {url}")
    return upsert_all(url, key, dedup, batch=args.batch, only=args.only)


FILE_RE = re.compile(r"\d{6}(\d{2})?_race_[0-9a-f]{16}\.json")
RAW_FILE_RE = re.compile(r"\d{6}(\d{2})?_race_[0-9a-f]{16}\.zip")
OBS_RE = re.compile(r'"source_observed_at":\s*"([^"]+)"')


def renormalize(zip_path):
    """生ZIPをその場で正規化する(§38 R-0)。既に保存済みの正規化JSONは**触らない**(不変アーカイブ)。
    2026-08-27 より前に作られた正規化JSONには 生年月日/調教師所属(と §27.2 race_kind)が入っていないので、
    遡って投入したいときはこちらで読む。ZIPの中身から作るので source_snapshot_hash はファイル名と同じ値になる。"""
    cloud_dir = str(Path(__file__).resolve().parent.parent / "cloud")
    if cloud_dir not in sys.path:
        sys.path.insert(0, cloud_dir)
    from nar_official_csv import normalize_archive        # noqa: PLC0415(遡り投入のときだけ要る)
    stamp = zip_path.name.split("_", 1)[0]
    observed = peek_observed(zip_path) or dt.datetime.fromtimestamp(
        zip_path.stat().st_mtime, dt.timezone.utc).isoformat()
    return normalize_archive(
        zip_path.read_bytes(), kind="race", scope=("monthly" if len(stamp) == 6 else "daily"),
        source_url=zip_path.as_uri(), observed_at=observed,
    )


def peek_observed(path):
    """JSON 先頭だけ読んで観測時刻を取る(全文パースしない)。無ければ空文字。
    生ZIP のときは同名の正規化JSONから借りる(取り込み順が正規化JSON経由と同じになるように)。"""
    if path.suffix.lower() == ".zip":
        path = SRC_DIR / f"{path.stem}.json"
    try:
        with path.open("r", encoding="utf-8") as f:
            head = f.read(2000)
    except OSError:
        return ""
    m = OBS_RE.search(head)
    return m.group(1) if m else ""


def latest_per_stamp(files):
    """スタンプ(日次=YYYYMMDD / 月次=YYYYMM)ごとに観測時刻が最新の1本だけ残す。
    スナップショットは累積(後のものが前のものを含む)なので十分。開催中は日次が15分ごとに増えるため
    全部読むと月末に 1GB 級になる(2026-08-23 レビュー指摘)。"""
    latest = {}
    for f in files:
        stamp = f.name.split("_", 1)[0]
        obs = peek_observed(f)
        if stamp not in latest or (obs, f.name) > latest[stamp][:2]:
            latest[stamp] = (obs, f.name, f)
    return [v[2] for v in sorted(latest.values(), key=lambda v: v[1])]


def build_dedup(docs):
    """正規化文書の列 [(observed_at, name, document), ...] を3表の行辞書にまとめる。

    同じ(場,日,R)が複数文書にある場合の優先順位(2026-08-23 修正):
      ファイル名順は使わない(月次 '202608_' は日次 '20260820_' より後に並び、しかも月次スナップショットは
      ハッシュ順で新旧が入れ替わる → 結果入りの行が「出馬表だけ」の古い月次で上書きされ 8/13〜8/20 の結果が消えた)。
      → 文書の source_observed_at(観測時刻)順に処理し、さらに「結果あり」の行は「結果なし」の行で上書きしない。
    戻り値: (dedup, stats)。dedup = {"races": {key: row}, "runs": {...}, "payouts": {...}}
    """
    docs = sorted(docs, key=lambda x: (x[0] or "", x[1]))
    dedup = {"races": {}, "runs": {}, "payouts": {}, "horses": {}}
    pay_rows = {}                        # 組番ごと(最新が勝つ)→ 最後に 1レース1行へ畳む
    horse_seen = {}                      # 馬名 -> 最後に見た (race_date, race_no)。新しい走の属性で上書き
    race_done = {}                       # (場,日,R) -> その行が「結果あり」のスナップショット由来か
    per_track = collections.Counter()
    kept_stale = 0
    for _, _, d in docs:
        snap = d.get("source_snapshot_hash")
        horses = d.get("horses") or []
        done_here = {(h.get("track"), h.get("race_date"), to_int(h.get("race_no")))
                     for h in horses if to_int(h.get("finish")) is not None}
        for r in d.get("races") or []:
            row = conv_race(r, snap); k = (row["track"], row["race_date"], row["race_no"])
            if k not in dedup["races"]:
                per_track[row["track"]] += 1
            elif race_done.get(k) and k not in done_here:
                kept_stale += 1; continue     # 結果入りの行を出馬表だけの行で戻さない
            dedup["races"][k] = row; race_done[k] = k in done_here
        for h in horses:
            row = conv_run(h); k = (row["track"], row["race_date"], row["race_no"], row["runner_number"])
            name = blank(h.get("horse_name"))
            if name:
                when = (row["race_date"] or "", row["race_no"] or 0)
                if name not in horse_seen or when >= horse_seen[name]:
                    horse_seen[name] = when; dedup["horses"][name] = conv_horse(h)
            old = dedup["runs"].get(k)
            if old is not None and has_result(old) and not has_result(row):
                kept_stale += 1; continue
            dedup["runs"][k] = row
        for p in d.get("payouts") or []:
            row = conv_payout(p)
            if row["payout_per_100"] is None:
                continue
            pay_rows[(row["track"], row["race_date"], row["race_no"], row["ticket_type"], row["combination"])] = row
    dedup["payouts"] = group_payouts(pay_rows)
    return dedup, {"per_track": per_track, "kept_stale": kept_stale, "payout_entries": len(pay_rows)}


def summarize(dedup, stats):
    lines = [f"  {k:8s} {len(dedup[k]):>8,} 行" for k in TABLES]
    lines.append(f"  (払戻の組番数 {stats.get('payout_entries', 0):,})")
    for name, table, cols in (("走", "runs", OPTIONAL_RUN_COLS), ("レース", "races", OPTIONAL_RACE_COLS)):
        rows = dedup[table].values()
        for col in cols:                              # 列を送る行数と、そのうち値の入っている行数
            sent = sum(1 for r in rows if col in r)
            filled = sum(1 for r in rows if r.get(col) is not None)
            lines.append(f"  {name}.{col}: 送る {sent:,} 行(値あり {filled:,}) / 送らない(古い正規化JSON) {len(dedup[table]) - sent:,} 行")
    lines.append("  レース数の場内訳: " + str(dict(sorted(stats["per_track"].items(), key=lambda x: -x[1]))))
    dates = sorted({k[1] for k in dedup["races"]})
    lines.append(f"  期間: {dates[0]} .. {dates[-1]}" if dates else "  期間: -")
    return "\n".join(lines)


def by_columns(rows):
    """列の組み合わせごとに分ける。PostgREST の一括 upsert は1リクエスト内で行ごとに列を変えられないため
    (行数の多い組み合わせから送る)。任意列が無い版の正規化JSONが混ざっても、その列を null で上書きしない。"""
    groups = {}
    for row in rows:
        groups.setdefault(tuple(sorted(row)), []).append(row)
    return sorted(groups.values(), key=lambda g: -len(g))


def upsert_all(url, key, dedup, batch=BATCH, only=None, log=print):
    """3表を順に upsert。成功 0 / 失敗 1(失敗した表で止まる。何度流しても同じ結果)。"""
    batch = max(50, int(batch or BATCH))
    for k in TABLES:
        if only and only != k:
            continue
        table, conflict = KEYS[k]
        rows = list(dedup[k].values()); done = 0; t0 = time.time()
        groups = by_columns(rows)
        if len(groups) > 1:
            log(f"  {table}: 列の組み合わせ {len(groups)} 通り " + " / ".join(f"{len(g):,}行" for g in groups))
        for group in groups:
            for i in range(0, len(group), batch):
                st, err = upsert(url, key, table, conflict, group[i:i + batch])
                if st >= 300 or st == 0:
                    log(f"  {table}: HTTP{st} {err} (batch {i})"); return 1
                done += len(group[i:i + batch])
                if done % 5000 < batch:
                    log(f"  {table}: {done:,}/{len(rows):,} ({time.time() - t0:.0f}s)")
        log(f"  {table}: 完了 {done:,} 行 ({time.time() - t0:.0f}s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
