# -*- coding: utf-8 -*-
"""odds_tanfuku.py と odds_full.py(と当日の確定結果 results_today.py)を回す(GitHub Actions の 1 ジョブで最長 6 時間)。

  python loop.py --until 2200              # JST 22:00 まで(HHMM)。--every 秒
  python loop.py --until 2200 --lane hot   # 発走が近いレースだけを 1 分刻みで
対象は「発走 40 分前〜8 分後」に絞る(発走が近いレースを密に、遠いレースは取らない)。
最終オッズが揃ったレースは各スクリプトが自分で飛ばす。1 周が --every 秒より長ければ、間を置かず次の周へ。

車線(--lane)= 発走までの残り分で 2 つに分け、同じジョブで並べて走らせるためのもの。どちらが取るかは
残り分だけで決まって重ならないので、同じレースを 2 回取ることはない。
  all(既定) … 発走 40 分前〜8 分後を 120 秒ごと。1 本で回していたときと同じ。
  far       … 発走 40 分前〜10 分前を 120 秒ごと。下のおまけもこちらで回す。
  hot       … 発走 10 分前〜8 分後を 60 秒ごと。おまけは回さない(オッズだけを 1 分刻みにする)。
  morning   … 発走 600 分前〜40 分前を 1800 秒ごと(§234・2026-09-20)。朝 10 時ごろの公開から 30 分おきに
              単勝・複勝だけを写す。残り 40 分以下は far に任せる。全券種(odds_full.py)もおまけも回さない。

2 分ごとに要らないものは、周回を間引いて回す(オッズの刻みを遅らせないため):
  post_time_refresh.py … 5 周に 1 回(約 10 分)。発走時刻は当日ずれることがある。
  sales_rakuten.py     … 3 周に 1 回(約 6 分)・1 回 1 場。取得元が Crawl-Delay: 60 なので続けて取らない。
"""
import argparse
import datetime as dt
import os
import subprocess
import sys
import time

JST = dt.timezone(dt.timedelta(hours=9))

# 車線 → (周期秒, 発走の何分前から, 何分後まで, 残りがこれ以下は相手に任せる(None=分けない), おまけを回すか)
LANES = {
    "all": (120, 40, 8, None, True),
    "far": (120, 40, 8, 10, True),
    "hot": (60, 10, 8, None, False),
    "morning": (1800, 600, 0, 40, False),
}
LIMIT = 40          # 1 回の実行で取るレース数の上限(窓に入るのは多くて 6 前後)
# 朝の車線だけは窓が広く、当日の未発走レースがほぼ全部入る(3〜4 場 × 12 レース)。40 では足りないので増やす。
LANE_LIMIT = {"morning": 60}
# 全券種(3 連単まで)を回さない車線。⛔朝は単複だけ= 重い表の行数と HTTP を増やさない(§234)。
TANFUKU_ONLY = {"morning"}
ODDS_SCRIPTS = ("odds_tanfuku.py", "odds_full.py")

MAX_RUN_SECONDS = 345 * 60  # Leave 15 minutes before GitHub's six-hour hard stop.


