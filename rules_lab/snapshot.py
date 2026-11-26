"""实验快照的只读加载器（纯标准库，不接触生产路径）。

Snapshot 支持 ``cutoff``：只保留 date/ts <= cutoff 的数据，用于验证
「替换截止之后的数据，之前的信号保持一致」。
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from .paths import SNAPSHOT, read_json


def _load_gz(name):
    p = SNAPSHOT / name
    if not p.is_file():
        raise RuntimeError('SNAPSHOT_MISSING:' + str(p))
    return json.loads(gzip.decompress(p.read_bytes()).decode('utf-8'))


class Snapshot:
    def __init__(self, *, cutoff=None):
        self.manifest = read_json(SNAPSHOT / 'snapshot_manifest.json')
        if self.manifest.get('contract') != 'RULES_LAB_SNAPSHOT_MANIFEST_V1':
            raise RuntimeError('SNAPSHOT_CONTRACT_MISMATCH')
        self.fingerprint = self.manifest['snapshot_fingerprint']
        self.cutoff = int(cutoff) if cutoff else None

        d = _load_gz('daily.json.gz')
        m = _load_gz('minutes.json.gz')
        ce = _load_gz('corporate_events.json.gz')
        self.daily_columns = d['columns']
        self.minute_columns = m['columns']
        self.price_basis = d.get('price_basis')
        self.qfq_basis = d.get('qfq_basis')
        self.daily = {}
        self._daily_index = {}
        for code, rows in d['codes'].items():
            kept = [dict(zip(self.daily_columns, r))
                    for r in rows if self.cutoff is None or r[0] <= self.cutoff]
            if kept:
                self.daily[code] = kept
                self._daily_index[code] = {r['date']: r for r in kept}
        self.minutes = {}
        for day, codes in m['days'].items():
            if self.cutoff is not None and int(day) > self.cutoff:
                continue
            self.minutes[day] = {
                code: [dict(zip(self.minute_columns, b)) for b in bars]
                for code, bars in codes.items()
            }
        self.corporate_events = {
            code: [tuple(x) for x in rows if self.cutoff is None or x[0] <= self.cutoff]
            for code, rows in ce.get('events', {}).items()
        }
        cal = read_json(SNAPSHOT / 'calendar.json')
        self.calendar = [int(x) for x in cal['days']
                         if self.cutoff is None or int(x) <= self.cutoff]
        self.calendar_source = cal['source']
        self.calendar_cross_check = cal['cross_check']
        self.boards = read_json(SNAPSHOT / 'boards.json')
        self.reference_pool = read_json(SNAPSHOT / 'reference_pool.json')
        self.limitations = self.manifest.get('limitations', [])
        self.coverage = self.manifest.get('coverage', {})
        self._file = str(SNAPSHOT)

    # ------------------------------------------------------------ 日历
    def prev_trading_day(self, day):
        day = int(day)
        earlier = [d for d in self.calendar if d < day]
        if not earlier:
            raise RuntimeError('NO_PREVIOUS_TRADING_DAY:' + str(day))
        return earlier[-1]

    def next_trading_day(self, day):
        day = int(day)
        later = [d for d in self.calendar if d > day]
        if not later:
            raise RuntimeError('NO_NEXT_TRADING_DAY:' + str(day))
        return later[0]

    def forward_trading_days(self, day, count):
        day = int(day)
        later = [d for d in self.calendar if d > day]
        if len(later) < count:
            return None
        return later[:count]

    def has_day(self, day):
        return int(day) in self.calendar

    # ------------------------------------------------------------ 数据切片
    def codes(self):
        return sorted(self.daily)

    def daily_asof(self, code, asof):
        return [r for r in self.daily.get(code, []) if r['date'] <= int(asof)]

    def daily_row(self, code, day):
        return self._daily_index.get(code, {}).get(int(day))

    def minute_bars(self, day, code):
        return self.minutes.get(str(day), {}).get(code)

    def minute_day_codes(self, day):
        return sorted(self.minutes.get(str(day), {}))

    def minute_days(self):
        return sorted(self.minutes)

    def corporates(self, code, start, end):
        return [x for x in self.corporate_events.get(code, [])
                if int(start) <= x[0] <= int(end)]

    def snapshot_ref(self):
        return dict(fingerprint=self.fingerprint, cutoff=self.cutoff,
                    daily_last=self.calendar[-1], calendar_source=self.calendar_source,
                    price_basis=self.price_basis,
                    minute_day_count=len(self.minutes))


def open_snapshot(cutoff=None) -> Snapshot:
    return Snapshot(cutoff=cutoff)
