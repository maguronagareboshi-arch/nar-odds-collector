# -*- coding: utf-8 -*-
"""定期便の最終成功時刻(本番の表 nar_job_heartbeat・関数 nar_beat)を書く。監査 A8(2026-09-24)。

  from heartbeat import beat
  beat(url, key, "odds_tanfuku")                 # 書けた周だけ呼ぶ
  python heartbeat.py votes ok [補足]            # 便の段から(SUPABASE_URL / SUPABASE_SERVICE_KEY)

⛔標準ライブラリだけ。⛔表・関数がまだ無い/DB が読めないときも呼び手を落とさない(常に戻る・exit 0)。
⛔短い timeout(5 秒)= 2 分刻みのオッズを遅らせない。
"""
import json
import os
import sys
import urllib.request


def beat(url, key, job, ok=True, note=None, timeout=5):
    """nar_beat(p_job, p_ok, p_note) を REST で呼ぶ。True= 書けた。例外は出さない。"""
    try:
        url = (url or "").rstrip("/")
        if not url or not key:
            return False
        body = json.dumps({"p_job": job, "p_ok": bool(ok), "p_note": note}, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(f"{url}/rest/v1/rpc/nar_beat", data=body, method="POST",
                                     headers={"apikey": key, "Authorization": "Bearer " + key,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return True
    except Exception as e:                          # noqa: BLE001
        try:
            print(f"  heartbeat {job} が書けない(続行): {type(e).__name__}: {str(e)[:100]}", flush=True)
        except Exception:                           # noqa: BLE001
            pass
        return False


if __name__ == "__main__":
    a = sys.argv[1:]
    if len(a) >= 2 and a[1] in ("ok", "fail"):
        done = beat(os.environ.get("SUPABASE_URL", ""), os.environ.get("SUPABASE_SERVICE_KEY", ""),
                    a[0], a[1] == "ok", " ".join(a[2:]) or None, timeout=20)
        print(f"heartbeat {a[0]} {a[1]}: {'書けた' if done else '書けない(続行)'}")
    sys.exit(0)
