# -*- coding: utf-8 -*-
"""地方競馬公式サイト(keiba.go.jp TodayRaceInfo)の当日オッズ(枠連複/枠連単/馬連/馬単/ワイド/3連複/3連単)を
Supabase の表 nar_odds_full へ保存する収集ジョブ。標準ライブラリだけで動く。

  python odds_full.py            # 今日(JST)の「発走 60 分前〜10 分後」のレース・最大10レース
  python odds_full.py --dry-run  # 取得と解析だけ(投入しない)
環境変数: SUPABASE_URL / SUPABASE_SERVICE_KEY(GitHub Secrets)。COLLECTOR_UA で User-Agent を上書きできる。
終了コード: 0 正常(対象なし・一部の取得失敗も 0=次の実行に任せる)/ 1 投入失敗 / 2 前提の読み取りに失敗

前提の表(nar_races: track,race_no,post_time / nar_odds_full: track,race_date,race_no,kind,observed_at,is_final,combos)
"""
import argparse
import datetime as dt
import html as html_mod
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

JST = dt.timezone(dt.timedelta(hours=9))
UA = os.environ.get("COLLECTOR_UA") or "odds-collector/1.0"
TIMEOUT = 20

# 公式表記の場名 → k_babaCode
BABA = {
    "帯広ば": "03", "盛岡": "10", "水沢": "11", "浦和": "18", "船橋": "19", "大井": "20",
    "川崎": "21", "金沢": "22", "笠松": "23", "名古屋": "24", "園田": "27", "姫路": "28",
    "高知": "31", "佐賀": "32", "門別": "36",
}


