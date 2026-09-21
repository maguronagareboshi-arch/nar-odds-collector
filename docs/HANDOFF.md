commit: 5e50da0(枝 s238b・6 コミット・push なし)。作った物= rakuten_parse.py(解析)・rakuten_backfill.py(便)・sql/rakuten_backfill_20260921.sql・.github/workflows/rakuten-backfill.yml・tests/test_rakuten_parse.py(28 本・全 74 本 OK)。

便= カレンダー→払戻の日ページ→成績のレース→5 表へ upsert。1 秒 1 ページ・並列なし・生 HTML は残さない・公式のある 2022-11 以降は取らない・5.5 時間で自停し続きは nar_meta 'rakuten_backfill:v1' から。

2022-10 の --dry= 154 ページ(払戻の日ページ 119 枚= 1 か月ぶん全部・成績は 3 日×場 34 枚に絞り)。行= payouts 1,394 / votes 1,394(その月の全レース)/ races 34 / runs 340。詳細= docs/notes_s238b_horse_id.md。

欠け率(楽天 2022-10 の 3 場 / 公式 2022-11 の同じ 3 場)= corners 35.3/45.5・surface 35.3/45.5・body_weight 0.9/0.6・margin 42.4/49.3・last3f 35.9/43.8。⚠race_last3f/4f は 100/45.5(高知・佐賀は楽天に無い・南関はある)。

corners= 公式と同じ [{name,order}]。facts.py と同じ規則で 2022-10 と 2014-01 の 6 コーナー 100% 解析・頭数も一致。名前は場で違う(３コーナー/１角)が位置で決めるので影響なし。2014-01 も同じ便で通った。

馬 ID= 楽天の ID は登録番号ではない(公式の生年と 0/154)。捨てずに新表 nar_horse_ext_ids へ(⛔nar_runs には列を足さない・9 桁は 0 詰め)。繋ぎは馬名で 2022-11-01 は 576/576・大井 154 頭の 14 列は 100% 一致。

追加分= 枠複/枠単を落とさず 'wakuren'/'wakutan' で払戻の JSON に足した(公式の 7 種の鍵は不変・川崎 2014 の 11 レースで検算)。race_name は切らない・direction は null のまま。未了= 本番に 1 行も書いていない(冪等とAction からの到達は未実測)。
