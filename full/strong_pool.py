"""Independent local strong-pool publication and validated production reference."""
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path
from statistics import median
from threading import Lock

from rules_lab.paths import ROOT, atomic_write_json, read_json

ZONE = timezone(timedelta(hours=8))
PROD_DATA = Path(os.environ.get('STARGAZER_PROD_DATA') or ROOT.parents[1]/'stargazer_desktop/dist/data')
PROD_ROOT = PROD_DATA/'postclose_risk/strong_sector'
STORE = ROOT/'data'
CONTRACT = 'STRONG_SECTOR_PULLBACK_OBSERVATION_20261104_V1'
LOCK = Lock()
ALIASES = {'pcb':'PCB概念','cpo':'共封装光学(CPO)','存储':'存储芯片','煤炭':'煤炭概念',
           '数据中心':'数据中心(AIDC)'}


def _read(path):
    try:
        value = read_json(path)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def content_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()


def watch_config():
    doc = _read(STORE/'board_watch.json')
    if 'boards' in doc:
        return doc
    seed = _read(PROD_DATA/'user_watch_sectors.json')
    return dict(enabled=False, boards=list(dict.fromkeys(seed.get('names', []))))


def strong_watch():
    local = _read(STORE/'strong_watch.json')
    if isinstance(local.get('boards'),list):
        return local['boards']
    from full.vipdoc_reader import vipdoc_root
    from plan_board_change_v1 import current_boards
    _, boards = current_boards(PROD_DATA/'trade_plan.json', PROD_ROOT.parent/'board_identities')
    return list(dict.fromkeys(b['name'] for b in boards))


def _catalog():
    from full.vipdoc_reader import vipdoc_root
    ths = vipdoc_root().parent/'ths'
    return _read(ths/'catalog/index_codes.json')


def identity(token, *, catalog=None):
    # The shared reader validates the cached constituent digest. The store is read-only here.
    from full.vipdoc_reader import vipdoc_root
    from ths_board_identity_v1 import resolve_for_pool
    store = PROD_ROOT.parent/'board_identities'
    record = resolve_for_pool(token, store)
    alias = ALIASES.get(token.casefold(), token)
    if not record and alias!=token:
        record = resolve_for_pool(alias,store)
    if record:
        return record
    target = alias.casefold()
    matches = [v for v in (catalog if catalog is not None else _catalog()).values() if isinstance(v,dict)
               and target in (str(v.get('name','')).casefold(), str(v.get('index_code','')))
               and v.get('name') and v.get('index_code')]
    unique = {str(v['index_code']):v for v in matches}
    if len(unique) != 1:
        return None
    code, value = next(iter(unique.items()))
    record = resolve_for_pool(value['name'], store)
    if record:
        return record
    # Locally downloaded THS constituent snapshots use the same verification format.
    from ths_board_identity_v1 import constituent_record
    item = dict(name=value['name'], index_code=code, key='ths:'+code, source='ths',
                type=value.get('kind','concept'), web_id=str(value.get('web_id',code)))
    members, provenance = constituent_record(item, vipdoc_root().parent/'ths')
    return dict(identity=item,members=members,provenance=provenance,
                status='IDENTITY_CONFIRMED' if members is not None else 'CONSTITUENTS_PENDING')


def change_watch(action, name=None, *, strong=False):
    with LOCK:
        filename = 'strong_watch.json' if strong else 'board_watch.json'
        cfg = dict(boards=strong_watch()) if strong else watch_config()
        if action == 'add':
            record = identity(name) if name else None
            if not record:
                raise ValueError('板块身份未确认，请输入同花顺准确名称或指数代码')
            item = record['identity']
            code = item.get('index_code')
            if item['name'] not in cfg['boards'] and (not code or not any(
                    (identity(b) or {}).get('identity',{}).get('index_code')==code for b in cfg['boards'])):
                cfg['boards'].append(item['name'])
        elif action == 'remove':
            cfg['boards'] = [b for b in cfg['boards'] if b != name]
        elif not strong and action in ('enable','disable'):
            if action == 'enable' and not (cfg['boards'] or strong_watch()):
                raise ValueError('请选择至少一个关注板块')
            cfg['enabled'] = action == 'enable'
        else:
            raise ValueError('无效的板块监控操作')
        atomic_write_json(STORE/filename, cfg)
        return cfg


def _valid_pool(pool):
    try:
        return (pool.get('status')=='READY' and pool.get('contract')==CONTRACT
                and isinstance(pool['boards'], list) and isinstance(pool['rows'], list)
                and 0 < int(pool['data_date']) < int(pool['observation_date'])
                and content_hash(pool['rows']) == pool['rows_sha256'])
    except (KeyError, ValueError, TypeError):
        return False