def log(msg):
    print(f"[{dt.datetime.now(JST):%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def load_env(path):
    """KEY=VALUE の .env を環境変数に読む(ローカル試験用)。"""
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http_get(url, headers=None, timeout=TIMEOUT):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def sb_get(base, key, path):
    """PostgREST の GET(読み取りだけ)。"""
    raw = http_get(f"{base}/rest/v1/{path}", {"apikey": key, "Authorization": f"Bearer {key}",
                                              "Accept": "application/json"})
    return json.loads(raw.decode("utf-8"))


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


def _text(fragment):
    """タグを落として1行の文字列に。"""
    s = re.sub(r"<[^>]+>", " ", fragment)
    s = html_mod.unescape(s).replace("　", " ")
    return re.sub(r"\s+", " ", s).strip()


def _num(text):
    """'6.2' → 6.2 / '取消'・''・'---' → None。"""
    m = re.search(r"\d+(?:\.\d+)?", str(text or ""))
    return float(m.group(0)) if m else None


def post_minutes(post_time):
    """'1420' → 860(JST の分)。読めなければ None。"""
    m = re.fullmatch(r"(\d{2})(\d{2})", str(post_time or "").strip())
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def pick_targets(races, finals, now_min, before, after, limit):
    """発走 before 分前〜 after 分後のレース(最終オッズ済みは除く)。発走が近い順。"""
    done = {(r.get("track"), int(r.get("race_no"))) for r in finals if r.get("is_final")}
    out = []
    for r in races:
        track, no = r.get("track"), r.get("race_no")
        baba = BABA.get(track)
        pm = post_minutes(r.get("post_time"))
        if baba is None or no is None or pm is None:
            continue
        if (track, int(no)) in done:
            continue
        delta = pm - now_min
        if delta > before or delta < -after:
            continue
        out.append({"track": track, "race_no": int(no), "baba": baba, "post": pm, "delta": delta})
    out.sort(key=lambda x: (x["post"], x["track"]))
    return out[:limit]


BASE = "https://www.keiba.go.jp/KeibaWeb/TodayRaceInfo"
TABLE = "nar_odds_full"
CONFLICT = "track,race_date,race_no,kind"
SLEEP = 1.0            # 1リクエストの間隔(秒)。公式サイトへの負荷を抑える

# 公式ページ(パス) → そのページから採れる券種。枠連のページだけ1ページで2種
PAGES = [
    ("OddsWakuLenFukuTan", ("wakuren", "wakutan")),
    ("OddsUmLenFuku", ("umaren",)),
    ("OddsUmLenTan", ("umatan",)),
    ("OddsWide", ("wide",)),
    ("Odds3LenFuku", ("sanrenpuku",)),
    ("Odds3LenTan", ("sanrentan",)),
]
KIND_LABEL = {"wakuren": "枠連複", "wakutan": "枠連単", "umaren": "馬連", "umatan": "馬単",
              "wide": "ワイド", "sanrenpuku": "3連複", "sanrentan": "3連単"}
RANKING_KINDS = ("umaren", "umatan", "wide", "sanrenpuku", "sanrentan")
WAKU_MAX = 8           # 枠は 1〜8(公式の全場共通)


# ---------------------------------------------------------------- 組合せ数の理論値

def _nCr(n, r):
    if n < r:
        return 0
    out = 1
    for i in range(r):
        out = out * (n - i) // (i + 1)
    return out


def _nPr(n, r):
    if n < r:
        return 0
    out = 1
    for i in range(r):
        out *= n - i
    return out


# 券種 → (組合せの頭数, 理論値の式)。枠連は枠の数で決まるので**この検算はしない**
COMBO_SIZE = {"umaren": 2, "umatan": 2, "wide": 2, "sanrenpuku": 3, "sanrentan": 3}
COMBO_COUNT = {
    "umaren": lambda n: _nCr(n, 2), "wide": lambda n: _nCr(n, 2), "umatan": lambda n: _nPr(n, 2),
    "sanrenpuku": lambda n: _nCr(n, 3), "sanrentan": lambda n: _nPr(n, 3),
}


def runners_for_count(kind, count, head):
    """組合せ数 count が「何頭ぶんの全通り」かを返す(合わなければ None)。

    ⛔取消馬がいるとその馬を含む組は公式ページに載らない= 組合せ数は **取消後の頭数 m の理論値ちょうど**になる。
      理論値は m について厳密に増えるので、`f(m) == count` を満たす m は高々1つ。
      m == head なら取消なし、m < head なら (head - m) 頭が取消。m が見つからない(=どの頭数の全通りでもない)
      ページは、途中で切れているか読み方が違う=**捨てて次回に任せる**。
    """
    f = COMBO_COUNT.get(kind)
    if f is None or not head or count <= 0:
        return None
    for m in range(COMBO_SIZE[kind], head + 1):
        if f(m) == count:
            return m
    return None


# ---------------------------------------------------------------- 取得

def page_url(path, date, race_no, baba):
    q = urllib.parse.urlencode({"k_raceDate": date.strftime("%Y/%m/%d"), "k_raceNo": race_no,
                                "k_babaCode": baba})
    return f"{BASE}/{path}?{q}"


# ---------------------------------------------------------------- 解析(標準ライブラリだけ)

TITLE_RE = re.compile(r'<h4[^>]*class="[^"]*odd_title[^"]*"[^>]*>(.*?)</h4>', re.S)
TABLE_RE = re.compile(r"<table[^>]*>(.*?)</table>", re.S)
RANKING_RE = re.compile(r'<table[^>]*class="[^"]*odd_ranking_table[^"]*"[^>]*>(.*?)</table>', re.S)
TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)


def is_final_of(page):
    """見出し(odd_title)に「最終」があれば確定。枠連ページは見出しが2つあるのでどちらかにあれば真。"""
    return any("最終" in _text(m) for m in TITLE_RE.findall(page))


