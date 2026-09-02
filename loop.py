# -*- coding: utf-8 -*-
"""2 分おきに odds_tanfuku.py と odds_full.py を回す(GitHub Actions の 1 ジョブで最長 6 時間)。

  python loop.py --until 2200      # JST 22:00 まで(HHMM)。--every 秒(既定 120)
対象は「発走 40 分前〜8 分後」に絞る(発走が近いレースを密に、遠いレースは取らない)。
最終オッズが揃ったレースは各スクリプトが自分で飛ばす。1 周が --every 秒より長ければ、間を置かず次の周へ。
"""
import argparse
import datetime as dt
import subprocess
import sys
import time

JST = dt.timezone(dt.timedelta(hours=9))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--until", required=True, help="JST HHMM")
    ap.add_argument("--every", type=int, default=120)
    ap.add_argument("--before", type=int, default=40)
    ap.add_argument("--after", type=int, default=8)
    a = ap.parse_args()
    until = int(a.until[:2]) * 60 + int(a.until[2:])
    n = 0
    while True:
        now = dt.datetime.now(JST)
        if now.hour * 60 + now.minute >= until:
            print(f"[{now:%H:%M:%S}] {a.until} を過ぎたので終了(周回 {n})", flush=True)
            return 0
        t0 = time.time()
        n += 1
        print(f"===== 周回 {n} {now:%H:%M:%S} =====", flush=True)
        for script in ("odds_tanfuku.py", "odds_full.py"):
            rc = subprocess.call([sys.executable, script, "--before", str(a.before), "--after", str(a.after),
                                  "--limit", "40"])
            if rc:
                print(f"  {script} rc={rc}(次の周で取り直す)", flush=True)
        wait = a.every - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)


if __name__ == "__main__":
    sys.exit(main())
