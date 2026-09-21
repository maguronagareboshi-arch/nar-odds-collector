# -*- coding: utf-8 -*-
"""楽天競馬のページ(カレンダー・払戻の日ページ・成績のレースページ)を公式 ZIP と同じ形の行に直す。

⛔ここには**解析だけ**を置く(通信も DB も無い)。取得と投入は rakuten_backfill.py。
   tests/test_rakuten_parse.py が手元に保存した HTML(tests/fixtures/rakuten/*.html)で期待値を固定する。

公式(nar_official_csv.py)と同じ書き方に揃える所:
  ・corners= [{"name": "３コーナー", "order": "5,1,6,4,3,2"}](公式の名称も全角の「１コーナー」)
  ・time_raw= 数字だけ("1:33.2" → "1332")・margin= "2.1/2" "クビ" "アタマ"(公式の書式)
  ・going/weather= 公式の語(不良/稍重/晴…)。帯広ばの馬場は水分の数字("1.0%" → "1.0")
  ・surface= ダート/芝。帯広ばは公式に合わせて空文字
楽天に**無い**もの(取れない= null のまま。⛔埋めない):
  ハロンタイム(furlongs)・上がり3F/4F(レース全体)・回り(右/左)・条件の「定量」等・生年月日・血統・馬主・生産者
"""
import html as html_mod
import re

BANEI = "帯広ば"

# 楽天の券種名 → 払戻(nar_race_payouts の "t")の鍵。7 つは公式 nar_official_csv.TICKET_NAMES と同じ値。
# 枠複/枠単は公式の払戻に**無い**(2026-09-21 に本番の鍵を確認= 7 種だけ)。⛔落とさずに 'wakuren'/'wakutan'
# の鍵で同じ JSON に足す(既にある 7 種の鍵は変えない)。
TICKETS = {
    "単勝": "win", "複勝": "place", "枠複": "wakuren", "枠単": "wakutan",
    "馬複": "quinella", "馬単": "exacta", "ワイド": "wide", "三連複": "trio", "三連単": "trifecta",
}
# 票数(nar_race_votes)の鍵だけは**既にある nar_sales の列名**にそろえる(同じ読み手で両方引けるように)。
VOTE_KEYS = dict(TICKETS, **{"枠複": "bracket_quinella", "枠単": "bracket_exacta"})
UNORDERED = {"quinella", "wide", "trio", "wakuren"}
WIDTH = {"win": 1, "place": 1, "quinella": 2, "exacta": 2, "wide": 2,
         "wakuren": 2, "wakutan": 2, "trio": 3, "trifecta": 3}

ZEN = str.maketrans("０１２３４５６７８９／　", "0123456789/ ")
ZEN_DIGIT = str.maketrans("０１２３４５６７８９", "0123456789")


def text(fragment):
    """タグを落として 1 行に。"""
    s = re.sub(r"<[^>]+>", " ", fragment or "")
    return re.sub(r"\s+", " ", html_mod.unescape(s).replace("　", " ")).strip()


def text_ja(fragment):
    """text() と同じだが**全角の空白は残す**(公式のレース名・条件が全角空白で区切られているため)。"""
    s = re.sub(r"<[^>]+>", " ", fragment or "")
    s = html_mod.unescape(s)
    s = re.sub(r"[^\S　]+", " ", s)
    return re.sub(r" *　 *", "　", s).strip()


def lines(fragment):
    """<br> とタグを改行にして、空でない行の列に。"""
    s = re.sub(r"<br\s*/?>", "\n", fragment or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "\n", s)
    out = []
    for raw in html_mod.unescape(s).splitlines():
        v = raw.replace("　", " ").strip()
        if v and v != "\xa0":
            out.append(v)
    return out


def to_int(v):
    m = re.search(r"-?\d+", str(v or "").replace(",", ""))
    return int(m.group(0)) if m else None


def to_num(v):
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return float(m.group(0)) if m else None


# ---------------------------------------------------------------- カレンダー

CAL_ROW_RE = re.compile(r'<tr class="place">(.*?)</tr>', re.S)
CAL_TRACK_RE = re.compile(r'<th scope="row">([^<]+)</th>')
CAL_ID_RE = re.compile(r"/RACEID/(\d{18})")