def _archive_pool_bytes(day, filename, digest):
    import base64
    import gzip
    import zlib
    for path in (PROD_DATA/'review_archive/strong_sector'/str(day)).glob('*.json.gz'):
        try:
            body = gzip.decompress(path.read_bytes())
            if hashlib.sha256(body).hexdigest()+'.json.gz' != path.name:
                continue
            archive = json.loads(body)
            if archive.get('contract')!='STRONG_SECTOR_DAILY_REVIEW_V1' or archive.get('day')!=day:
                continue
            item = archive['files'].get('pools/'+filename)
            if item and item.get('sha256')==digest:
                raw = base64.b64decode(item['base64'],validate=True)
                if len(raw)==item['bytes'] and hashlib.sha256(raw).hexdigest()==digest:
                    return raw
        except (OSError, EOFError, ValueError, KeyError, TypeError, zlib.error):
            continue
    return None


def _production_pool(*, allow_archive=False):
    jobs = sorted((_read(f) for f in (PROD_ROOT/'prep_jobs').glob('*.json')),
                  key=lambda j:str(j.get('finished_at') or ''), reverse=True)
    for job in jobs:
        if job.get('state') != 'READY' or not job.get('pool_file') or not job.get('pool_sha256'):
            continue
        file = PROD_ROOT/'pools'/Path(job['pool_file']).name
        try:
            if file.is_file():
                raw = file.read_bytes()
            elif allow_archive:
                raw = _archive_pool_bytes(job['observation_date'],file.name,job['pool_sha256'])
            else:
                continue
            if not raw or hashlib.sha256(raw).hexdigest() != job['pool_sha256']:
                continue
            pool = json.loads(raw)
            if _valid_pool(pool) and all(pool.get(k)==job.get(k) for k in ('boards','data_date','observation_date','source_key')):
                causal = str(pool.get('built_at','9'*14)) < str(job['observation_date'])+'093000'
                manual = pool.get('manual_refresh') is True and pool.get('enabled_at')==pool.get('built_at')
                if (causal or manual) and isinstance(pool.get('constituent_versions'),dict) and pool['constituent_versions']==job.get('member_versions'):
                    return pool
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return None


def _day_pair(now, calendar):
    today = int(now.strftime('%Y%m%d'))
    previous = [d for d in calendar if d < today or (d==today and now.hour>=15)]
    if previous:
        future = [d for d in calendar if d > previous[-1]]
        if future:
            return previous[-1], future[0]
    raise ValueError('交易日历未覆盖当前日期')


def prepare(now=None):
    from full.cli import _verify_snapshot
    from full.vipdoc_reader import calendar_days, stock_names
    from rules_lab.snapshot import open_snapshot
    from strong_sector_rules_v1 import build
    from strong_pullback_policy_v1 import build_map

    now = now or datetime.now(ZONE)
    if not LOCK.acquire(blocking=False):
        return dict(ok=False, error='板块配置或强势池正在处理')
    try:
        boards = strong_watch()
        if not boards:
            raise ValueError('请先添加监控板块')
        day, observation = _day_pair(now, calendar_days())
        _verify_snapshot()
        snap = open_snapshot(day,daily_only=True)
        if not any(snap.daily_row(c,day) for c in snap.codes()):
            raise ValueError(f'规则快照缺 {day} 日线，请先在数据中心导入')
        members, board_meta = _memberships(boards)
        reverse = {}
        for board, codes in members.items():
            date = str(board_meta[board]['member_source_date'] or '')
            if not date.isdigit() or int(date)>day:
                codes = []
            for code in codes:
                reverse.setdefault(code, []).append(board)
        rows, examined = [], set()
        names = stock_names()
        for code, memberships in sorted(reverse.items()):
            if not names.get(code):
                continue
            history = snap.daily_asof(code,day)
            if len(history) < 62 or history[-1]['date'] != day:
                continue
            recent = history[-18:]
            try:
                # Same rolling 14-day true range then five-day median used by Tarot.
                ranges = [max(r['high']-r['low'],abs(r['high']-r['pre_close']),
                              abs(r['low']-r['pre_close'])) for r in recent]
                atr = median(sum(ranges[i:i+14])/14 for i in range(5))
                facts = build(history,day,atr)
                if facts.get('reason') in ('RECENT_VOLUME_UNKNOWN','TOP_VOLUME_UNKNOWN'):
                    continue
                examined.add(code)
                if facts['pool'] != 'MONITOR' or facts['structure']=='TRIPLE':
                    continue
                if any(s in names[code].upper() for s in ('ST','退')) or history[-1].get('is_st'):
                    continue
                bands = build_map(history,day)
            except (ValueError, KeyError, TypeError):
                continue
            if not bands:
                continue
            positions = [dict(lower=max(.01,b['lower']-.2*atr), upper=b['upper']+.2*atr,
                              node_lower=b['lower'], node_upper=b['upper'],primary=b['primary'],
                              source_waves=b['source_waves'],tolerance_basis='0.2_PREVIOUS_DAILY_ATR') for b in bands]
            rows.append(dict(code=code, name=names[code], boards=memberships, positions=positions,
                             reference_close=facts['reference_close'], neckline=facts['neckline'],
                             wave_return=facts['wave_return'],pool='MONITOR',atr_ref=atr))
        if not examined:
            raise ValueError('没有覆盖到可核验的板块成分与日线，旧池保留')
        if len(rows)>512:
            raise ValueError('强势池超过 512 只，请缩小监控板块范围')
        pool = dict(contract=CONTRACT,status='READY',boards=boards,rows=rows,rows_sha256=content_hash(rows),
                    data_date=day,observation_date=observation,built_at=int(now.strftime('%Y%m%d%H%M%S')),
                    snapshot_fingerprint=snap.fingerprint, price_basis=snap.price_basis,
                    constituents={b:dict(meta, evaluated=sum(b in reverse.get(c,[]) for c in examined))
                                  for b,meta in board_meta.items()})
        atomic_write_json(STORE/'strong_pool.json',pool)
        return dict(ok=True, qualified_count=len(rows), evaluated_count=len(examined), observation_day=observation)
    finally:
        LOCK.release()


