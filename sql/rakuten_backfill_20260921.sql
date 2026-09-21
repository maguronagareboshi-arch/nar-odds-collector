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

-- 3) 新しい表 nar_horse_ext_ids = よそのサイトが馬に付けている ID を捨てずに残す置き場。
--    ⛔nar_runs には列を足さない(その表を読む全員に影響が出るため)。別の表として置く。
--    楽天の ID は血統登録番号ではない(2022-11-01 大井 154 頭で公式の生年と 0/154 一致)。同名馬を
--    分ける手がかりとして残すだけで、今は誰も読まない。birth_year は ID の 3〜6 桁目(登録の年らしい
--    数字)をそのまま入れる= ⛔生年として使わない。

create table if not exists public.nar_horse_ext_ids (
  source     text not null,          -- 'rakuten'
  ext_id     text not null,          -- 10 桁(先頭の 0 が落ちるので 0 詰めして入れる)
  horse_name text,
  birth_year int,
  first_seen date,
  last_seen  date,
  updated_at timestamptz not null default now(),
  primary key (source, ext_id)
);

create index if not exists nar_horse_ext_ids_name_idx on public.nar_horse_ext_ids (horse_name);

alter table public.nar_horse_ext_ids enable row level security;

drop policy if exists nar_horse_ext_ids_read on public.nar_horse_ext_ids;
create policy nar_horse_ext_ids_read on public.nar_horse_ext_ids
  for select to anon, authenticated using (true);

grant select on public.nar_horse_ext_ids to anon, authenticated;

-- 便は新しい月から古い月へ遡るので、upsert がそのまま上書きすると last_seen が古い日に戻ってしまう。
-- 見た日の**幅**が広がるだけにする(何度流しても同じ結果)。
create or replace function public.nar_horse_ext_ids_widen() returns trigger
language plpgsql as $$
begin
  new.first_seen := least(coalesce(new.first_seen, old.first_seen), coalesce(old.first_seen, new.first_seen));
  new.last_seen  := greatest(coalesce(new.last_seen, old.last_seen), coalesce(old.last_seen, new.last_seen));
  new.horse_name := coalesce(new.horse_name, old.horse_name);
  new.birth_year := coalesce(new.birth_year, old.birth_year);
  return new;
end $$;

drop trigger if exists nar_horse_ext_ids_widen_trg on public.nar_horse_ext_ids;
create trigger nar_horse_ext_ids_widen_trg before update on public.nar_horse_ext_ids
  for each row execute function public.nar_horse_ext_ids_widen();