def parse_calendar(page):
    """その月のカレンダー → [{"track","race_date","raceid"}](開催のある場と日だけ・日付順)。

    ⛔回/日は RACEID から**拾うだけ**(自分で組み立てない)。RACEID = 年月日8 + 場4 + 回2 + 日2 + R2。
    """
    out = []
    for row in CAL_ROW_RE.findall(page):
        m = CAL_TRACK_RE.search(row)
        if not m:
            continue
        track = m.group(1).strip()
        seen = set()
        for rid in CAL_ID_RE.findall(row):
            if rid in seen:
                continue
            seen.add(rid)
            d = rid[:8]
            out.append({"track": track, "race_date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "raceid": rid})
    out.sort(key=lambda r: (r["race_date"], r["track"]))
    return out


def race_id_for(day_raceid, race_no):
    """日のRACEID(末尾 00)→ そのレースの RACEID。"""
    return f"{day_raceid[:16]}{int(race_no):02d}"


# ---------------------------------------------------------------- 払戻の日ページ

HEAD_RE = re.compile(r'<h3 class="headline"><span>■</span>(\d+)R([^<]*)</h3>')
PAY_RE = re.compile(r'<th scope="row">([^<]+)</th>\s*'
                    r'<td class="number"[^>]*>(.*?)</td>\s*'
                    r'<td class="money"[^>]*>(.*?)</td>\s*'
                    r'<td class="rank"[^>]*>(.*?)</td>', re.S)
VOTE_RE = re.compile(r'<th scope="row">([^<]+)</th>\s*<td>([\d,]+)</td>')


def combination(kind, raw):
    """'5-6' → '5-6'(順の無い券種は小さい順)。読めなければ None(⛔推定しない)。"""
    nums = [int(x) for x in re.findall(r"\d+", raw or "")]
    if len(nums) != WIDTH[kind] or len(set(nums)) != len(nums) or min(nums) <= 0:
        return None
    if kind in UNORDERED:
        nums = sorted(nums)
    return "-".join(str(n) for n in nums)


def parse_payout_block(block):
    """1 レースぶんの払戻表 → 公式 nar_race_payouts と同じ [{"t","c","y","p"}, ...]。"""
    out = []
    for name, num, money, rank in PAY_RE.findall(block):
        kind = TICKETS.get(name.strip())
        if not kind:
            continue
        combos, yens, ranks = lines(num), lines(money), lines(rank)
        for i, c in enumerate(combos):
            key = combination(kind, c)
            yen = to_int(yens[i]) if i < len(yens) else None
            if key is None or yen is None:
                continue
            out.append({"t": kind, "c": key, "y": yen,
                        "p": to_int(ranks[i]) if i < len(ranks) else None})
    out.sort(key=lambda p: (p["t"], p["c"]))
    return out


def parse_votes_block(block):
    """「総票数」「返還票数」の小さい表 → ({券種: 票数}, {券種: 返還票数}, 読めなかった券種名)。"""
    m = re.search(r"総票数([\s\S]*?)返還票数([\s\S]*?)</table>\s*</td>", block)
    if not m:
        return None, None, []
    got, dropped = [], []
    for part in (m.group(1), m.group(2)):
        cur = {}
        for name, num in VOTE_RE.findall(part):
            kind = VOTE_KEYS.get(name.strip())
            if kind:
                cur[kind] = int(num.replace(",", ""))
            else:
                dropped.append(name.strip())
        got.append({k: cur.get(k, 0) for k in VOTE_KEYS.values()})
    return got[0], got[1], sorted(set(dropped))


def parse_dividend_day(page):
    """1 日 1 場の払戻ページ → {レース番号: {"payouts": [...], "votes": {...}, "refunds": {...},
    "headline": "…"}}。⛔払戻の読めないレースは入れない。"""
    out = {}
    parts = HEAD_RE.split(page)
    for i in range(1, len(parts) - 2, 3):
        no = int(parts[i])
        block = parts[i + 2]
        payouts = parse_payout_block(block)
        votes, refunds, dropped = parse_votes_block(block)
        if not payouts:
            continue
        out[no] = {"payouts": payouts, "votes": votes, "refunds": refunds,
                   "headline": text(parts[i + 1]), "dropped": dropped}
    return out


# ---------------------------------------------------------------- 成績のレースページ

NOTE_RE = re.compile(r"(取消|除外|中止|失格|降着)")
DIST_RE = re.compile(r"<li class=\"distance\">\s*([^<]+)</li>")
DL_RE = re.compile(r"<dt>([^<]*)</dt>\s*<dd>([^<]*)</dd>")
ROW2_RE = re.compile(r'<th scope="row">([^<]*)</th>\s*<td[^>]*>([^<]*)</td>')
# 「上がり」= '4F 53.5 - 3F 41.7'(場によっては 3F だけ・高知のように表ごと無い日もある)
LAST_RE = re.compile(r"(\d)F\s*([\d.]+)")
ROW_RE = re.compile(r"<tr[^>]*data-grouping=\"\d+\">(.*?)</tr>", re.S)
CELL_RE = re.compile(r"<t[dh] class=\"([a-zA-Z]+)[^\"]*\"[^>]*>(.*?)</t[dh]>", re.S)
HORSEID_RE = re.compile(r"/HORSEID/(\d+)")
PRIZE_RE = re.compile(r"<li>\d着([\d,]+)円</li>")


def norm_margin(raw):
    """楽天の全角の着差 → 公式の書式。'２　１／２'→'2.1/2' / '１／２'→'1/2' / '７'→'7' / 'アタマ'→'アタマ'。"""
    s = (raw or "").translate(ZEN).strip()
    s = re.sub(r"\s+", " ", s)
    if not s:
        return None
    m = re.fullmatch(r"(\d+) (\d+/\d+)", s)
    if m:
        return f"{m.group(1)}.{m.group(2)}"
    return s.replace(" ", "")


def norm_time(raw):
    """'1:33.2' → '1332' / '50.5' → '505'(公式 タイム 列と同じ数字だけの書式)。"""
    s = re.sub(r"[^0-9]", "", str(raw or ""))
    return s or None


def time_to_sec(raw):
    """公式の生表記を秒へ(load_nar_official.time_to_sec と**同じ**規則)。"""
    s = re.sub(r"[^0-9]", "", str(raw or ""))
    if not s:
        return None
    tenth = int(s[-1]); rest = s[:-1]
    if not rest:
        return tenth / 10
    return int(rest[:-2] or 0) * 60 + int(rest[-2:]) + tenth / 10


def parse_race_head(page, track):
    """ページ頭の「レースの条件」→ nar_races 相当の辞書(鍵の 場/日/R は呼び手が入れる)。"""
    note = page[page.find("raceNote"):page.find("id=\"localBox\"")]
    dl = {k.strip().rstrip("："): v.strip() for k, v in DL_RE.findall(note)}
    dist = DIST_RE.search(note)
    surface, distance_m = "", None
    if dist:
        d = dist.group(1).strip()
        distance_m = to_int(d)
        surface = "" if track == BANEI else ("芝" if d.startswith("芝") else "ダート" if d.startswith("ダ") else "")
    going = None
    for key in ("ダ", "芝", "馬場"):
        if key in dl:
            going = dl[key].replace("%", "").strip()
            break
    h2 = re.search(r"<h2>(.*?)</h2>", note, re.S)
    cond = re.search(r"<ul class=\"horseCondition\">(.*?)</ul>", note, re.S)
    laps, last = _time_table(page)
    return {
        "post_time": re.sub(r"[^0-9]", "", dl.get("発走時刻", "")) or None,
        "race_name": text_ja(h2.group(1)) if h2 else None,
        "surface": surface, "direction": None, "distance_m": distance_m,
        "weather": dl.get("天候") or None, "going": going,
        # 公式の「条件」は半角の数字(サラブレッド系　3歳以上)。楽天は全角なので数字だけ半角に寄せる
        "condition": text_ja(cond.group(1)).translate(ZEN_DIGIT) if cond else None,
        "prize_yen": [int(p.replace(",", "")) for p in PRIZE_RE.findall(note)] or None,
        "race_last4f": last.get(4), "race_last3f": last.get(3), "furlongs": laps,
        "corners": _corners(page),
    }


def _table(page, summary):
    m = re.search(r'<table[^>]*summary="%s"[^>]*>(.*?)</table>' % re.escape(summary), page, re.S)
    return m.group(1) if m else ""


def _corners(page):
    """「コーナー通過順位」の表 → [{"name","order"}]。名前は場で違う(「３コーナー」「１角」)ので**そのまま**入れる
    (facts.py は名前でなく並んだ位置で c1..c4 を決める)。"""
    out = []
    for name, order in ROW2_RE.findall(_table(page, "コーナー通過順位")):
        n, o = name.strip(), order.strip()
        if n or o:
            out.append({"name": n, "order": o})
    return out


def _time_table(page):
    """「タイム」の表 → (ハロンタイムの列, {3: 上がり3F, 4: 上がり4F})。表が無い場・日は ([], {})。"""
    laps, last = [], {}
    for name, value in ROW2_RE.findall(_table(page, "タイム")):
        if "ハロン" in name:
            laps = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", value)]
        elif "上がり" in name:
            last = {int(f): float(v) for f, v in LAST_RE.findall(value)}
    return laps, last


def parse_runs(page):
    """全着順の表 → [{…}](nar_runs 相当 + horse_id)。走った順のまま。"""
    body = page[page.find("id=\"oddsField\""):]
    body = body[:body.find("</table>")]
    out = []
    for row in ROW_RE.findall(body):
        cell = {}
        for name, frag in CELL_RE.findall(row):
            cell.setdefault(name, frag)
        num = to_int(text(cell.get("number", "")))
        if num is None:
            continue
        order_raw = text(cell.get("order", ""))
        finish = to_int(order_raw) if re.fullmatch(r"\d+", order_raw) else None
        margin = norm_margin(text(cell.get("lead", "")))
        note = None
        if finish is None and order_raw:
            note = order_raw
        if margin and NOTE_RE.search(margin):
            note, margin = margin, None
        state = text(cell.get("state", ""))
        # 公式は 牡/牝/セン。楽天は「セ」1 字で出す
        state = re.sub(r"^セ(?![ン])", "セン", state)
        sexage = re.match(r"([牡牝せセンセ騸]+)\s*(\d+)", state)
        weight = lines(cell.get("weight", ""))
        jockey = lines(cell.get("jockey", ""))
        wt = text(cell.get("weightTax", ""))
        mark = re.match(r"^[^\d.]+", wt)
        # ⛔公式は減量の記号(◇☆▲★△)を「負担重量」の頭に付けるが、楽天は**騎手名の頭**に付ける。
        #   公式と同じ列(weight_mark)に移し、騎手名は記号を外す(2022-11-01 大井で 27/154 が該当)。
        jmark = re.match(r"^[^\w぀-ヿ一-鿿]+", jockey[0]) if jockey else None
        time_raw = norm_time(text(cell.get("time", "")))
        hid = HORSEID_RE.search(cell.get("horse", "") or "")
        out.append({
            "gate": to_int(text(cell.get("position", ""))), "runner_number": num,
            "horse_name": text(cell.get("horse", "")) or None,
            "horse_id": hid.group(1) if hid else None,
            "sex": sexage.group(1) if sexage else None,
            "age": int(sexage.group(2)) if sexage else None,
            "jockey": (jockey[0][jmark.end():] if jmark else jockey[0]) if jockey else None,
            "trainer": text(cell.get("tamer", "")) or None,
            "carried_weight": to_num(wt),
            "weight_mark": mark.group(0) if mark else (jmark.group(0) if jmark else None),
            "body_weight": to_int(weight[0]) if weight else None,
            "body_weight_change": to_int(weight[1]) if len(weight) > 1 else None,
            "finish": finish, "finish_note": note,
            "time_raw": time_raw, "time_sec": time_to_sec(time_raw), "margin": margin,
            "last3f": to_num(text(cell.get("spurt", ""))),
            "popularity": to_int(text(cell.get("rank", ""))),
        })
    return out


def parse_performance(page, track):
    """成績のレースページ → {"race": {...}, "runs": [...]}"""
    runs = parse_runs(page)
    race = parse_race_head(page, track)
    race["field_size"] = len(runs)
    return {"race": race, "runs": runs}
