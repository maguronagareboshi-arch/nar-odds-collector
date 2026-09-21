-- §238b 楽天競馬からの遡り便が要る変更(適用は検品役。⛔Opus 実装は流さない)
-- 2026-09-21 / 本番 nar-official
--
-- 1) 新しい表 nar_race_votes = 券種ごとの「総票数」と「返還票数」。公式 ZIP には無い事実。
--    形は既に動いている nar_sales と同じ(1 レース 1 行・中身は jsonb)。
--    ⛔1 レース 9 行にすると 15 万レースで 135 万行になるため 1 行に畳む(nar_race_payouts と同じ考え)。
-- 2) nar_races.source に 'rakuten' を入れる。
--    2026-09-21 に本番を見たところ nar_races / nar_runs / nar_race_payouts に check 制約は
--    race_no(1..12)だけで、source は自由な text = **変更は要らない**(この SQL では触らない)。

create table if not exists public.nar_race_votes (
  track      text not null,
  race_date  date not null,
  race_no    smallint not null,
  votes      jsonb,                 -- {"win": 21518, "place": 7909, ...} 9 券種
  refunds    jsonb,                 -- 同じ鍵で返還票数
  source     text,                  -- 'rakuten'
  updated_at timestamptz not null default now(),
  primary key (track, race_date, race_no)
);

create index if not exists nar_race_votes_date_idx on public.nar_race_votes (race_date);

alter table public.nar_race_votes enable row level security;

-- 画面と同じ匿名キーで**読むだけ**(書きは service key)。既にある nar_sales / nar_race_payouts と同じ形。
drop policy if exists nar_race_votes_read on public.nar_race_votes;
create policy nar_race_votes_read on public.nar_race_votes
  for select to anon, authenticated using (true);

grant select on public.nar_race_votes to anon, authenticated;
