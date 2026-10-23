# -*- coding: utf-8 -*-
"""地方競馬公式サイト(keiba.go.jp TodayRaceInfo/RaceList)の当日のレース一覧を読み、
発走時刻が変わっていたら Supabase の表 nar_races の post_time を書き換える。標準ライブラリだけ。

発走時刻は当日ずれることがある(実測 2026-09-03: ある場の 7R が 18:02 → 18:04)。
公式の一覧は直っているのに保存側が古いままだと、発走前後の窓で取るオッズも「あと何分」もその分ずれる。

§137: 同じ一覧ページの上に出る取り止めの告知(section.earlyWarning)も、同じ 1 回の取得から
nar_races.cancel_note に写す(⛔通信は増やさない)。
2026-10-23: 告知で取り止めと読めたレースには cancelled='notice'(公式の告知で取り止め・払戻は発表待ち)を付ける。
  ⛔cancelled が空の行だけ= 公式 ZIP の印(refund/nosale)は上書きしない。ZIP の払戻が来ればそちらが正(走っていれば印は消える)。
  実測 10/23 浦和 3R=「第3競走は、…取り止めとなりました」が旧判定(以降/開催だけ)で読めず、印は ZIP の 16:06 まで付かなかった。

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
# §137 当日メニューの上に出る告知(取り止め・発走時刻の変更など)。実測 2026-09-08 の笠松=
# <section class="earlyWarning"><div class="message">9/8（火）の笠松競馬第7競走以降は、…取り止めとなりました。</div></section>
WARN_RE = re.compile(r'<section[^>]*class="[^"]*earlyWarning[^"]*"[^>]*>(.*?)</section>', re.S | re.I)
MSG_RE = re.compile(r'<div[^>]*class="[^"]*message[^"]*"[^>]*>(.*?)</div>', re.S | re.I)
NOTICE_MARK = "notice"  # nar_races.cancelled の値= 公式の告知で取り止め(払戻は ZIP 待ち)
# 取り止めと言い切る文だけ。⛔「中止」は開催ごと/言い切りの形だけ(理由の「第2競走の競走中止した馬」を拾わない)
CANCEL_RE = re.compile(r"取り止め|取りやめ|取止め|開催を?中止|開催は中止|中止(?:します|とします|といたします|いたします|となりました|としました)")
HEDGE_RE = re.compile(r"場合|可能性|予定|見込み|恐れ|おそれ|検討")   # 見込みの文は推定しない
_JOIN = r"[、,，・]|及び|および|ならびに|並びに|と|から|より|～|〜|~|－|-"
# 主語= 「第3競走は」「第7競走以降は」「第9競走から第12競走を」「第3・4競走は」。⛔「第2競走の…」は主語ではない
SUBJ_RE = re.compile(r"(第\s*\d{1,2}\s*(?:競走|R)?(?:\s*(?:" + _JOIN + r")\s*(?:第\s*)?\d{1,2}\s*(?:競走|R)?)*)"
                     r"\s*(以降|以後)?\s*(?:の競走|の各競走|の全競走)?\s*(?:は|を|につき|について)")
RANGE_JOIN = re.compile(r"から|より|～|〜|~|－|-")
WHOLE_RE = re.compile(r"開催|全競走|全レース|本日の競走")


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


def warning_text(page):
    """当日メニューの上の告知(section.earlyWarning の div.message)を1行の文字列で。無ければ None。"""
    m = WARN_RE.search(page)
    if not m:
        return None
    # 10/23 告知が 2 件以上並ぶ日もある= div.message を全部つなぐ(最初の 1 件だけだと 2 件目の取り止めを落とす)
    ds = [_text(x) for x in MSG_RE.findall(m.group(1))]
    return " ".join(x for x in ds if x) or _text(m.group(1)) or None


def _subject_nos(span, after):
    """主語の字列 → レース番号の集合と「以降」かどうか。範囲(から/～)は間を埋める。"""
    nums = [int(n) for n in re.findall(r"\d{1,2}", span)]
    joins = re.split(r"\d{1,2}", span)[1:-1]          # 数と数の間の字
    out = set(nums)
    for i, j in enumerate(joins):
        if RANGE_JOIN.search(j):
            out.update(range(nums[i], nums[i + 1] + 1))
    return out, bool(after)


def cancel_races(text, nos):
    """告知文 → 取り止めのレース番号(昇順・nos の中だけ)。取り止めの告知でなければ []。

    ⛔文(。区切り)ごとに見る。言い切りの「取り止め」があり、見込みの語(場合・予定など)が無い文だけ。
    ⛔主語(第N競走は/を)が読めればその番号・「以降」ならそこから最後まで。主語が無く開催ごとの話なら全部。
      それ以外(番号も開催も読めない)は何も返さない= 推定しない。
    """
    nos = sorted({int(n) for n in (nos or [])})
    if not text or not nos:
        return []
    hit = set()
    for sent in re.split(r"[。\n]", str(text)):
        if not CANCEL_RE.search(sent) or HEDGE_RE.search(sent):
            continue
        subj = SUBJ_RE.findall(sent)
        if subj:
            for span, after in subj:
                got, onward = _subject_nos(span, after)
                if onward:
                    got = {n for n in nos if n >= min(got)}
                hit |= got
        elif WHOLE_RE.search(sent):
            hit |= set(nos)
    return [n for n in nos if n in hit]


def cancel_from(text):
    """旧の口(試験と互換)= 取り止めの最初のレース番号(全部なら 1)・無ければ None。1〜12R として読む。"""
    got = cancel_races(text, range(1, 13))
    return got[0] if got else None


def write_cancel_note(url, key, date, track, page, saved_notes, dry_run):
    """§137 取り止めの告知文を nar_races.cancel_note に写し、そのレースに印 notice を付ける(10/23)。

    ⛔印は cancelled が空の行だけ(cancelled=is.null で絞る)= ZIP の refund/nosale を上書きしない。
    ⛔同じ文が既に入っていれば告知文は PATCH しない(印の PATCH は空の行が無ければ 0 行で終わる)。
    戻り= 0 正常 / 1 書き込み失敗(呼び手の rc に混ぜる)。
    """
    text = warning_text(page)
    if not text:
        return 0
    targets = cancel_races(text, saved_notes.keys())
    if not targets:
        log(f"  {track}: 告知はあるが取り止めのレースが読めない= 何も書かない({text[:60]})")
        return 0
    if dry_run:
        log(f"  {track}: 取り止めの告知 {targets} (dry-run)({text[:60]})")
        return 0
    base = [("race_date", f"eq.{date.isoformat()}"), ("track", f"eq.{track}"),
            ("race_no", "in.(" + ",".join(str(n) for n in targets) + ")")]
    rc = 0
    if not all(saved_notes.get(no) == text for no in targets):
        log(f"  {track}: 取り止めの告知を {targets} へ({text[:60]})")
        st, msg = patch(url, key, TABLE, base, {"cancel_note": text})
        if st >= 300 or st == 0:
            log(f"    告知の書き込み失敗 status={st} {msg}")
            rc = 1
    st, msg = patch(url, key, TABLE, base + [("cancelled", "is.null")], {"cancelled": NOTICE_MARK})
    if st >= 300 or st == 0:
        log(f"    印の書き込み失敗 status={st} {msg}")
        rc = 1
    return rc


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
        races = sb_get(url, key, f"{TABLE}?select=track,race_no,post_time,cancel_note"
                                 f"&race_date=eq.{date.isoformat()}"
                                 "&order=track.asc,race_no.asc")
    except Exception as e:
        log(f"{TABLE} の読み取りに失敗: {type(e).__name__}: {str(e)[:200]}")
        return 2
    saved, notes = {}, {}
    for r in races:
        no, pt = r.get("race_no"), r.get("post_time")
        if r.get("track") not in BABA or no is None:
            continue
        if pt:
            saved.setdefault(r["track"], {})[int(no)] = str(pt)
        # §137 告知は発走時刻の有無と関係なく写す(⛔いま入っている文と同じなら書かない)
        notes.setdefault(r["track"], {})[int(no)] = r.get("cancel_note")
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
        # §137 同じ 1 回の取得から告知も見る(⛔発走時刻の突合とは独立= 番号が食い違う日でも告知は書ける)
        rc = max(rc, write_cancel_note(url, key, date, track, page, notes.get(track) or {}, a.dry_run))
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
