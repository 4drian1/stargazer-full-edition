"""Live board breadth and deduplicated first-touch delivery for the full edition."""
from datetime import datetime
from functools import lru_cache
import math
from threading import Lock, Thread
from time import monotonic, sleep

from rules_lab.paths import atomic_write_json
from full import strong_pool as pool
from full.push_config import load as push_config, send_summary

LOCK = Lock()
_WORKER = None
INTERVAL = 20
BATCH = 120
SEND_INTERVAL = 3.2


def _session(now):
    clock = now.hour*100 + now.minute
    return 930 <= clock < 1130 or 1300 <= clock < 1500


def _file(now):
    return pool.STORE/'board_monitor'/f'{now:%Y%m%d}.json'


@lru_cache(maxsize=1)
def _quote_reader():
    # Load the same public quote parser used by the desktop, with no production runtime imports.
    import importlib.util
    from rules_lab.paths import ROOT
    source = ROOT.parents[1]/'stargazer_desktop/tools/market.py'
    spec = importlib.util.spec_from_file_location('full_public_quotes', source)
    reader = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reader)
    return reader.quotes


def _quotes(codes):
    return _quote_reader()(codes, timeout=2, batch_size=30, max_workers=4, tries=1)


def _quality(quote, now):
    try:
        stamp = datetime.strptime(str(quote['time']), '%Y%m%d%H%M%S').replace(tzinfo=pool.ZONE)
        return (stamp.date()==now.date() and 0 <= (now-stamp).total_seconds() <= 120 and
                all(math.isfinite(float(quote[k])) and float(quote[k]) > 0 for k in ('price','prev')))
    except (KeyError,ValueError,TypeError):
        return False


def _rolling(codes, cursor):
    if len(codes) <= BATCH:
        return codes
    start = cursor % len(codes)
    return (codes[start:]+codes[:start])[:BATCH]


def _breadth(codes, quotes):
    changes = [float(quotes[c]['price'])/float(quotes[c]['prev'])-1 for c in codes if c in quotes]
    return dict(covered=len(changes), total=len(codes), up=sum(x>0 for x in changes),
                down=sum(x<0 for x in changes), mean=sum(changes)/len(changes) if changes else None)


def state(now=None):
    now = now or datetime.now(pool.ZONE)
    cfg = pool.watch_config()
    focus_boards = pool.strong_watch()
    doc = pool._read(_file(now))
    current = doc.get('scope') == cfg['boards'] and doc.get('strong_scope') == focus_boards
    try:
        recent = 0 <= (now-datetime.fromisoformat(doc['at'])).total_seconds() <= 120
    except (KeyError, TypeError, ValueError):
        recent = False
    status = ('OFF' if not cfg['enabled'] else 'OUT_OF_SESSION' if not _session(now) else
              doc.get('status','WAIT_QUOTES') if current and recent else 'WAIT_QUOTES')
    return dict(ok=True, enabled=cfg['enabled'], boards=cfg['boards'], strong_board_count=len(focus_boards),
                push_channel=push_config()['channel'], status=status,
                at=doc.get('at') if current else None,
                rows=doc.get('display',[]) if current and recent and cfg['enabled'] and _session(now) else [],
                alerts=sorted(doc.get('alerts',{}).values(),key=lambda a:a['at'],reverse=True)[:40],
                pool_state=doc.get('pool_state') if current else None,
                error=doc.get('error') if current and recent else None)