def head_count(page):
    """そのページの `odd_table`(馬番の行列)から出走頭数を数える。読めなければ None。

    ⚠この表は入力UI用でオッズは入っていないが、**馬番の行だけは実在の馬番**が並ぶ(2026-09-02 実測)。
    """
    m = re.search(r'<table[^>]*class="[^"]*odd_table[^"]*"[^>]*>(.*?)</table>', page, re.S)
    if not m:
        return None
    for tr in TR_RE.findall(m.group(1)):
        ths = [_text(x) for x in re.findall(r"<th[^>]*>(.*?)</th>", tr, re.S)]
        if not any("馬番" in x for x in ths):
            continue
        nums = set()
        for td in TD_RE.findall(tr):
            v = _num(_text(td))
            if v is not None:
                nums.add(int(v))
        if nums:
            return len(nums)
    return None


def parse_ranking(page):
    """人気順の表(馬連/馬単/ワイド/3連複/3連単)→ ([(組, オッズ, 人気)], 落とした行数)。

    ⛔表は25件ごとに分かれる(3連単は50表を超える)。**全部の `odd_ranking_table` をつないでから**数える。
    ⛔人気は公式の整数をそのまま写す(同オッズは同順位。自分で振り直さない= と同じ姿勢)。
    """
    out, dropped = [], 0
    for tb in RANKING_RE.findall(page):
        for tr in TR_RE.findall(tb):
            tds = [_text(x) for x in TD_RE.findall(tr)]
            if len(tds) < 3:
                continue                                    # 見出し行(th だけ)
            combo = re.fullmatch(r"(\d+)-(\d+)(?:-(\d+))?", tds[0].replace(" ", ""))
            odds, rank = _num(tds[1]), _num(tds[2])
            if not combo or odds is None or rank is None:
                dropped += 1                                # 取消・「発売前」など数値でない行
                continue
            nums = tuple(int(g) for g in combo.groups() if g is not None)
            out.append((nums, float(odds), int(rank)))
    return out, dropped


def parse_waku(page):
    """枠連のページ → {'wakuren': [[i, j, odds]], 'wakutan': [[i, j, odds]]}。

    ⛔このページの表には class が無いので、見出し(odd_title)の「枠連複」「枠連単」で本文を切る。
    ⛔各表は 行頭の `<th>` が枠 i で、`<td>枠j</td><td>オッズ</td>` が続く。「-」は無し(_num が None)。
      枠連複は i ≤ j の上三角(**同枠のゾロ目は枠に2頭以上いれば値がある**=2026-09-02 大井 5-5 で実測)、
      枠連単は i→j の順序あり。
    ⛔**見出しごと無い場がある**(2026-09-02 実測: 門別・名古屋は枠連単の見出しが無い=その場は売っていない)。
      「売っていない(None)」と「見出しはあるが空=発売前([])」を**言い分ける**(空欄を嘘にしない=)。
    """
    out = {"wakuren": None, "wakutan": None}
    parts = TITLE_RE.split(page)                            # [前置き, 見出し1, 本文1, 見出し2, 本文2, …]
    for i in range(1, len(parts) - 1, 2):
        head = _text(parts[i])
        kind = "wakuren" if "枠連複" in head else ("wakutan" if "枠連単" in head else None)
        if kind is None:
            continue
        rows = []
        for tb in TABLE_RE.findall(parts[i + 1]):
            th = re.search(r"<th[^>]*>(.*?)</th>", tb, re.S)
            a = _num(_text(th.group(1))) if th else None
            if a is None:
                continue
            for tr in TR_RE.findall(tb):
                tds = [_text(x) for x in TD_RE.findall(tr)]
                if len(tds) < 2:
                    continue
                b, odds = _num(tds[0]), _num(tds[1])
                if b is None or odds is None:
                    continue                                # 「-」= その組は無い
                if not (1 <= int(a) <= WAKU_MAX and 1 <= int(b) <= WAKU_MAX):
                    continue                                # 枠は 1〜8 の外に出ない
                rows.append([int(a), int(b), float(odds)])
        out[kind] = rows
    return out


