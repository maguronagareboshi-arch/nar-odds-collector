"""Archive already downloaded official input on the existing collector runner.

No HTTP, credentials, Supabase writes, models or betting. Opt-in by env only.
The output is published by the existing job's artifact step, never by this code.
"""
import datetime as dt,hashlib,json,os
from pathlib import Path
JST=dt.timezone(dt.timedelta(hours=9))
MAX_BYTES=8<<20

def archive_if_needed(payload,received_at,races,folder=None):
    folder=folder or os.environ.get('NAR_RESEARCH_ARCHIVE_DIR')
    if not folder:return {'status':'DISABLED'}
    received=dt.datetime.fromisoformat(received_at)
    if received.tzinfo is None:raise ValueError('RECEIPT_TIMEZONE')
    day=received.astimezone(JST).date();targets=[]
    if day>dt.date(2026,9,30):return {'status':'DEVELOPMENT_CAPTURE_PERIOD_ENDED'}
    for race in races:
        if race.get('race_date')!=day.isoformat():continue
        post=str(race.get('post_time') or '').replace(':','').zfill(4)
        if len(post)!=4 or not post.isdigit():continue
        try:start=dt.datetime.combine(day,dt.time(int(post[:2]),int(post[2:])),JST)
        except ValueError:continue
        if 300<=(start-received).total_seconds()<=480:
            targets.append({'track':race['track'],'race_no':int(race['race_no']),'scheduled_start':start.isoformat()})
    if not targets:return {'status':'OUTSIDE_PREOFF_WINDOW'}
    if not isinstance(payload,bytes) or not payload.startswith(b'PK') or len(payload)>4<<20:raise ValueError('SOURCE_FORMAT_OR_BOUND')
    root=Path(folder);root.mkdir(parents=True,exist_ok=True)
    used=sum(p.stat().st_size for p in root.rglob('*') if p.is_file())
    if used+len(payload)+16384>MAX_BYTES:return {'status':'ARCHIVE_RUN_CAP_REACHED'}
    h=hashlib.sha256(payload).hexdigest();obj=root/'objects'/(h+'.zip');obj.parent.mkdir(exist_ok=True)
    if not obj.exists():
        with obj.open('xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
    rec=dict(schema='NAR_PREOFF_CARD_ARCHIVE_V1',day=int(day.strftime('%Y%m%d')),received_at=received.isoformat(),sha256=h,
             source='EXISTING_OFFICIAL_DAILY_DOWNLOAD',path='objects/'+h+'.zip',targets=targets,
             github_run_id=os.environ.get('GITHUB_RUN_ID'),github_sha=os.environ.get('GITHUB_SHA'))
    receipts=root/'receipts';receipts.mkdir(exist_ok=True)
    name=received.strftime('%Y%m%dT%H%M%S%f')+'_'+h+'.json';path=receipts/name
    if not path.exists():
        with path.open('x',encoding='utf-8') as f:json.dump(rec,f,ensure_ascii=False);f.flush();os.fsync(f.fileno())
    return {'status':'SAVED','sha256':h,'races':len(targets)}
