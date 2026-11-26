"""真实行情 → rules_lab 快照格式（一次性构建，之后只读）。

覆盖：全市场股票日线（P 日起回退 history_days 个交易日）、监控日全 universe
的 5 分钟线、交易日历、names/base.dbf 派生字段（is_st/float_mv）。
价格口径 RAW（不复权），qfq_factor 置 1 并在 manifest 标注。
缺数样本：--holes 按名单丢分钟/丢字段，用于验收「缺线显示原因、恢复后重判」。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from . import vipdoc_reader as V

DAILY_COLUMNS = ['date', 'open', 'high', 'low', 'close', 'volume', 'amount',
                 'pre_close', 'pct_chg', 'is_st', 'float_mv', 'qfq_factor']
MINUTE_COLUMNS = ['ts', 'open', 'high', 'low', 'close', 'volume', 'amount']


def _gz_write(path: Path, doc: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(gzip.compress(json.dumps(doc, ensure_ascii=False,
                                              separators=(',', ':')).encode('utf-8'), mtime=0))


def build(p_day: int, monitor_day: int, history_days: int, out_dir: Path,
          minute_codes: set[str] | None = None, holes: dict | None = None,
          minute_history_days: int = 10):
    holes = holes or {}
    cal = V.calendar_days()
    idx = {d: i for i, d in enumerate(cal)}
    if p_day not in idx or monitor_day not in idx:
        raise SystemExit('P/monitor day not in calendar')
    end_i = idx[p_day]
    win_end = idx[monitor_day] if monitor_day in idx and monitor_day > p_day else end_i
    win = [d for d in cal[max(0, win_end - history_days + 1):win_end + 1]]

    names = V.stock_names()
    stems = V.stock_stems()
    print('universe stems:', len(stems), flush=True)

    daily_codes: dict[str, list] = {}
    n_skip = 0
    for i, stem in enumerate(stems):
        rows = V.daily_rows(stem, len(win), monitor_day)
        rows = [r for r in rows if r['date'] in set(win)]
        if len(rows) < 30:
            n_skip += 1
            continue
        fs = V.float_shares(stem)
        name = names.get(stem[2:], '') or ''
        is_st = 1 if ('ST' in name.upper()) else 0
        packed = []
        prev_close = None
        for r in rows:
            pc = prev_close if prev_close else r['close']
            pct = (r['close'] / pc - 1.0) if pc else None
            packed.append([r['date'], round(r['open'], 3), round(r['high'], 3),
                           round(r['low'], 3), round(r['close'], 3),
                           int(r['volume']), round(r['amount'], 1),
                           round(pc, 3) if pc else None,
                           round(pct, 5) if pct is not None else None,
                           is_st,
                           round(r['close'] * fs, 1) if fs else None,
                           1.0])
            prev_close = r['close']
        daily_codes[stem[2:]] = packed
        if i % 800 == 0:
            print('daily %d/%d codes=%d' % (i, len(stems), len(daily_codes)), flush=True)
    print('daily done, codes=%d skipped=%d' % (len(daily_codes), n_skip), flush=True)

    scope = minute_codes if minute_codes is not None else set(daily_codes)
    minutes_days = {str(monitor_day): {}}
    cal_all = [d for d in cal if d <= monitor_day]
    for hd in cal_all[max(0, len(cal_all) - 1 - minute_history_days):len(cal_all) - 1]:
        minutes_days[str(hd)] = {}
    miss = []
    for i, code in enumerate(sorted(scope)):
        stem = ('sh' if code.startswith('6') else 'sz' if code[0] in '023' else 'bj') + code
        if code in holes.get('drop_minute_codes', set()):
            miss.append(code)
            continue
        for dkey, dval in minutes_days.items():
            bars = V.minute_bars(stem, int(dkey))
            if not bars:
                if dkey == str(monitor_day):
                    miss.append(code)
                continue
            drop_fields = holes.get('drop_fields', {}).get(code, [])
            dval[code] = [
                [b['ts'], b['open'], b['high'], b['low'], b['close'],
                 None if 'volume' in drop_fields else b['volume'],
                 None if 'amount' in drop_fields else b['amount']] for b in bars]
        if i % 800 == 0:
            print('minutes %d/%d' % (i, len(scope)), flush=True)
    print('minutes done, codes=%d missing=%d' % (len(minutes_days[str(monitor_day)]), len(miss)), flush=True)

    _gz_write(out_dir / 'daily.json.gz', dict(columns=DAILY_COLUMNS, codes=daily_codes,
                                              price_basis='RAW_TDX_VIPDOC',
                                              qfq_basis='NONE_FACTOR_1'))
    _gz_write(out_dir / 'minutes.json.gz', dict(columns=MINUTE_COLUMNS, days=minutes_days))
    _gz_write(out_dir / 'corporate_events.json.gz', dict(events={},
              note='round1 not populated; strong_pullback does not use corporate events'))
    (out_dir / 'calendar.json').write_text(json.dumps(
        dict(contract='RULES_LAB_CALENDAR_V1',
             count=len([d for d in cal if d <= monitor_day]),
             first=str(cal[0]), last=str(monitor_day),
             days=[str(d) for d in cal if d <= monitor_day],
             source='DERIVED_FROM_TDX_DAILY_SOURCE_V2_CALENDAR',
             cross_check=dict(external_count=len(cal), external_first=str(cal[0]),
                              external_last=str(cal[-1]),
                              note='calendar from tdx_daily_source_v2 (akshare file_fold); '
                                   'snapshot truncated at monitor_day'))), encoding='utf-8')
    missing_doc = dict(monitor_day=monitor_day, missing_minute_codes=sorted(miss),
                       holes={k: (sorted(v) if isinstance(v, (set, list)) else v)
                              for k, v in holes.items()})
    (out_dir / 'missing_minutes.json').write_text(json.dumps(missing_doc, ensure_ascii=False, indent=1),
                                                  encoding='utf-8')
    manifest = dict(contract='RULES_LAB_SNAPSHOT_MANIFEST_V1',
                    source='market_data vipdoc (read-only build)',
                    p_day=p_day, monitor_day=monitor_day, history_days=history_days,
                    minute_history_days=minute_history_days,
                    minute_days=len(minutes_days),
                    universe=len(daily_codes), minute_codes=len(minutes_days[str(monitor_day)]),
                    price_basis='RAW_TDX_VIPDOC', qfq_basis='NONE_FACTOR_1')
    body = json.dumps(manifest, sort_keys=True, ensure_ascii=False).encode('utf-8')
    manifest['snapshot_fingerprint'] = hashlib.sha256(body).hexdigest()
    (out_dir / 'snapshot_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                                    encoding='utf-8')
    print('fingerprint', manifest['snapshot_fingerprint'][:16])
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--p-day', type=int, required=True)
    ap.add_argument('--monitor-day', type=int, required=True)
    ap.add_argument('--history-days', type=int, default=90)
    ap.add_argument('--minute-history-days', type=int, default=10,
                    help='监控日之前保留的分钟线交易日数（跨日预热/同刻量比用）')
    ap.add_argument('--out', required=True)
    ap.add_argument('--holes-json', default=None, help='缺数样本名单 json')
    a = ap.parse_args()
    holes = json.loads(Path(a.holes_json).read_text(encoding='utf-8')) if a.holes_json else {}
    for k in ('drop_minute_codes',):
        if k in holes:
            holes[k] = set(holes[k])
    build(a.p_day, a.monitor_day, a.history_days, Path(a.out), holes=holes,
          minute_history_days=a.minute_history_days)


if __name__ == '__main__':
    main()