def combos_json(kind, rows):
    """保存形。2連系 [[a,b,odds,rank]] / 3連系 [[a,b,c,odds,rank]] / 枠連 [[i,j,odds]]。"""
    if kind in ("wakuren", "wakutan"):
        return rows
    return [list(nums) + [odds, rank] for nums, odds, rank in rows]


# ---------------------------------------------------------------- 1着固定の合成オッズ(§123)

TICKS = "nar_odds_ticks"        # 2 分刻みの行(odds_tanfuku.py が同じ周回で 1 行入れる)。ここは u1/s1 を書き足すだけ
SYNTH_MAX_AGE_MIN = 4           # 直近の tick がこれより古ければ書かない(別の周回の行に付けない)


def synth_first(got):
    """その馬が 1 着の組を全部同じ額で買ったときの倍率= 1 / Σ(1/オッズ)。{馬番: 小数1桁}。
    got= parse_ranking の行 [(nums, odds, rank)]。オッズ 0 以下・数でないものは足さない(発売の無い組)。"""
    acc = {}
    for nums, odds, _rank in got:
        try:
            o = float(odds)
        except (TypeError, ValueError):
            continue
        if o <= 0 or not nums:
            continue
        acc[int(nums[0])] = acc.get(int(nums[0]), 0.0) + 1.0 / o
    return {str(k): round(1.0 / v, 1) for k, v in acc.items() if v > 0}


def _minutes_since(hhmm, now):
    try:
        h, m = int(hhmm[:2]), int(hhmm[3:5])
    except (TypeError, ValueError):
        return None
    d = (now.hour * 60 + now.minute) - (h * 60 + m)
    return d if d >= 0 else d + 24 * 60


def patch(url, key, path, body):
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{url}/rest/v1/{path}", data=data, method="PATCH",
                                 headers={"apikey": key, "Authorization": f"Bearer {key}",
                                          "Content-Type": "application/json", "Prefer": "return=minimal",
                                          "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, ""
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]
    except Exception as e:
        return 0, str(e)


def attach_synth(url, key, date, target, synth, now):
    """同じ周回の tick(直近 1 行・u1 が空)に u1/s1 を書き足す。無ければ何もしない(単複の行が無い= 窓の外)。"""
    if not synth:
        return "なし"
    try:
        rows = sb_get(url, key, f"{TICKS}?select=id,t,u1&track=eq.{urllib.parse.quote(target['track'])}"
                                f"&race_date=eq.{date.isoformat()}&race_no=eq.{target['race_no']}&order=id.desc&limit=1")
    except Exception as e:
        return f"tick 読めず {type(e).__name__}"
    if not rows:
        return "tick なし"
    row = rows[0]
    if row.get("u1") is not None:
        return "済み"
    age = _minutes_since(str(row.get("t") or ""), now)
    if age is None or age > SYNTH_MAX_AGE_MIN:
        return f"tick が古い({row.get('t')})"
    status, msg = patch(url, key, f"{TICKS}?id=eq.{row['id']}", synth)
    if status >= 300 or status == 0:
        return f"書けず status={status} {msg}"
    return f"OK id={row['id']} t={row.get('t')} 馬 {len(synth.get('u1') or {})}/{len(synth.get('s1') or {})}"


# ---------------------------------------------------------------- 1レースぶん

def save_page(save_dir, path, page):
    p = Path(save_dir) / f"{path}.html"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(page, encoding="utf-8")