def tick(now=None, quote_fn=None):
    at = now or datetime.now(pool.ZONE)
    cfg = pool.watch_config()
    if not cfg['enabled'] or not _session(at) or not LOCK.acquire(blocking=False):
        return state(at)
    try:
        from full.vipdoc_reader import calendar_days
        today = int(at.strftime('%Y%m%d'))
        calendar = calendar_days()
        focus_boards = pool.strong_watch()
        prior = pool._read(_file(at))
        if today not in calendar:
            atomic_write_json(_file(at),dict(prior,scope=cfg['boards'],strong_scope=focus_boards,at=at.isoformat(),display=[],
                                           status='CALENDAR_PENDING' if not calendar or today>calendar[-1] else 'OUT_OF_SESSION'))
            return state(at)
        watched = list(dict.fromkeys(cfg['boards']+focus_boards))
        members, info = pool._memberships(watched)
        frozen, source = pool.current_pool(at,for_monitor=True)
        same_day = frozen and int(frozen['observation_date']) == today
        strong = {board: {r['code'] for r in frozen['rows'] if any(
                         b in (board,info[board]['identity_name']) or
                         pool.ALIASES.get(b.casefold(),b) in (board,info[board]['identity_name']) for b in r['boards'])}
                  if same_day else set() for board in watched}
        focused = sorted({c for board in focus_boards for c in strong[board]})
        universe = sorted({c for board in cfg['boards'] for c in members[board]})
        cursor = int(prior.get('cursor',0))
        requested = list(dict.fromkeys(_rolling(focused,cursor)+_rolling(universe,cursor)))
        status, error = 'WAIT_QUOTES', None
        try:
            live = (quote_fn or _quotes)(requested) if requested else {}
        except (ValueError, OSError, TypeError) as exc:
            live, error = {}, type(exc).__name__+': '+str(exc)[:150]
        if not isinstance(live,dict):
            live, error = {}, '报价格式无效'
        checked = now or datetime.now(pool.ZONE)
        if pool.watch_config() != cfg or pool.strong_watch() != focus_boards:
            return state(checked)
        # Verify the data time after retrieval and retain only fresh samples across rounds.
        allowed = set(focused+universe)
        quotes = {c:q for c,q in prior.get('quotes',{}).items() if c in allowed and _quality(q,checked)}
        valid_live = {}
        for c,q in live.items():
            if c not in requested or not _quality(q,checked) or str(q['time'])<quotes.get(c,{}).get('time',''):
                continue
            valid_live[c] = dict(time=str(q['time']),price=float(q['price']),prev=float(q['prev']),name=q.get('name'))
            if 'low' in q:
                low = q['low']
                valid_live[c]['low'] = low if type(low) in (float,int) and math.isfinite(low) else None
        quotes.update(valid_live)
        if quotes:
            status = 'LIVE'
        elif not requested:
            status = 'CONSTITUENTS_PENDING'
        elif not error:
            error = '报价源未返回有效记录'
        display = [dict(info[b],board=b,constituents=_breadth(members[b],quotes),
                        strong_pool=_breadth(strong[b],quotes) if same_day else None) for b in cfg['boards']]
        events = prior.get('alerts',{})
        if same_day:
            from full.strong_touch import make_event
            seen = {e['code'] for e in events.values()}
            for r in frozen['rows']:
                code = r['code']
                q = valid_live.get(code)
                if code not in focused or not q or code in seen:
                    continue
                active_at = frozen.get('enabled_at') or frozen.get('built_at')
                if active_at and q['time'] < str(active_at):
                    continue
                event = make_event(r,q,checked,[b for b in focus_boards if code in strong[b]],source)
                if event:
                    events[code] = event
                    seen.add(code)
        doc = dict(status=status,scope=cfg['boards'],strong_scope=focus_boards,at=checked.isoformat(),quotes=quotes,display=display,alerts=events,
                   pool_state='CURRENT' if same_day else 'WAIT_SAME_DAY_POOL',cursor=cursor+BATCH,error=error)
        atomic_write_json(_file(at),doc)
        return state(checked)
    finally:
        LOCK.release()


def _update(at,key,**changes):
    with LOCK:
        file = _file(at)
        doc = pool._read(file)
        event = doc.get('alerts',{}).get(key)
        if event is not None:
            event.update(changes)
            atomic_write_json(file,doc)


