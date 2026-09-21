commit: 202c00c(枝 s238b・8 コミット・push なし)。作った物= rakuten_parse.py(解析)・rakuten_backfill.py(便)・sql/rakuten_backfill_20260921.sql・.github/workflows/rakuten-backfill.yml・tests/test_rakuten_parse.py(37 本・全 83 本 OK)。

便= カレンダー→払戻の日ページ→成績のレース→5 表へ upsert。⛔1 本のジョブの中は 1 秒 1 ページ・並列なし。月ごとに別ジョブへ分け同時 10 本まで(fail-fast なし・月ごとの concurrency・90 分/ジョブ・中は 80 分で自停)。

早さ= months は範囲('2014-01..2022-10'= 106 本)かカンマ区切り。最初の小さなジョブが到達確認(トップ 1 回 GET)と月の一覧を作り matrix に渡す。429/503 は 60 秒あけて最大 5 回出直し、待った回数を最後に出す。

冪等= 5 表の on_conflict が主キーと同じことを起動時に確かめ、違えば rc=2 で止まる(本番の主キーを 9/21 に確認= races/payouts/votes は track,race_date,race_no・runs は +runner_number・ext_ids は source,ext_id・meta は key)。

2022-10 の --dry= 154 ページ。行= payouts 1,394 / votes 1,394(その月の全レース)/ races 34 / runs 340(成績は 3 日×場に絞り)。欠け率と答え合わせの表= docs/notes_s238b_horse_id.md。

corners は公式と同じ形で facts.py が 100% 解析。馬 ID は登録番号でなく新表 nar_horse_ext_ids へ(nar_runs に列は足さない)。枠複/枠単は 'wakuren'/'wakutan' で払戻に足した(公式の 7 種は不変)。

⚠10 本並ぶと取得元から見れば最大 10 ページ/秒になる(1 本の中は 1 秒 1 ページのまま)。未了= 本番に 1 行も書いていない・Actions から取れるかは plan ジョブの到達確認で分かる。