def fetch_race(date, target, save_dir=None):
    """1レースの6ページを取り、券種ごとの行にする。→ (rows, stats, 頭数)。rows は upsert 用の dict。"""
    now_utc = dt.datetime.now(dt.timezone.utc).isoformat()
    rows = []
    st = {"ok": 0, "ng": 0, "empty": 0, "absent": 0, "reject": 0, "final": 0}
    head = None
    synth = {}                                   # §123 {"u1": {馬番: 馬単1着合成}, "s1": {馬番: 3連単1着合成}}
    if save_dir:
        # 見本一式には単複のページも揃える。ここでは解析も投入もしない
        try:
            time.sleep(SLEEP)
            save_page(save_dir, "OddsTanFuku",
                      http_get(page_url("OddsTanFuku", date, target["race_no"], target["baba"]))
                      .decode("utf-8", "replace"))
        except Exception as e:
            log(f"    見本の取得失敗 OddsTanFuku: {type(e).__name__}: {str(e)[:120]}")
    for pi, (path, kinds) in enumerate(PAGES):
        if pi:
            time.sleep(SLEEP)
        url = page_url(path, date, target["race_no"], target["baba"])
        try:
            page = http_get(url).decode("utf-8", "replace")
        except Exception as e:
            st["ng"] += 1
            log(f"    取得失敗 {path}: {type(e).__name__}: {str(e)[:120]}")
            continue
        if save_dir:
            save_page(save_dir, path, page)
        is_final = is_final_of(page)
        head = head_count(page) or head
        if kinds[0] in ("wakuren", "wakutan"):
            waku = parse_waku(page)
            for kind in kinds:
                got = waku.get(kind)
                if got is None:
                    st["absent"] += 1
                    log(f"    {KIND_LABEL[kind]} はこの場に無い(見出しごと無い=発売していない)")
                    continue
                if not got:
                    st["empty"] += 1
                    log(f"    発売前/表なし {KIND_LABEL[kind]}")
                    continue
                st["ok"] += 1
                st["final"] += 1 if is_final else 0
                log(f"    {KIND_LABEL[kind]} {len(got)}組{' (最終)' if is_final else ''} 先頭3組 {got[:3]}")
                rows.append({"track": target["track"], "race_date": date.isoformat(),
                             "race_no": target["race_no"], "kind": kind, "observed_at": now_utc,
                             "is_final": is_final, "combos": combos_json(kind, got),
                             "updated_at": now_utc})
            continue
        kind = kinds[0]
        got, dropped = parse_ranking(page)
        if not got:
            st["empty"] += 1
            log(f"    発売前/表なし {KIND_LABEL[kind]}")
            continue
        m = runners_for_count(kind, len(got), head)
        if m is None:
            st["reject"] += 1
            log(f"    ⚠検算落ち {KIND_LABEL[kind]}: {len(got)}組 は {head} 頭以下のどの全通りとも合わない"
                f"(数値でない行 {dropped})→ 捨てて次回に任せる")
            continue
        st["ok"] += 1
        st["final"] += 1 if is_final else 0
        scratched = "" if m == head else f"・取消 {head - m} 頭とみなす"
        log(f"    {KIND_LABEL[kind]} {len(got)}組 = {m}頭の全通り{scratched}"
            f"{' (最終)' if is_final else ''} 先頭3組 {[(list(a), o, r) for a, o, r in got[:3]]}")
        rows.append({"track": target["track"], "race_date": date.isoformat(),
                     "race_no": target["race_no"], "kind": kind, "observed_at": now_utc,
                     "is_final": is_final, "combos": combos_json(kind, got), "updated_at": now_utc})
        if kind == "umatan":
            synth["u1"] = synth_first(got)
        elif kind == "sanrentan":
            synth["s1"] = synth_first(got)
    return rows, st, head, synth


