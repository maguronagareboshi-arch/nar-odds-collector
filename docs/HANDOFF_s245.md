# §245 HANDOFF(nar-odds-collector 枝 s245・2026-09-23)
1. commit 1cc944b(origin/main 7a7f444 の上)= odds_full.py・loop.py・tests/test_wide_range.py・tests/fixtures/odds_wide_monbetsu_20260922_12r.html。
2. 直した所= 公式のワイド人気順は 2 列目が「1.3<br>-1.6」(下限と上限)で、_num が最初の数しか取らず上限が落ちていた。parse_ranking が 2 つ目の数を 4 つ目に持ち、combos_json でワイドだけ [a,b,下限,rank,上限] にする。先頭 4 つは不変= 古い読み手はそのまま。
3. 指紋 h は combos_json の md5 なので、上限だけ変わっても新しい周回として nar_odds_full_ticks に積まれる(切替直後の 1 周だけ全レース分の行が 1 回増える)。
4. 起動ログ= loop.py が最初の周回だけ odds_full.py に --announce を付け「対象の場: 門別 12R・浦和 12R…」を 1 行出す(nar_races の読みは今までと同じ 1 本)。
5. 検品= PYTHONUTF8=1 py -3.12 -m unittest discover -s tests= 104 OK(cp932 の端末だと test_votes_only の 3 件が「⚠」の印字で落ちる= 既存・中身は無関係)。公式の生ページは 2026-09-22 門別 12R を取得して写しにした。
6. 見張り= nar-jobs 枝 s245(f219f21)の nar-watchdog に job boards-yesterday(JST 08:40・前日に開催があった場で 7 券種の板が 0 なら赤)。読み 2 本・集計なし。
7. 残= push(main)はユーザー・本番の次の開催日に nar_odds_full の wide 行が 5 つになっているのを 1 レース anon で確認・nar-viewer 側(Opus 枝 s245)の表示。過去のワイドは片側のまま(遡れない)。
