-- 見張り nar-odds-watchdog(pg_cron・5 分おき)に「救助」を足す(2026-09-23)。
-- 教訓: 16:11〜17:28 に走行中の collect が 1 件も書けないまま回り続けた(走行機ごと keiba.go.jp に 404)。
--   見張りは collect を起こし直していたが、concurrency で後ろに積まれるだけで効かなかった。
-- 変更:
--   ①窓の判定から取り止めのレース(nar_races.cancelled あり)を外す(取れない印で起こし続けない)
--   ②窓が開いて 10 分以上たつのに 2 分刻みが 10 分書かれていない → rescue.yml も起こす
--     (rescue は collect の列の外で走り、DB の全券種が 10 分書かれず起動 10 分以上の collect だけを強制取り消し)
--   ⛔「窓が開いて 10 分」を待つのは、朝の最初のレースや前のレースから間が空いたとき、健康な便が
--     最初の刻みを書く前に取り消さないため。
select cron.alter_job(
  job_id := (select jobid from cron.job where jobname = 'nar-odds-watchdog'),
  command := $cmd$
do $body$
declare
  jst timestamp := now() at time zone 'Asia/Tokyo';
  now_min int := extract(hour from jst)::int * 60 + extract(minute from jst)::int;
  in_window boolean;
  open_long boolean;
  last_tick int;
  hdr jsonb;
begin
  if now_min < 9*60+55 or now_min >= 22*60 then return; end if;
  select exists (
    select 1 from nar_races r
    where r.race_date = jst::date and r.post_time ~ '^[0-9]{4}$' and r.cancelled is null
      and now_min between ((r.post_time::int/100)*60 + r.post_time::int%100) - 40
                      and ((r.post_time::int/100)*60 + r.post_time::int%100) + 6
  ) into in_window;
  if not in_window then return; end if;
  select max(split_part(asof,':',1)::int*60 + split_part(asof,':',2)::int) into last_tick
    from nar_odds_ticks where race_date = jst::date and asof ~ '^[0-9]{1,2}:[0-9]{2}$';
  if last_tick is not null and now_min - last_tick < 5 then return; end if;
  hdr := jsonb_build_object(
    'Authorization', 'Bearer ' || (select decrypted_secret from vault.decrypted_secrets where name = 'github_dispatch_token'),
    'Accept', 'application/vnd.github+json',
    'User-Agent', 'nar-dispatcher');
  insert into nar_dispatch_log(reason)
    values (format('watchdog: last tick %s min ago', coalesce((now_min - last_tick)::text, 'none')));
  perform net.http_post(
      url := 'https://api.github.com/repos/maguronagareboshi-arch/nar-odds-collector/actions/workflows/collect.yml/dispatches',
      headers := hdr,
      body := '{"ref":"main","inputs":{}}'::jsonb);
  -- 救助: 窓が開いて 10 分以上(発走 30 分前〜6 分後)のレースがあるのに、刻みが 10 分以上無い
  if last_tick is not null and now_min - last_tick < 10 then return; end if;
  select exists (
    select 1 from nar_races r
    where r.race_date = jst::date and r.post_time ~ '^[0-9]{4}$' and r.cancelled is null
      and now_min between ((r.post_time::int/100)*60 + r.post_time::int%100) - 30
                      and ((r.post_time::int/100)*60 + r.post_time::int%100) + 6
  ) into open_long;
  if not open_long then return; end if;
  insert into nar_dispatch_log(reason)
    values (format('watchdog: rescue (last tick %s min ago)', coalesce((now_min - last_tick)::text, 'none')));
  perform net.http_post(
      url := 'https://api.github.com/repos/maguronagareboshi-arch/nar-odds-collector/actions/workflows/rescue.yml/dispatches',
      headers := hdr,
      body := '{"ref":"main"}'::jsonb);
end
$body$
$cmd$
);
