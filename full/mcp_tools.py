"""Backend-neutral tools and durable jobs shared by MCP, DingTalk and the web app."""
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
from uuid import uuid4

from rules_lab.paths import ROOT, STATE, assert_write_allowed, canonical_sha, run_root, sha256_file

JOBS = {
    'select': '/api/plan/select', 'replay': '/api/run', 'backtest': '/api/backtest/run',
    'mine': '/api/research/mine', 'import_data': '/api/data/import',
    'fill_gaps': '/api/data/fill_gaps', 'push_test': '/api/push/test',
}
TERMINAL = frozenset({'SUCCEEDED','FAILED','INTERRUPTED'})
BUSY = '行情快照正在更新或使用'


@contextmanager
def ledger():
    path = assert_write_allowed(Path(os.environ.get('STARGAZER_AGENT_LEDGER') or STATE/'agent'/'jobs.sqlite3'))
    path.parent.mkdir(parents=True,exist_ok=True)
    db = sqlite3.connect(path,timeout=15)
    db.row_factory = sqlite3.Row
    try:
        db.executescript('''CREATE TABLE IF NOT EXISTS jobs(
            id TEXT PRIMARY KEY, dedup TEXT UNIQUE, actor TEXT, action TEXT, payload TEXT,
            state TEXT, created REAL, updated REAL, pid INTEGER, result TEXT);
            CREATE TABLE IF NOT EXISTS inbox(
            id TEXT PRIMARY KEY, actor TEXT, reply TEXT, command TEXT, expires REAL, consumed INTEGER DEFAULT 0);
        ''')
        with db:
            yield db
    finally:
        db.close()


@lru_cache(maxsize=1)
def _app():
    from full.webapp import create_app
    return create_app()


def call_api(path, body=None, query=None):
    with _app().test_client() as client:
        r = client.get(path,query_string=query) if body is None else client.post(path,json=body)
        doc = r.get_json(silent=True)
        return doc if isinstance(doc,dict) else {'ok':False,'error':f'HTTP_{r.status_code}'}


def status():
    from full.webapp import snapshot_manifest
    from full.strong_pool import ZONE, _day_pair
    from full.vipdoc_reader import calendar_days
    m = snapshot_manifest()
    try:
        p_day, day = _day_pair(datetime.now(ZONE),calendar_days())
    except (ValueError,OSError):
        p_day = day = None
    return dict(ok=True,snapshot=m['snapshot_fingerprint'],daily_through=m.get('daily_through'),
                selection_data_day=p_day,observation_day=day,replay_day=m.get('monitor_day'))


def plan(run_id=None):
    return call_api('/api/plan/latest',query={'run_id':run_id} if run_id else None)


def positions():
    return call_api('/api/positions')


def review(run_id=None):
    return call_api('/api/review',query={'run_id':run_id} if run_id else None)


def _bound_payload(action, args):
    from full.webapp import BASE_RULE, _run_directory, snapshot_manifest
    from rules_lab import rules
    if action == 'push_test':
        return {}
    m = snapshot_manifest()
    body = dict(args)
    binding = {'snapshot':m['snapshot_fingerprint']}
    body['_agent_binding'] = binding
    if action in ('select','replay','mine','import_data'):
        _app()  # Existing rule bootstrap, including freshly installed packages.
        rule = rules.get(body.get('rule') or BASE_RULE)
        body['rule'] = f"{rule['rule_id']}@{rule['version']}"
        binding['rule_sha'] = rule['sha256']
    if action == 'select':
        s = status()
        if not s['observation_day']:
            raise ValueError('TRADING_CALENDAR_UNAVAILABLE')
        binding.update(p_day=s['selection_data_day'],observation_day=s['observation_day'])
    if action == 'replay':
        body['days'] = body.get('days') or [int(m['monitor_day'])]
        body['with_tarot_shadow'] = bool(body.get('with_tarot_shadow',False))
    if action == 'mine' and not body.get('days'):
        from full.webapp import recent_trading_days
        body['days'] = recent_trading_days(5)
        if not body['days']:
            raise ValueError('NO_RESEARCH_DAYS')
    if action in ('replay','mine','fill_gaps'):
        days = body.get('days') or []
        if not days or len(days)>20:
            raise ValueError('INVALID_TRADING_DAYS')
        for day in days:
            if not re.fullmatch(r'20\d{6}',str(day)):
                raise ValueError('INVALID_TRADING_DAY')
            datetime.strptime(str(day),'%Y%m%d')
    if action == 'import_data':
        from full.vipdoc_reader import vipdoc_root
        source = body.pop('source','configured_tdx')
        if source not in ('configured_tdx','import_dir'):
            raise ValueError('INVALID_IMPORT_SOURCE')
        body['src'] = str((vipdoc_root() if source=='configured_tdx' else ROOT/'import_dir').resolve())
    if action == 'backtest':
        directory = _run_directory(run_root(),body.get('run_id'))
        if not directory:
            raise ValueError('NO_REPLAY_RUN')
        body['run_id'] = directory.name
        binding['run_sha'] = sha256_file(directory/'run_manifest.json')
    return body