def bounded_child(command, env, deadline):
    """Stop this lane cleanly at its budget, including a slow child request."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    try:
        return subprocess.call([sys.executable] + command, env=env, timeout=remaining)
    except subprocess.TimeoutExpired:
        return None


def lane_plan(lane, every=None, before=None, after=None, min_before=None):
    """車線の既定に、明示された指定を上書きしたもの。"""
    e, b, a, mb, extras = LANES[lane]
    return {"every": e if every is None else every,
            "before": b if before is None else before,
            "after": a if after is None else after,
            "min_before": mb if min_before is None else min_before,
            "extras": extras,
            "limit": LANE_LIMIT.get(lane, LIMIT),
            "scripts": ("odds_tanfuku.py",) if lane in TANFUKU_ONLY else ODDS_SCRIPTS}


def odds_cmd(script, plan, limit=None):
    """オッズ 1 本ぶんの引数。⛔車線を分けない周回では --min-before を付けない= 今までと同じ行。"""
    if limit is None:
        limit = plan.get("limit", LIMIT)
    cmd = [script, "--before", str(plan["before"]), "--after", str(plan["after"]), "--limit", str(limit)]
    if plan["min_before"] is not None:
        cmd += ["--min-before", str(plan["min_before"])]
    return cmd


def extra_cmds(plan, n):
    """周回 n で回すおまけ。⛔おまけを持たない車線では空= オッズの刻みを遅らせない。"""
    if not plan["extras"]:
        return []
    # 当日の確定結果・払戻(results_today.py)は毎周= 公式 ZIP は発走 6〜9 分後に載る。変化が無ければ 0.3 秒で戻る
    extra = [["results_today.py"]]
    if n % 5 == 1:
        extra.append(["post_time_refresh.py"])
    if n % 3 == 2:
        extra.append(["sales_rakuten.py", "--max-tracks", "1"])
    return extra


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", required=True, help="JST HHMM")
    ap.add_argument("--lane", choices=sorted(LANES), default="all", help="車線(既定 all= 1 本で全部)")
    ap.add_argument("--every", type=int, help="周期秒(既定は車線ごと)")
    ap.add_argument("--before", type=int, help="発走の何分前から(既定は車線ごと)")
    ap.add_argument("--after", type=int, help="発走の何分後まで(既定は車線ごと)")
    ap.add_argument("--min-before", type=int, help="発走までの残りがこれ以下は相手の車線に任せる")
    ap.add_argument("--start", type=int, default=9 * 60 + 30, help="この時刻(JST 分)より前に起動したら何もしない")
    ap.add_argument("--max-run-seconds", type=int, default=MAX_RUN_SECONDS,
                    help="収集を正常終了する実行秒数(最大20700秒)。次の既存ジョブへ引き継ぐ")
    a = ap.parse_args()
    if not 0 < a.max_run_seconds <= MAX_RUN_SECONDS:
        ap.error("--max-run-seconds must be between 1 and 20700")
    plan = lane_plan(a.lane, a.every, a.before, a.after, a.min_before)
    until = int(a.until[:2]) * 60 + int(a.until[2:])
    # 深夜〜早朝に(遅れて)起動した回は何もせず終わる= 6 時間の枠を空回りで使い切り、朝の回を待たせないため
    # (実測 2026-09-03 01:39 JST に schedule が着火し、until=2200 で回り続けていた)
    now = dt.datetime.now(JST)
    if now.hour * 60 + now.minute < a.start:
        print(f"[{now:%H:%M:%S}] 開始時刻 {a.start // 60:02d}:{a.start % 60:02d} より前なので終了", flush=True)
        return 0
    # 2 本並べて回すとき、子のログもどちらの車線か分かるようにする(1 本のときは付けない= 今までどおり)
    env = dict(os.environ)
    if a.lane != "all":
        env["COLLECTOR_TAG"] = f"[{a.lane}]"
    note = f"・残り {plan['min_before']} 分以下は別の車線" if plan["min_before"] is not None else ""
    print(f"[{now:%H:%M:%S}] 車線 {a.lane}= {plan['every']} 秒ごと・発走 {plan['before']} 分前〜"
          f"{plan['after']} 分後{note}・{'/'.join(plan['scripts'])}"
          f"・おまけ {'あり' if plan['extras'] else 'なし'}", flush=True)
    n = 0
    deadline = time.monotonic() + a.max_run_seconds
    while True:
        now = dt.datetime.now(JST)
        if time.monotonic() >= deadline:
            print(f"[{now:%H:%M:%S}] 収集の実行上限前に正常終了。保存後、次の既存ジョブへ引継ぎ", flush=True)
            return 0
        if now.hour * 60 + now.minute >= until:
            print(f"[{now:%H:%M:%S}] {a.until} を過ぎたので終了(車線 {a.lane}・周回 {n})", flush=True)
            return 0
        t0 = time.time()
        n += 1
        print(f"===== {a.lane} 周回 {n} {now:%H:%M:%S} =====", flush=True)
        for script in plan["scripts"]:
            rc = bounded_child(odds_cmd(script, plan), env, deadline)
            if rc is None:
                print("収集の実行枠が終了。成果物保存のため正常終了", flush=True)
                return 0
            if rc:
                print(f"  {script} rc={rc}(次の周で取り直す)", flush=True)
        # 間引いて回すもの(落ちても次の周に任せる= オッズ本体は止めない)
        for cmd in extra_cmds(plan, n):
            rc = bounded_child(cmd, env, deadline)
            if rc is None:
                print("収集の実行枠が終了。成果物保存のため正常終了", flush=True)
                return 0
            if rc:
                print(f"  {cmd[0]} rc={rc}(次の周で取り直す)", flush=True)
        wait = min(plan["every"] - (time.time() - t0), deadline - time.monotonic())
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
