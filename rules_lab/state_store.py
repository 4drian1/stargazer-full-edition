"""实验运行态：实例锁、任务进度、事件持久化、模拟收件箱、可续跑。

所有状态都在 lab_data/state/runs/<run_id>/ 下，run_id 由输入参数确定性推导，
因此同一组参数重放得到同一份字节；生产 dist/data 永不作为写入目标。
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .paths import (STATE, assert_write_allowed, atomic_write_json,
                    canonical_bytes, read_json, run_root)

STAGES = ('PREP', 'SELECTION', 'FREEZE', 'MONITOR', 'INDICATOR', 'REVIEW', 'REPORT')


def make_run_id(**params):
    return hashlib.sha256(canonical_bytes(params)).hexdigest()[:16]


class InstanceLock:
    """独立实例锁：实验版自己的锁，不读写正式版锁文件。"""

    def __init__(self, name='rules_lab.lock'):
        self.path = STATE / 'locks' / name

    @contextmanager
    def hold(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(20):
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, ('%d' % os.getpid()).encode('ascii'))
                os.close(fd)
                break
            except FileExistsError:
                try:
                    pid = int(self.path.read_text(encoding='utf-8').strip())
                except (ValueError, OSError):
                    pid = -1
                if pid > 0 and _pid_alive(pid):
                    time.sleep(0.05)
                    continue
                self.path.unlink(missing_ok=True)
                continue
        else:
            raise RuntimeError('INSTANCE_LOCK_BUSY:' + str(self.path))
        try:
            yield
        finally:
            self.path.unlink(missing_ok=True)


def _pid_alive(pid):
    if os.name != 'nt':
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes
    handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    ctypes.windll.kernel32.CloseHandle(handle)
    return True


class RunContext:
    def __init__(self, run_id, *, meta=None, root_base=None):
        self.run_id = run_id
        base = Path(root_base) if root_base is not None else run_root()
        self.root = base / run_id
        for sub in ('tasks', 'freeze', 'events', 'inbox', 'out', 'cache', 'locks'):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self.meta = meta or {}
        self.manifest_path = self.root / 'run_manifest.json'
        self._outputs = {}

    # ---------------------------------------------------------- 路径
    def path(self, *parts):
        return self.root.joinpath(*parts)

    def write(self, *parts, value):
        p = self.path(*parts)
        atomic_write_json(p, value)
        self._outputs[str(p.relative_to(self.root)).replace('\\', '/')] = \
            hashlib.sha256(canonical_bytes(value)).hexdigest()
        return p

    def read(self, *parts, default=None):
        p = self.path(*parts)
        return read_json(p) if p.is_file() else default

    def exists(self, *parts):
        return self.path(*parts).is_file()

    # ---------------------------------------------------------- 重置 / 续跑
    def reset(self):
        """清空本 run 的全部产物（仅实验目录内），用于可重复的完整回放。"""
        for p in sorted(self.root.rglob('*'),
                        key=lambda x: len(x.parts), reverse=True):
            if p.is_file():
                p.unlink()
            elif p.is_dir():
                try:
                    p.rmdir()
                except OSError:
                    pass
        for sub in ('tasks', 'freeze', 'events', 'inbox', 'out', 'cache', 'locks'):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        self._outputs = {}
        return self

    # ---------------------------------------------------------- 任务进度
    def journal(self, day, stage=None, detail=None):
        p = self.path('tasks', '%s.json' % day)
        value = read_json(p) if p.is_file() else dict(
            contract='RULES_LAB_TASK_V1', day=int(day), completed=[], events=[])
        if stage:
            if stage not in value['completed']:
                value['completed'].append(stage)
            value['events'].append(dict(stage=stage, detail=detail))
        atomic_write_json(p, value)
        self._outputs[str(p.relative_to(self.root)).replace('\\', '/')] = \
            hashlib.sha256(canonical_bytes(value)).hexdigest()
        return value

    def resume_point(self, day):
        done = set((self.read('tasks', '%s.json' % day, default={}) or {})
                   .get('completed', []))
        for stage in STAGES:
            if stage not in done:
                return stage
        return None

    # ---------------------------------------------------------- 输出清单
    def finish(self, extra=None):
        """扫描 run 目录全部输出（含 JSONL 回执/样本），保证清单完整可复算。"""
        files = {}
        for p in sorted(self.root.rglob('*')):
            if not p.is_file() or p.name == 'run_manifest.json' or p.name.endswith('.tmp'):
                continue
            rel = str(p.relative_to(self.root)).replace('\\', '/')
            files[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
        payload = dict(contract='RULES_LAB_RUN_MANIFEST_V1', run_id=self.run_id,
                       meta=self.meta, outputs=self._outputs, files=files,
                       extra=extra or {})
        atomic_write_json(self.manifest_path, payload)
        return payload


class EventStore:
    """事件状态机：SENT / UNKNOWN / EXPIRED 为终态，重启不重发。"""

    TERMINAL = ('SENT', 'UNKNOWN', 'EXPIRED')

    def __init__(self, run: RunContext, day):
        self.run = run
        self.day = str(day)

    def _read(self):
        return self.run.read('events', '%s.json' % self.day,
                             default=dict(contract='RULES_LAB_EVENTS_V1',
                                          day=int(self.day), events={}))

    def _write(self, value):
        return self.run.write('events', '%s.json' % self.day, value=value)

    def events(self):
        return self._read()['events']

    def get(self, event_id):
        return self.events().get(event_id)

    def put(self, event):
        value = self._read()
        value['events'][event['event_id']] = event
        self._write(value)
        return event

    def recover(self):
        """v1.1：SENDING/PENDING 不再提前判 UNKNOWN，交给回执账本对账。

        旧版把 SENDING 一律标 UNKNOWN 且不重发，若进程恰在「事件落盘后、
        回执写入前」中断，首条触位会永远停在 PENDING/UNKNOWN。本地模拟收件箱
        的回执是权威账本，故此处只解除排队标记、保留非终态，由
        monitor.reconcile_touch_delivery 以 (event_id, kind) 去重键补齐。
        """
        value = self._read()
        recovered = []
        for eid, event in value['events'].items():
            status = event.get('delivery_status')
            if status in ('SENDING', 'PENDING'):
                event['delivery_queued'] = False
                event['recovery_note'] = 'RECONCILE_AGAINST_RECEIPT_LEDGER'
                recovered.append(dict(event_id=eid, action='RECONCILE_REQUIRED'))
            elif status in self.TERMINAL:
                event['delivery_queued'] = False
                recovered.append(dict(event_id=eid, action='TERMINAL_KEEP'))
        self._write(value)
        return dict(contract='RULES_LAB_RECOVERY_V1_1', recovered=recovered,
                    terminal=sum(1 for e in value['events'].values()
                                 if e.get('delivery_status') in self.TERMINAL),
                    pending=sum(1 for e in value['events'].values()
                                if e.get('delivery_status') in ('PENDING', 'SENDING')))


class LocalInbox:
    """模拟收件箱：本地 JSONL 追加式回执，去重键 event_id+kind。"""

    def __init__(self, run: RunContext, *, fail_on=None):
        self.run = run
        self.path = run.path('inbox', 'receipts.jsonl')
        self.address = str(self.path)
        self.fail_on = fail_on or set()

    def _lines(self):
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in
                self.path.read_text(encoding='utf-8').splitlines() if line.strip()]

    def keys(self):
        return {(r['event_id'], r['kind']) for r in self._lines()}

    def send(self, record):
        assert_write_allowed(self.path)
        key = (record['event_id'], record['kind'])
        if key in self.keys():
            return dict(delivery='DUPLICATE_SKIPPED', event_id=record['event_id'],
                        kind=record['kind'])
        if record['event_id'] in self.fail_on:
            return dict(delivery='UNKNOWN', event_id=record['event_id'],
                        kind=record['kind'], note='SIMULATED_SEND_FAILURE')
        line = json.dumps(record, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':')) + '\n'
        mode = 'ab' if self.path.is_file() else 'wb'
        with open(self.path, mode) as fh:
            fh.write(line.encode('utf-8'))
            fh.flush()
            os.fsync(fh.fileno())
        return dict(delivery='SENT', event_id=record['event_id'], kind=record['kind'])

    def records(self):
        return self._lines()

    def counts(self):
        out = {}
        for r in self._lines():
            out[r['delivery']] = out.get(r['delivery'], 0) + 1
        return out
