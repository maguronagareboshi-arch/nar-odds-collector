commit: 3283c54 / 720752d (枝 s238c2 / origin/main から・push なし)
検品: 通信なしの検算のみ= `py -3.12 -m unittest discover -s tests -p "test_*.py"` 99 本 ALL PASS(新 9 本)・yml は PyYAML で読めて手順名に「: 」なし。⛔本番 DB には触っていない
通信: +0(取得元へのアクセスは 1 秒 1 ページのまま。同時に動く月の本数を 10→3 に既定変更= 本番 DB への同時書き込みは減る)
回帰: 触ったのは票数の便だけ / 毎日便 votes-daily・遡り便 rakuten-backfill への影響= upsert の出直しが 3 回 3 秒→5 回 30 秒になる(強くなる方向)
変えた点: rakuten_backfill.py= ①upsert は時間切れ(HTTP0)と 5xx を 30 秒あけて 5 回まで出直す・4xx は出直さない②--list-months --votes-only --pending= done でない月だけ出す / votes-backfill.yml= max_parallel(既定 3)・pending 入力 / tests 90→99 本
⚠: 9/22 の run 35670546144 の失敗 10 本は**取り消しのせいではない**= 10 本とも 1 か月ぶん取り終えた後の nar_race_votes 投入が「HTTP0 The read operation timed out (batch 0)」で rc=1。取れた月= 2025-12〜2026-09 の 10 本だけ
要判断: push の可否(公開リポはユーザー)。流し直しは votes-backfill を months=2022-11..2026-09・pending=true・max_parallel=3 で 1 本(残り 37 本= 失敗 10 本 2025-02〜2025-11 と 取り消し 27 本 2022-11〜2025-01)