# ---------------------------------------------------------------- 本体

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="取得・解析だけして投入しない")
    ap.add_argument("--env", help="ローカル試験用 .env(既定は環境変数)")
    ap.add_argument("--url", help="読み取り先の上書き(試験用)")
    ap.add_argument("--key", help="APIキーの上書き(読み取りだけの試験なら anon でよい)")
    ap.add_argument("--date", help="対象日 YYYY-MM-DD(既定=今日。公式は当日分しか返さない)")
    ap.add_argument("--before", type=int, default=60, help="発走の何分前から取るか(既定60)")
    ap.add_argument("--after", type=int, default=10, help="発走の何分後まで取るか(既定10)")
    ap.add_argument("--limit", type=int, default=10, help="1回の実行で取るレース数の上限(既定10)")
    ap.add_argument("--save-fixtures", metavar="DIR",
                    help="取得した生HTMLを DIR/<場>_<R>R/ に保存する(処理したレースぶん全部)")
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
    except Exception as e:
        log(f"nar_races の読み取りに失敗: {type(e).__name__}: {str(e)[:200]}")
        return 2
    # 既に全券種の最終が入っているレースを飛ばすための**下読み**。⛔ここは best-effort:
    # 表がまだ無い(SQL 未適用)ときは 404 になるが、取り直すだけで害は無い(upsert は何度流しても同じ)。
    # ⚠**書き込みの失敗は rc 1 のまま**= 表が無ければ投入で必ず落ちる(黙って成功しない)
    try:
        have = sb_get(url, key, f"{TABLE}?select=track,race_no,kind,is_final&race_date=eq.{date.isoformat()}")
    except Exception as e:
        log(f"⚠{TABLE} の下読みに失敗(取り直しになるだけ): {type(e).__name__}: {str(e)[:120]}")
        have = []

    # 全券種の最終オッズが揃ったレースだけ「済み」にする(1種でも欠けていれば取りに行く)
    got_final = {}
    for r in have:
        if r.get("is_final") and r.get("race_no") is not None:
            got_final.setdefault((r.get("track"), int(r.get("race_no"))), set()).add(r.get("kind"))
    all_kinds = {k for _, kinds in PAGES for k in kinds}
    done = [{"track": t, "race_no": n, "is_final": True}
            for (t, n), ks in got_final.items() if all_kinds <= ks]

    targets = pick_targets(races, done, now_min, args.before, args.after, args.limit)
    log(f"{date} 当日のレース {len(races)} / 対象 {len(targets)}"
        f"(発走 {args.before} 分前〜{args.after} 分後・全券種の最終が揃った {len(done)} レースは除く)")
    if not targets:
        return 0

    rows, tot = [], {"ok": 0, "ng": 0, "empty": 0, "absent": 0, "reject": 0, "final": 0}
    for i, t in enumerate(targets):
        if i:
            time.sleep(SLEEP)
        log(f"  {t['track']} {t['race_no']}R(発走まで {t['delta']} 分)")
        save = (Path(args.save_fixtures) / f"{t['track']}_{t['race_no']}R") if args.save_fixtures else None
        got, st, head, synth = fetch_race(date, t, save_dir=save)
        if save:
            log(f"    生HTMLを保存: {save}({head} 頭)")
        rows.extend(got)
        if synth:
            t["synth"] = synth
            u1, s1 = synth.get("u1") or {}, synth.get("s1") or {}
            top = sorted(u1.items(), key=lambda kv: kv[1])[:3]
            log(f"    1着固定の合成 馬単 {len(u1)}頭 / 3連単 {len(s1)}頭 先頭3頭 {top}")
        for k in tot:
            tot[k] += st[k]

    log(f"取得 成功 {tot['ok']} / 失敗 {tot['ng']} / 発売前 {tot['empty']} / その場に無い {tot['absent']} / "
        f"検算落ち {tot['reject']} / 最終 {tot['final']}(行 {len(rows)})")
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
    # §123 同じ周回の 2 分刻みの行(odds_tanfuku.py が先に入れている)に 1着固定の合成を書き足す
    for t in targets:
        if t.get("synth"):
            log(f"  合成→{TICKS} {t['track']} {t['race_no']}R: {attach_synth(url, key, date, t, t['synth'], now)}")
    return 0


if __name__ == "__main__":
    t0 = time.time()
    rc = main()
    log(f"終了 rc={rc} ({time.time() - t0:.0f}s)")
    sys.exit(rc)
