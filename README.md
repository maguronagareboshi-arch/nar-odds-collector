# nar-odds-collector

地方競馬公式サイト(keiba.go.jp)の当日オッズ(枠連複・枠連単・馬連・馬単・ワイド・3連複・3連単)を
発走前後の短い窓だけ取得し、Supabase の表に保存する GitHub Actions ジョブです。標準ライブラリのみ。

- 2 分おきのループ(loop.py)。1 リクエスト 1 秒、対象は発走 40 分前〜8 分後のレースだけ。最終オッズが出たレースは以後取りません。
- 単勝・複勝(odds_tanfuku.py)と 7 券種(odds_full.py)の 2 本。
- 公式の数値をそのまま写します(人気は公式の同順位を保持)。
- 接続先は Secrets(`SUPABASE_URL` / `SUPABASE_SERVICE_KEY`)で与えます。
