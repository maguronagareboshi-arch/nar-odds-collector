# nar-odds-collector

地方競馬公式サイト(keiba.go.jp)の当日オッズ(枠連複・枠連単・馬連・馬単・ワイド・3連複・3連単)を
発走前後の短い窓だけ取得し、Supabase の表に保存する GitHub Actions ジョブです。標準ライブラリのみ。

- 取得間隔は 1 リクエスト 1 秒、対象は発走 60 分前〜10 分後のレースだけ(最大 10 レース/回)。
- 公式の数値をそのまま写します(人気は公式の同順位を保持)。
- 接続先は Secrets(`SUPABASE_URL` / `SUPABASE_SERVICE_KEY`)で与えます。
