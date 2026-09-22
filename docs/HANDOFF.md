commit: 7b9c12a・b8f8805 枝 s238c(origin/main f8c7f02 から)。設計= docs/proposal_s238c_coverage_20260922.md §3。⛔push なし・本番への書き込み 0・鍵は読んでいない。

作った物= rakuten_backfill.py に --date と --votes-only / tests/test_votes_only.py 8 本(全 91 本 ALL PASS)/ votes-daily.yml(cron 20:40 UTC= 05:40 JST・前日)/ votes-backfill.yml(months・月ごと matrix・10 並列)。

--votes-only= 払戻の日ページ 1 枚だけ取り、書くのは nar_race_votes だけ(pick_tables が表を絞る)。払戻・成績・馬 ID は作らない= 公式のある日でも公式を上書きしない。票数の無いレースは行を作らない(⛔0 で埋めない)。

公式のある期間の扱い= date_days / month_days は votes_only のとき OFFICIAL_FROM の門を外す(票数は 2022-11 以降の公式 ZIP に無いため)。votes_only なしの今までの遡りは門も出力も不変(tests で確認)。今日より後の日は取らない。

進み具合= nar_meta 'rakuten_votes:v1'(⛔遡り便の 'rakuten_backfill:v1' と混ぜない)。--list-months --votes-only は範囲を 2022-11〜当月に読み替える。冪等の確認(on_conflict=主キー)は今までどおり起動時に走る。

検品(--date 2026-09-21 --votes-only --dry)= 取得 5 ページ 4 秒(1 秒 1 ページ)・票数 43 R・作った表は votes だけ。答え合わせ= 本番 nar_races 同日の場ごとの数(佐賀 9・帯広ば 12・水沢 12・金沢 10)と 43/43 一致。

⚠毎日便は 05:40 JST に 1 回・約 5 ページ 2 分(Actions の分数は月 60 分ほど)。未了= 本番に 1 行も書いていない。要判断= ①Secrets は既存 SUPABASE_URL/SERVICE_KEY 流用でよいか ②過去分は votes-backfill を months=2022-11..2026-09(47 本)で 1 回流すか。