def _memberships(boards):
    members, info = {}, {}
    catalog = _catalog()
    for board in boards:
        record = identity(board,catalog=catalog) or {}
        codes = record.get('members')
        provenance = record.get('provenance') or {}
        members[board] = list(dict.fromkeys(c for v in codes or [] if
                                           len(c := str(v).zfill(6))==6 and c.isdigit()))
        item = record.get('identity') or {}
        info[board] = dict(identity_index_code=item.get('index_code'),identity_name=item.get('name'),
                           member_count=len(members[board]) if codes is not None else None,
                           member_source=provenance.get('kind'),
                           member_source_date=provenance.get('snapshot_date') or provenance.get('day'))
    return members, info


def current_pool(now=None, *, for_monitor=False):
    now = now or datetime.now(ZONE)
    today = int(now.strftime('%Y%m%d'))
    local = _read(STORE/'strong_pool.json')
    pool, source = (local,'LOCAL_RULE_SNAPSHOT') if _valid_pool(local) else (None,None)
    if pool is None or int(pool['observation_date'])<today or (for_monitor and int(pool['observation_date'])!=today):
        production = _production_pool(allow_archive=not for_monitor)
        if production and ((for_monitor and int(production['observation_date'])==today) or
                           pool is None or int(production['observation_date'])>int(pool['observation_date'])):
            pool, source = production,'PRODUCTION_POOL_READONLY'
    return pool, source


def load_or_compute(now=None):
    from full.vipdoc_reader import calendar_days
    from strong_pool_display_v1 import nearby_observation_zones

    now = now or datetime.now(ZONE)
    try:
        expected_day = _day_pair(now,calendar_days())[1]
    except (OSError,ValueError,KeyError):
        expected_day = None
    scope = strong_watch()
    pool, source = current_pool(now)
    ready_for_target = pool is not None and expected_day==int(pool['observation_date'])
    rows = pool['rows'] if pool else []
    members, meta = _memberships(scope)
    del members
    boards_out = []
    for board in scope:
        current = meta[board]
        canonical = current['identity_name']
        key = next((b for b in (pool or {}).get('boards',[]) if b in (board,canonical) or
                    ALIASES.get(b.casefold(), b) in (board,canonical)),None)
        published = (pool or {}).get('constituents',{}).get(key,{})
        picked = [r for r in rows if key and key in r['boards']]
        ready = bool(key) and (pool or {}).get('constituent_versions',{}).get(key) != 'PENDING'
        if published and not published.get('evaluated'):
            ready = False
        partial = published and published.get('evaluated',0)<(published.get('member_count') or 0)
        status = ('STALE' if not ready_for_target else 'PARTIAL' if partial else
                  'READY' if picked else 'READY_EMPTY') if ready else (
            'CONSTITUENTS_PENDING' if current['member_count'] is None else 'WAIT_PREPARE')
        qualified = []
        for row in picked:
            zones = nearby_observation_zones(row['positions'],row['reference_close'])
            qualified.append(dict(code=row['code'],name=row.get('name'),reference_close=row['reference_close'],
                                  neckline=row.get('neckline'),wave_return=row.get('wave_return'),
                                  zones=[f'{lo:.4f}–{hi:.4f}' for _,lo,hi in zones]))
        boards_out.append(dict(current,board=board,pool_status=status,qualified=qualified,
                               evaluated_count=published.get('evaluated'),
                               qualified_count=len(picked) if ready and (picked or not partial) else None))
    return dict(ok=True, boards=boards_out, source=source,
                p_day=(pool or {}).get('data_date'),observation_day=(pool or {}).get('observation_date'),
                expected_observation_day=expected_day,
                status='READY' if ready_for_target else 'STALE' if pool else 'WAIT_PREPARE',
                total_rows=len({r['code'] for b in boards_out for r in b['qualified']}) if pool else None)