def _view(row):
    if row is None:
        return dict(ok=False,error='TASK_NOT_FOUND')
    return dict(ok=True,task_id=row['id'],action=row['action'],status=row['state'],
                created_at=row['created'],updated_at=row['updated'],binding=json.loads(row['payload']).get('_agent_binding'),
                result=json.loads(row['result']) if row['result'] else None)


def _refresh(db, row):
    if row and row['state'] in ('ACCEPTED','RUNNING'):
        from rules_lab.state_store import _pid_alive
        if not row['pid'] or not _pid_alive(row['pid']):
            db.execute("UPDATE jobs SET state='INTERRUPTED',updated=?,result=? WHERE id=? AND state=?",
                       (time.time(),'{"ok":false,"error":"WORKER_EXITED"}',row['id'],row['state']))
            return db.execute('SELECT * FROM jobs WHERE id=?',(row['id'],)).fetchone()
    return row


def task_status(task_id, *, actor=None):
    with ledger() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?',(task_id,)).fetchone()
        if row is None or (actor is not None and row['actor']!=actor):
            return _view(None)
        return _view(_refresh(db,row))


def recent_tasks(limit=10):
    with ledger() as db:
        rows = db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?',(limit,)).fetchall()
        return [_view(_refresh(db,row)) for row in rows]


def _spawn(task_id):
    flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name=='nt' else 0
    return subprocess.Popen([sys.executable,'-B','-m','full.mcp_tools',task_id],cwd=ROOT,
                            stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                            creationflags=flags,start_new_session=os.name!='nt')


def submit(action, args=None, *, actor='local', request_id=None, prepared=None):
    if action not in JOBS:
        return dict(ok=False,error='UNKNOWN_JOB')
    try:
        body = _bound_payload(action,args or {}) if prepared is None else prepared
    except (OSError,ValueError,KeyError,RuntimeError) as exc:
        return dict(ok=False,error=str(exc)[:200])
    key = canonical_sha([actor,request_id] if request_id else [actor,action,body])
    with ledger() as db:
        db.execute('BEGIN IMMEDIATE')
        prior = db.execute('SELECT * FROM jobs WHERE dedup=?',(key,)).fetchone()
        if prior:
            prior = _refresh(db,prior)
            if prior['state'] not in ('FAILED','INTERRUPTED') or request_id:
                return dict(_view(prior),reused=True)
            db.execute('UPDATE jobs SET dedup=NULL WHERE id=?',(prior['id'],))
        rows = db.execute("SELECT * FROM jobs WHERE state IN ('ACCEPTED','RUNNING')").fetchall()
        if sum(_refresh(db,r)['state'] not in TERMINAL for r in rows)>=6:
            return dict(ok=False,error='AGENT_QUEUE_FULL')
        task_id = uuid4().hex
        now = time.time()
        db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?)',
                   (task_id,key,actor,action,json.dumps(body,ensure_ascii=False),'ACCEPTED',now,now,None,None))
        try:
            worker = _spawn(task_id)
            db.execute('UPDATE jobs SET pid=? WHERE id=?',(worker.pid,task_id))
        except OSError:
            db.execute("UPDATE jobs SET state='FAILED',result=? WHERE id=?",
                       ('{"ok":false,"error":"WORKER_START_FAILED"}',task_id))
        return dict(_view(db.execute('SELECT * FROM jobs WHERE id=?',(task_id,)).fetchone()),reused=False)


def worker(task_id):
    if not re.fullmatch('[a-f0-9]{32}',task_id):
        return
    with ledger() as db:
        changed = db.execute("UPDATE jobs SET state='RUNNING',updated=?,pid=? WHERE id=? AND state='ACCEPTED'",
                             (time.time(),os.getpid(),task_id)).rowcount
        if not changed:
            return
        row = db.execute('SELECT * FROM jobs WHERE id=?',(task_id,)).fetchone()
    try:
        deadline = time.monotonic()+1800
        while True:
            result = call_api(JOBS[row['action']],json.loads(row['payload']))
            if result.get('error')!=BUSY or time.monotonic()>=deadline:
                break
            time.sleep(1)
        if row['action']=='push_test':
            result = {'ok':bool(result.get('ok')),'channel':result.get('channel'),'error':result.get('reason')}
        ok = result.get('ok',not result.get('error')) is True
        encoded = json.dumps(result,ensure_ascii=False,allow_nan=False)
    except (Exception,SystemExit) as exc:
        ok = False
        encoded = json.dumps({'ok':False,'error':type(exc).__name__})
    with ledger() as db:
        db.execute('UPDATE jobs SET state=?,result=?,updated=? WHERE id=? AND state=\'RUNNING\'',
                   ('SUCCEEDED' if ok else 'FAILED',encoded,time.time(),task_id))


if __name__ == '__main__':
    worker(sys.argv[1])