def deliver_once(now=None):
    from full.strong_touch import first_card,supplement_card
    at = now or datetime.now(pool.ZONE)
    if not pool.watch_config()['enabled']:
        return False
    cfg = push_config()
    send = ((cfg['channel']=='dingtalk' and cfg.get('dingtalk_webhook')) or
            (cfg['channel']=='pushplus' and cfg.get('pushplus_token')))
    with LOCK:
        file = _file(at)
        doc = pool._read(file)
        events = sorted(doc.get('alerts',{}).items(),key=lambda item:item[1]['at'])
        first = next(((key,event) for key,event in events if event['delivery']=='PENDING'),None)
        supplement = next(((key,event) for key,event in events if event['delivery']=='SENT' and
                           event.get('indicator_status')=='AVAILABLE' and not event.get('supplement')),None)
        selected = first or supplement
        if selected is None or (not send and not first):
            return False
        key,event = selected
        field = 'delivery' if first else 'supplement'
        if not send:
            event[field] = 'LOCAL_ONLY'
            atomic_write_json(file,doc)
            return False
        card = first_card(event) if first else supplement_card(event)
        event[field] = 'SENDING'
        if card.get('includes_indicators'):
            event['supplement'] = 'INCLUDED_IN_FIRST'
        atomic_write_json(file,doc)
    try:
        result = send_summary(card['title'],card['body'],cfg=cfg,include_heading=False)
        delivery = 'SENT' if result.get('ok') else 'UNKNOWN' if result.get('unknown') else 'FAILED'
    except Exception:
        delivery = 'UNKNOWN'
    _update(at,key,**{field:delivery})
    return True


def indicator_once(now=None):
    from full.strong_touch import compute_indicators
    at = now or datetime.now(pool.ZONE)
    if not _session(at) or not pool.watch_config()['enabled']:
        return False
    with LOCK:
        file = _file(at)
        doc = pool._read(file)
        pending = sorted(doc.get('alerts',{}).items(),key=lambda item:item[1]['at'])
        selected = next(((k,e) for k,e in pending if e.get('indicator_status')=='PENDING' and
                         e['delivery'] in ('PENDING','SENDING','SENT','LOCAL_ONLY') and
                         e.get('retry_after',0)<=at.timestamp()),None)
        if not selected:
            return False
        key,event = selected
        event['indicator_status'] = 'COMPUTING'
        event['attempts'] += 1
        atomic_write_json(file,doc)
    try:
        result = compute_indicators(event)
    except (OSError,ValueError,KeyError,TypeError,ImportError) as exc:
        exhausted = event['attempts']>=3
        _update(at,key,indicator_status='UNAVAILABLE' if exhausted else 'PENDING',
                indicator_reason=str(exc)[:100],retry_after=at.timestamp()+45)
    else:
        _update(at,key,indicator_status='AVAILABLE',indicator=result)
    return True


def _recover(at):
    with LOCK:
        file = _file(at)
        doc = pool._read(file)
        changed = False
        for event in doc.get('alerts',{}).values():
            for field in ('delivery','supplement'):
                if event.get(field)=='SENDING':
                    event[field] = 'UNKNOWN'
                    changed = True
            if event.get('indicator_status')=='COMPUTING':
                event['indicator_status'] = 'PENDING' if event['attempts']<3 else 'UNAVAILABLE'
                changed = True
        if changed:
            atomic_write_json(file,doc)


def start():
    global _WORKER
    if _WORKER and _WORKER.is_alive():
        return
    import logging

    def follow():
        while True:
            try:
                tick()
            except (OSError, ValueError, KeyError, TypeError, ImportError):
                logging.getLogger(__name__).exception('Board monitor pass failed')
            sleep(INTERVAL)

    def consume(task, delay):
        while True:
            started = monotonic()
            try:
                worked = task()
            except (OSError, ValueError, KeyError, TypeError, ImportError):
                logging.getLogger(__name__).exception('Board touch task failed')
                worked = False
            sleep(max(.2,delay-(monotonic()-started)) if worked else 1)

    _recover(datetime.now(pool.ZONE))
    Thread(target=consume,args=(deliver_once,SEND_INTERVAL),name='full-board-delivery',daemon=True).start()
    Thread(target=consume,args=(indicator_once,0.5),name='full-board-indicators',daemon=True).start()
    _WORKER = Thread(target=follow,name='full-board-monitor',daemon=True)
    _WORKER.start()
