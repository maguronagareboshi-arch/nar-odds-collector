# 本体 cloud: 元は 他場/scraper/nar_official_csv.py の写し(2026-08-23)だが、本体の正規化はこちらが正本。
# ⚠ 当日更新の本線 = GitHub Actions → cloud/refresh.py → **この写し**(PROJECT.md)。
#   PC 側の予備 pipeline/nar_refresh.py だけが 他場/scraper の古い写しを読む(sys.path)。
#   他場 は読み取り専用(PROJECT.md)なので向こうは直さない = 2つは既にずれている:
#     race_kind(§27.2・2026-08-25 追加) / birth_date・trainer_area(§38 R-0・2026-08-27 追加)は**こちらにしか無い**。
#   → PC 側の予備で作った正規化JSONにはこの3列が入らない。遡って入れるときは
#     load_nar_official.py --from-raw(生ZIPをこの写しで正規化し直す)を使うこと。
"""Download and normalize NAR's official ZIP/CSV data feed.

The official feed contains race lists, runners, exact combination odds and
100-yen payouts.  Raw archives are stored immutably beside a normalized JSON
receipt.  Monthly odds are settlement/final evidence only; only a daily file
captured before post time may be used as a decision-time quote.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any, Iterable
from urllib.parse import urlencode

BASE_URL = "https://www.keiba.go.jp/KeibaWeb/DataDownload"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36"
FORMAT = "nar-official-csv-v1"
UNORDERED = {"quinella", "wide", "trio"}
TICKET_NAMES = {
    "単勝": "win",
    "単勝式": "win",
    "複勝": "place",
    "複勝式": "place",
    "馬複": "quinella",
    "馬連複": "quinella",
    "馬複式": "quinella",
    "馬単": "exacta",
    "馬連単": "exacta",
    "馬単式": "exacta",
    "ワイド": "wide",
    "拡大馬複": "wide",
    "３連複": "trio",
    "3連複": "trio",
    "３連単": "trifecta",
    "3連単": "trifecta",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def digest(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


_WEIGHT_MARK = re.compile(r"^[^\d.]+")     # 負担重量の頭の記号(◇☆▲★△ など)


def _weight_mark(value: Any) -> str | None:
    m = _WEIGHT_MARK.match(str(value or "").strip())
    return m.group(0) if m else None


def _number(value: Any, *, integer: bool = False) -> float | int | None:
    text = str(value or "").strip().replace(",", "")
    if not text or text in {"-", "--", "---", "取消", "除外"}:
        return None
    try:
        number = float(text)
    except ValueError:
        return None
    if number < 0:
        return None
    return int(number) if integer else float(number)


def iso_date(value: Any) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8:
        raise ValueError(f"invalid NAR race date: {value!r}")
    return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"


def opt_iso_date(value: Any) -> str | None:
    """欠測を許す日付(生年月日)。空・桁違い・暦にない日は None を返す(1行で月次全体を落とさない)。
    競走年月日は必須なので今まで通り iso_date() が例外を投げる。"""
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) != 8:
        return None
    text = f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    try:
        dt.date.fromisoformat(text)
    except ValueError:
        return None
    return text


def canonical_combination(ticket_type: str, values: Iterable[Any]) -> str:
    numbers = tuple(int(value) for value in values if str(value or "").strip())
    expected = 1 if ticket_type in {"win", "place"} else 2 if ticket_type in {"quinella", "exacta", "wide"} else 3
    if len(numbers) != expected or len(set(numbers)) != expected:
        raise ValueError(f"invalid {ticket_type} combination: {numbers}")
    if ticket_type in UNORDERED:
        numbers = tuple(sorted(numbers))
    return "-".join(map(str, numbers))


def download_url(kind: str, *, scope: str, year: int | None = None,
                 month: int | None = None, race_date: str | None = None) -> str:
    if kind not in {"race", "odds"}:
        raise ValueError("kind must be race or odds")
    if scope not in {"monthly", "daily"}:
        raise ValueError("scope must be monthly or daily")
    endpoint = "RaceDataDownload" if kind == "race" else "OddsDataDownload"
    params: dict[str, Any] = {"type": scope}
    if scope == "monthly":
        if year is None or month is None:
            raise ValueError("monthly download requires year and month")
        params.update({"k_year": int(year), "k_month": int(month)})
    else:
        if not race_date:
            raise ValueError("daily download requires race_date")
        parsed = dt.date.fromisoformat(str(race_date))
        params["k_raceDate"] = parsed.strftime("%Y/%m/%d")
    return f"{BASE_URL}/{endpoint}?{urlencode(params)}"


def download_archive(url: str, *, timeout: int = 90) -> tuple[bytes, str]:
    import requests

    session = requests.Session()
    session.headers.update({"User-Agent": UA, "Accept": "application/zip,*/*"})
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    payload = bytes(response.content)
    if len(payload) < 4 or payload[:2] != b"PK":
        raise ValueError("NAR download did not return a ZIP archive")
    return payload, str(response.url)


def _decode_csv(payload: bytes) -> str:
    for encoding in ("utf-8-sig", "cp932", "shift_jis"):
        try:
            return payload.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise ValueError("unsupported NAR CSV encoding")


def read_archive(payload: bytes, *, max_uncompressed: int = 750_000_000) -> dict[str, list[dict[str, str]]]:
    files: dict[str, list[dict[str, str]]] = {}
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        total = sum(info.file_size for info in archive.infolist())
        if total > max_uncompressed:
            raise ValueError("NAR archive exceeds safety limit")
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if info.is_dir():
                continue
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("unsafe path in NAR archive")
            if path.suffix.lower() != ".csv":
                continue
            text = _decode_csv(archive.read(info))
            rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
            files[path.name] = rows
    if not files:
        raise ValueError("NAR archive contains no CSV files")
    return files


def normalize_races(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        laps = [_number(row.get(f"ハロンタイム{i}")) for i in range(1, 16)]
        corners = []
        for index in range(1, 9):
            name = str(row.get(f"コーナー名称{index}") or "").strip()
            order = str(row.get(f"コーナー通過順{index}") or "").strip()
            if name or order:
                corners.append({"name": name, "order": order})
        output.append({
            "track": str(row.get("競馬場") or "").strip(),
            "race_date": iso_date(row.get("競走年月日")),
            "race_no": int(_number(row.get("レース番号"), integer=True) or 0),
            "post_time": str(row.get("発走時刻") or "").strip(),
            "race_name": str(row.get("レース名") or "").strip(),
            # §27.2: 公式の競走種類名称(普通/特別/重賞/準重賞)。重賞ページの正本(名前の正規表現では
            # 道営・岩手などのローカル重賞を拾えない=2026-08-25 実測)
            "race_kind": str(row.get("競走種類名称") or "").strip(),
            "surface": str(row.get("芝ダート区分") or "").strip(),
            "direction": str(row.get("回り") or "").strip(),
            "distance_m": int(_number(row.get("距離"), integer=True) or 0),
            "weather": str(row.get("天候") or "").strip(),
            "going": str(row.get("馬場") or "").strip(),
            "field_size": int(_number(row.get("頭数"), integer=True) or 0),
            "condition": str(row.get("条件") or "").strip(),
            "prize_yen": [int(_number(row.get(f"{i}着賞金(円)"), integer=True) or 0) for i in range(1, 6)],
            "race_last4f": _number(row.get("上がり4F")),
            "race_last3f": _number(row.get("上がり3F")),
            "furlongs": [float(value) for value in laps if value is not None],
            "corners": corners,
        })
    return output


def normalize_horses(rows: Iterable[dict[str, str]]) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        output.append({
            "track": str(row.get("競馬場") or "").strip(),
            "race_date": iso_date(row.get("競走年月日")),
            "race_no": int(_number(row.get("レース番号"), integer=True) or 0),
            "gate": _number(row.get("枠番"), integer=True),
            "runner_number": int(_number(row.get("馬番"), integer=True) or 0),
            "horse_name": str(row.get("馬名") or "").strip(),
            "sex": str(row.get("性") or "").strip(),
            "age": _number(row.get("齢"), integer=True),
            # §38 R-0: 公式 horselist.csv の「生年月日」(2026-08 の 5,574行で充足率 100%)。
            # nar_runs は馬名しか鍵を持たず同名馬が実在する(2026-08 に ナナイロ が別個体2頭・同月出走)ので、
            # 馬名+生年月日 で個体を分けるために取り込む。血統登録番号を取りに行かなくても同定できる
            "birth_date": opt_iso_date(row.get("生年月日")),
            "sire": str(row.get("父馬名") or "").strip(),
            "dam": str(row.get("母馬名") or "").strip(),
            "broodmare_sire": str(row.get("母父馬名") or "").strip(),
            "jockey": str(row.get("騎手名") or "").strip(),
            "trainer": str(row.get("調教師") or "").strip(),
            # §38 R-0: 公式 horselist.csv の「調教師所属」(同 100%)。公式表記のまま入れる
            # (実測値= 北海道/岩手/浦和/船橋/大井/川崎/金沢/笠松/愛知/兵庫/高知/佐賀/ばんえい/JRA。
            #  「場名」ではなく主催者・地区の名前なので、場名へ寄せる変換はここではしない)
            "trainer_area": str(row.get("調教師所属") or "").strip(),
            "owner": str(row.get("馬主氏名") or "").strip(),
            "breeder": str(row.get("生産牧場名") or "").strip(),
            # 2026-09-04: 減量騎手は '◇54' '☆56' '▲52' '★51' のように記号が前に付く(float が失敗して null になっていた)。数字と記号に分ける
            "carried_weight": _number(_WEIGHT_MARK.sub("", str(row.get("負担重量") or ""))),
            "weight_mark": _weight_mark(row.get("負担重量")),
            "body_weight": _number(row.get("馬体重"), integer=True),
            "body_weight_change": _number(row.get("馬体重増減"), integer=True),
            "finish": _number(row.get("着順"), integer=True),
            "time_raw": str(row.get("タイム") or "").strip(),
            "margin": str(row.get("着差") or "").strip(),
            "last3f": _number(row.get("上がり3F")),
            "popularity": _number(row.get("人気"), integer=True),
        })
    return output


def normalize_odds(rows: Iterable[dict[str, str]], *, observed_at: str,
                   source_url: str, source_hash: str, scope: str) -> list[dict[str, Any]]:
    output = []
    for row in rows:
        raw_type = str(row.get("賭式") or "").strip().replace(" ", "")
        ticket_type = TICKET_NAMES.get(raw_type)
        if not ticket_type:
            continue
        width = 1 if ticket_type in {"win", "place"} else 2 if ticket_type in {"quinella", "exacta", "wide"} else 3
        try:
            combination = canonical_combination(ticket_type, [row.get(f"番号{i}") for i in range(1, width + 1)])
        except (TypeError, ValueError):
            continue
        odds_low = _number(row.get("オッズ"))
        odds_high = _number(row.get("オッズ（最大）")) or odds_low
        if odds_low is None or odds_low <= 0:
            continue
        output.append({
            "track": str(row.get("競馬場") or "").strip(),
            "race_date": iso_date(row.get("競走年月日")),
            "race_no": int(_number(row.get("レース番号"), integer=True) or 0),
            "ticket_type": ticket_type,
            "combination": combination,
            "odds_low": float(odds_low),
            "odds_high": float(odds_high),
            "popularity": _number(row.get("人気"), integer=True),
            "source_observed_at": observed_at,
            "source_url": source_url,
            "source_snapshot_hash": source_hash,
            "source_scope": scope,
            "decision_eligible": scope == "daily",
        })
    return output


def _add_payout(output: list[dict[str, Any]], row: dict[str, str], ticket_type: str,
                numbers: list[Any], payout_key: str, popularity_key: str,
                *, source_url: str, source_hash: str) -> None:
    payout = _number(row.get(payout_key), integer=True)
    if payout is None:
        return
    try:
        combination = canonical_combination(ticket_type, numbers)
    except (TypeError, ValueError):
        return
    output.append({
        "track": str(row.get("競馬場") or "").strip(),
        "race_date": iso_date(row.get("競走年月日")),
        "race_no": int(_number(row.get("レース番号"), integer=True) or 0),
        "ticket_type": ticket_type,
        "combination": combination,
        "official_payout_per_100": int(payout),
        "popularity": _number(row.get(popularity_key), integer=True),
        "settlement_quality": "official",
        "source_url": source_url,
        "source_snapshot_hash": source_hash,
    })


def normalize_payouts(rows: Iterable[dict[str, str]], *, source_url: str,
                      source_hash: str) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for row in rows:
        _add_payout(output, row, "win", [row.get("単勝組番")], "単勝払戻金（円）", "単勝人気", source_url=source_url, source_hash=source_hash)
        for index in range(1, 4):
            _add_payout(output, row, "place", [row.get(f"複勝組番{index}")], f"複勝払戻金{index}（円）", f"複勝人気{index}", source_url=source_url, source_hash=source_hash)
        _add_payout(output, row, "quinella", [row.get("馬複組番1"), row.get("馬複組番2")], "馬複払戻金（円）", "馬複人気1", source_url=source_url, source_hash=source_hash)
        _add_payout(output, row, "exacta", [row.get("馬単組番1"), row.get("馬単組番2")], "馬単払戻金（円）", "馬単人気1", source_url=source_url, source_hash=source_hash)
        for index in range(1, 4):
            _add_payout(output, row, "wide", [row.get(f"ワイド組番{index}馬番1"), row.get(f"ワイド組番{index}馬番2")], f"ワイド払戻金{index}（円）", f"ワイド人気{index}", source_url=source_url, source_hash=source_hash)
        _add_payout(output, row, "trio", [row.get("３連複組番馬番1"), row.get("３連複組番馬番2"), row.get("３連複組番馬番3")], "３連複払戻金（円）", "３連複人気", source_url=source_url, source_hash=source_hash)
        _add_payout(output, row, "trifecta", [row.get("３連単組番馬番1"), row.get("３連単組番馬番2"), row.get("３連単組番馬番3")], "３連単払戻金（円）", "３連単人気", source_url=source_url, source_hash=source_hash)
    return output


# §137 公式 payback.csv の「組番」と「払戻金」の列(normalize_payouts が使うのと同じ字)。
# 取り止めになった回は **組番が空のまま払戻金だけ 100**(全額返還)で来る。_add_payout は組番の無い行を
# 捨てるので、捨てる前にレース単位で見ないと「払戻なし」と見分けが付かない(2026-09-08 実測)。
_PAYBACK_COMBO_COLS = (
    "単勝組番", "複勝組番1", "複勝組番2", "複勝組番3",
    "馬複組番1", "馬複組番2", "馬単組番1", "馬単組番2",
    "ワイド組番1馬番1", "ワイド組番1馬番2", "ワイド組番2馬番1", "ワイド組番2馬番2",
    "ワイド組番3馬番1", "ワイド組番3馬番2",
    "３連複組番馬番1", "３連複組番馬番2", "３連複組番馬番3",
    "３連単組番馬番1", "３連単組番馬番2", "３連単組番馬番3",
)
_PAYBACK_YEN_COLS = (
    "単勝払戻金（円）", "複勝払戻金1（円）", "複勝払戻金2（円）", "複勝払戻金3（円）",
    "馬複払戻金（円）", "馬単払戻金（円）",
    "ワイド払戻金1（円）", "ワイド払戻金2（円）", "ワイド払戻金3（円）",
    "３連複払戻金（円）", "３連単払戻金（円）",
)
REFUND_YEN = 100        # 発売後に取り止め= 買った分は全額返る= 払戻金の欄が 100 で埋まる


def race_cancel_marks(payback_rows: Iterable[dict[str, str]],
                      horses: Iterable[dict[str, Any]]) -> dict[tuple[str, str, int], str | None]:
    """公式 payback.csv をレース単位で見て、取り止めの印を作る(§137)。

    戻り = {(競馬場, 競走年月日, レース番号): "refund" | "nosale" | None}。
      refund … 組番が1つも無く、払戻金に 100 がある = 発売後に取り止め(全額返還)
      nosale … 組番も払戻金も全部空                = 発売前に取り止め
      None   … 取り止めではない(印が入っていたら消す)

    ⛔payback 行の**無い**レースは鍵ごと返さない= 出馬表だけの先の日には触らない
      (実測 2026-09-07 13:27 の月次: racelist 420 行に対し payback は 292 行= まだ走っていない
       128 レースには payback 行が無い。開催前の朝の日次に至っては payback 0 行)。
    ⛔着順が1頭でもあるレースには印を付けない(走ったのだから取り止めではない)。
    ⛔組番が1つでもあれば売れて確定している= 印なし(一部の券種だけ返還でも走ったことに変わりはない)。
    ⛔上のどれでもない形(組番なしで 100 以外の払戻金)は**推定しない**= 印なし。
    ⛔同じレースに payback 行が2つあって食い違うときも印なし(安全側。実測 2026-09-08 の日次は 59 行 58 レース)。
    """
    finished = {(h.get("track"), h.get("race_date"), h.get("race_no"))
                for h in horses if h.get("finish") is not None}
    marks: dict[tuple[str, str, int], str | None] = {}
    for row in payback_rows:
        try:
            key = (str(row.get("競馬場") or "").strip(), iso_date(row.get("競走年月日")),
                   int(_number(row.get("レース番号"), integer=True) or 0))
        except ValueError:
            continue
        mark = None
        if key not in finished and not any(str(row.get(col) or "").strip() for col in _PAYBACK_COMBO_COLS):
            payouts = [_number(row.get(col), integer=True) for col in _PAYBACK_YEN_COLS]
            if any(value == REFUND_YEN for value in payouts):
                mark = "refund"
            elif all(value is None for value in payouts):
                mark = "nosale"
        if key in marks and (marks[key] is None or mark is None):
            mark = None                     # 同じレースの行どうしが食い違う= 印を付けない
        marks[key] = mark
    return marks


def normalize_archive(payload: bytes, *, kind: str, scope: str,
                      source_url: str, observed_at: str) -> dict[str, Any]:
    source_hash = digest_bytes(payload)
    files = read_archive(payload)
    document: dict[str, Any] = {
        "format": FORMAT,
        "kind": kind,
        "scope": scope,
        "source_url": source_url,
        "source_observed_at": observed_at,
        "source_snapshot_hash": source_hash,
        "files": sorted(files),
    }
    if kind == "race":
        race_rows = next((rows for name, rows in files.items() if name.endswith("_racelist.csv")), [])
        horse_rows = next((rows for name, rows in files.items() if name.endswith("_horselist.csv")), [])
        payout_rows = next((rows for name, rows in files.items() if name.endswith("_payback.csv")), [])
        races = normalize_races(race_rows)
        horses = normalize_horses(horse_rows)
        # §137 取り止めの印。payback 行のあるレースにだけ "cancelled" を付ける(無い日は鍵ごと付けない=
        # 投入側 load_nar_official.py の「無い列は送らない」に乗せて、先の日を null で上書きしない)
        marks = race_cancel_marks(payout_rows, horses)
        for race in races:
            key = (race["track"], race["race_date"], race["race_no"])
            if key in marks:
                race["cancelled"] = marks[key]
        document.update({
            "races": races,
            "horses": horses,
            "payouts": normalize_payouts(payout_rows, source_url=source_url, source_hash=source_hash),
        })
    else:
        odds_rows = [row for name, rows in files.items() if name.endswith("_odds.csv") for row in rows]
        document["odds"] = normalize_odds(
            odds_rows, observed_at=observed_at, source_url=source_url,
            source_hash=source_hash, scope=scope,
        )
    document["document_hash"] = digest(document)
    return document


def immutable_write(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(payload)
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind", choices=("race", "odds"), required=True)
    parser.add_argument("--scope", choices=("monthly", "daily"), required=True)
    parser.add_argument("--year", type=int)
    parser.add_argument("--month", type=int)
    parser.add_argument("--date")
    parser.add_argument("--output-dir", type=Path, default=Path("data/nar_official_csv"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    url = download_url(args.kind, scope=args.scope, year=args.year, month=args.month, race_date=args.date)
    observed = dt.datetime.now(dt.timezone.utc).isoformat()
    payload, final_url = download_archive(url)
    source_hash = digest_bytes(payload)
    stamp = (f"{args.year:04d}{args.month:02d}" if args.scope == "monthly" else str(args.date).replace("-", ""))
    raw_path = args.output_dir / "raw" / f"{stamp}_{args.kind}_{source_hash[:16]}.zip"
    immutable_write(raw_path, payload)
    document = normalize_archive(
        payload, kind=args.kind, scope=args.scope,
        source_url=final_url, observed_at=observed,
    )
    normalized_path = args.output_dir / "normalized" / f"{stamp}_{args.kind}_{source_hash[:16]}.json"
    immutable_write(normalized_path, json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8"))
    print(json.dumps({
        "raw": str(raw_path), "normalized": str(normalized_path),
        "source_snapshot_hash": source_hash,
        "rows": len(document.get("odds") or document.get("races") or []),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
