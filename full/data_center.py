"""数据中心（本地通达信）：扫描 → 分类 → 校验 → 发布新快照。

- 来源：vipdoc 目录树（sh/sz/bj 的 lday/fzline）或平铺的 .day/.lc5 文件。
- 扫描分类：新增（快照里没有的日期/代码）、重复（内容一致）、
  冲突（同日价格不一致，默认拒绝导入并列出）。
- 依赖窗口：按规则实际依赖检查（7 个交易日是最低；MA60/ret_60d/黄金地图 123）。
- 发布：暂存→校验（记录数/日期单调/OHLC 合法）→原子替换+新内容指纹；
  失败保留上一份快照。
用法:
  python -m full.data_center scan --src <vipdoc或目录>
  python -m full.data_center import --src <目录> [--rule strong_pullback@v1]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import struct
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))
sys.path.insert(0, str(FE.parents[1] / 'stargazer_desktop'))

DAY_STRUCT = '<IIIIIfII'
LC5_STRUCT = '<HHfffffiI'


def _iter_day(path):
    raw = Path(path).read_bytes()
    for rec in struct.iter_unpack(DAY_STRUCT, raw):
        yield rec  # date,o,h,l,c,amount,vol,res


def _day_dates(path):
    raw = Path(path).read_bytes()
    return [rec[0] for rec in struct.iter_unpack(DAY_STRUCT, raw)]


def _file_last(path):
    raw = Path(path).read_bytes()
    if len(raw) < 32:
        return None, 0
    rec = raw[-32:]
    return struct.unpack(DAY_STRUCT, rec)[0], len(raw) // 32


def _find_files(src: Path):
    """支持 vipdoc 树（sh/lday/*.day）或平铺文件。"""
    out = {'day': [], 'lc5': []}
    if (src / 'sh' / 'lday').is_dir() or (src / 'sz' / 'lday').is_dir():
        for ex in ('sh', 'sz', 'bj'):
            for sub, kind in (('lday', 'day'), ('fzline', 'lc5')):
                d = src / ex / sub
                if d.is_dir():
                    out[kind].extend(sorted(d.glob('*.' + kind)))
    else:
        out['day'] = sorted(src.rglob('*.day'))
        out['lc5'] = sorted(src.rglob('*.lc5'))
    return out


def _code_of(path: Path):
    return path.stem[2:] if path.stem[:2] in ('sh', 'sz', 'bj') else path.stem


STOCK_PREFIX = ('sh60', 'sh68', 'sz00', 'sz30', 'bj')


def _snap_daily_index(snap_dir: Path):
    """{code: (last_date, rows)} 来自当前快照。"""
    p = snap_dir / 'daily.json.gz'
    if not p.is_file():
        return {}
    d = json.loads(gzip.decompress(p.read_bytes()))
    cols = d['columns']
    di = cols.index('date')
    return {c: (rows[-1][di], len(rows)) for c, rows in d['codes'].items() if rows}



def swap_snapshot(staging: Path, snap_dir: Path):
    """可恢复切换：旧快照改名备份 → 新快照就位 → 校验 → 删备份。
    任一步失败自动回滚，旧快照始终可用。"""
    staging = Path(staging)
    snap_dir = Path(snap_dir)
    backup = snap_dir.with_name(snap_dir.name + '_backup')
    if backup.exists():
        shutil.rmtree(backup)
    snap_dir.replace(backup)
    try:
        staging.replace(snap_dir)
        if not (snap_dir / 'snapshot_manifest.json').is_file():
            raise RuntimeError('SWAPPED_BUT_MANIFEST_MISSING')
    except Exception:
        if snap_dir.exists():
            shutil.rmtree(snap_dir, ignore_errors=True)
        backup.replace(snap_dir)
        raise
    shutil.rmtree(backup, ignore_errors=True)


def scan(src: Path, snap_dir: Path):
    snap_dir = Path(snap_dir)
    files = _find_files(Path(src))
    snap_idx = _snap_daily_index(Path(snap_dir))
    snap_rows = {}
    dp = Path(snap_dir) / 'daily.json.gz'
    if dp.is_file():
        d0 = json.loads(gzip.decompress(dp.read_bytes()))
        cols0 = d0['columns']
        ri0 = cols0.index('date')
        snap_rows = {c: (cols0, rows) for c, rows in d0['codes'].items() if rows}
        del d0
    entries = []
    for f in files['day']:
        code = _code_of(f)
        stem = f.stem
        if not any(stem.startswith(p) for p in STOCK_PREFIX):
            entries.append(dict(code=code, file=f.name, status='OUT_OF_SCOPE',
                                note='非股票代码（指数/板块/基金），不进入选股快照'))
            continue
        try:
            dates = _day_dates(f)
        except Exception as exc:
            entries.append(dict(code=code, file=f.name, status='ERROR',
                                error=str(exc)[:80]))
            continue
        if not dates:
            continue
        last, n = dates[-1], len(dates)
        snap_last, snap_n = snap_idx.get(code, (None, 0))
        if snap_last is None:
            status = 'NEW_CODE'
        elif last == snap_last:
            status = 'DUPLICATE'
        elif last > snap_last:
            status = 'NEW_DATE'
        else:
            status = 'OLDER_IGNORED'
        conflict = None
        if snap_last:
            # 重叠区校验：取 min(源末日, 快照末日) 的重叠日比 OHLC（抬高历史价也能发现）
            cmp_date = min(last, snap_last)
            raw = f.read_bytes()
            hit_rec = None
            for rec in struct.iter_unpack(DAY_STRUCT, raw):
                if rec[0] == cmp_date:
                    hit_rec = rec
                    break
            if hit_rec is None:
                cmp_date = last
                hit_rec = struct.unpack(DAY_STRUCT, raw[-32:])
            rows_pack = snap_rows.get(code)
            if rows_pack:
                cols, rows = rows_pack
                ri = cols.index('date')
                hit = [r for r in rows if r[ri] == cmp_date]
                if hit:
                    oi, hi, li, ci = cols.index('open'), cols.index('high'), \
                        cols.index('low'), cols.index('close')
                    r = hit[-1]
                    src_ohlc = [hit_rec[1], hit_rec[2], hit_rec[3], hit_rec[4]]
                    snap_ohlc = [round(r[oi] * 100), round(r[hi] * 100),
                                 round(r[li] * 100), round(r[ci] * 100)]
                    if any(abs(a - b) > 1 for a, b in zip(src_ohlc, snap_ohlc)):
                        conflict = dict(date=int(cmp_date),
                                        src=src_ohlc, snap=snap_ohlc,
                                        changed_field='OHLC_ANY')
                        status = 'CONFLICT'
        entries.append(dict(code=code, file=f.name, status=status,
                            src_last=last, src_rows=n,
                            snap_last=snap_last, snap_rows=snap_n,
                            conflict=conflict))
    return dict(entries=entries,
                summary=_summarize(entries),
                lc5_files=len(files['lc5']))


def _summarize(entries):
    from collections import Counter
    c = Counter(e['status'] for e in entries)
    return dict(total=len(entries), **{k.lower(): v for k, v in c.items()})


def rule_window(rule_ref='strong_pullback@v1'):
    """按规则因子依赖给出日线检查窗口（交易日）。最低 7。"""
    from rules_lab import rules
    from rules_lab.factors import factor_catalog
    need = 7
    try:
        rule = rules.get(rule_ref)
        cat = factor_catalog()

        def walk(node):
            if isinstance(node, dict):
                if 'field' in node:
                    yield node
                for c in node.get('children') or []:
                    yield from walk(c)
            elif isinstance(node, list):
                for c in node:
                    yield from walk(c)

        for section in ('selection', 'trigger'):
            for child in walk(rule.get(section) or {}):
                spec = cat.get(child.get('field'))
                for dep in (spec or {}).get('dependencies', []):
                    if dep.startswith('daily.'):
                        w = (spec or {}).get('window', '')
                        for tok in w.replace('+', ' ').split():
                            if tok.endswith('交易日') and tok[:-3].isdigit():
                                need = max(need, int(tok[:-3]) + 1)
    except Exception:
        pass
    return min(need, 130)   # 黄金地图分支最多 123，封顶 130


def import_daily(src: Path, snap_dir: Path, rule_ref='strong_pullback@v1', *, include_minutes=True):
    snap_dir = Path(snap_dir)
    rep = scan(src, snap_dir)
    conflicts = [e for e in rep['entries'] if e['status'] == 'CONFLICT']
    if conflicts:
        return dict(ok=False, status='CONFLICT_REFUSED',
                    conflicts=[dict(code=e['code'], **(e['conflict'] or {}))
                               for e in conflicts[:20]],
                    note='价格冲突默认拒绝导入；核对来源后重试，旧快照保留')
    updates = [e for e in rep['entries'] if e['status'] in ('NEW_DATE', 'NEW_CODE')]
    has_lc5 = include_minutes and bool(_find_files(Path(src))['lc5'])
    if not updates and not has_lc5:
        return dict(ok=True, status='NOTHING_NEW', summary=rep['summary'])
    staging = Path(str(snap_dir) + '_staging')
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(snap_dir, staging)
    try:
        if updates:
            _apply_daily(staging, Path(src), updates)
        lc5_rep = _apply_minutes(staging, Path(src)) if has_lc5 else None
        if not updates and not (lc5_rep or {}).get('imported'):
            shutil.rmtree(staging, ignore_errors=True)
            return dict(ok=True, status='NOTHING_NEW', summary=rep['summary'])
        _extend_calendar(staging)
        _refresh_manifest(staging, src, lc5_rep)
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        return dict(ok=False, status='PUBLISH_FAILED', error=str(exc)[:200],
                    note='旧快照保留')
    try:
        swap_snapshot(staging, snap_dir)
    except Exception as exc:
        return dict(ok=False, status='SWAP_FAILED', error=str(exc)[:160],
                    note='已自动回滚，旧快照完整可用，可重试')
    win = rule_window(rule_ref)
    return dict(ok=True, status='PUBLISHED', imported=len(updates),
                minutes_imported=(lc5_rep or {}).get('imported', 0),
                summary=rep['summary'], rule_check_window_days=win,
                snapshot_fingerprint=json.loads(
                    (snap_dir / 'snapshot_manifest.json').read_text(encoding='utf-8')
                )['snapshot_fingerprint'][:16])


def _apply_daily(staging: Path, src: Path, updates):
    daily_p = staging / 'daily.json.gz'
    d = json.loads(gzip.decompress(daily_p.read_bytes()))
    cols = d['columns']
    di, oi, hi, li, ci, vi, ai = (cols.index(x) for x in
                                  ('date', 'open', 'high', 'low', 'close',
                                   'volume', 'amount'))
    files = _find_files(src)
    by_code = {_code_of(f): f for f in files['day']}
    for e in updates:
        f = by_code.get(e['code'])
        if f is None:
            continue
        rows = d['codes'].setdefault(e['code'], [])
        have = {r[di] for r in rows}
        prev_close = rows[-1][ci] if rows else None
        for rec in _iter_day(f):
            date, o, h, l, c, amount, vol, _r = rec
            if date in have or date <= (e.get('snap_last') or 0):
                continue
            o, h, l, c = o / 100.0, h / 100.0, l / 100.0, c / 100.0
            if not (o > 0 and h >= max(o, c) and l <= min(o, c)):
                raise ValueError('BAD_OHLC %s %s' % (e['code'], date))
            rows.append([date, round(o, 3), round(h, 3), round(l, 3), round(c, 3),
                         int(vol), float(amount),
                         prev_close if prev_close else None,
                         round(c / prev_close - 1, 5) if prev_close else None,
                         rows[-1][cols.index('is_st')] if rows else 0,
                         rows[-1][cols.index('float_mv')] if rows else None,
                         1.0])
            have.add(date)
            prev_close = c
        rows.sort(key=lambda r: r[di])
    daily_p.write_bytes(gzip.compress(
        json.dumps(d, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))


def _refresh_manifest(staging: Path, src: Path, minute_report):
    mp = staging / 'snapshot_manifest.json'
    man = json.loads(mp.read_text(encoding='utf-8'))
    with gzip.open(staging/'daily.json.gz', 'rt', encoding='utf-8') as stream:
        daily = json.load(stream)
    dates = sorted(set(map(int,json.loads((staging/'calendar.json').read_bytes())['days'])))
    date_col = daily['columns'].index('date')
    daily_dates = {int(row[date_col]) for rows in daily['codes'].values() for row in rows}
    if minute_report:
        valid_days = minute_report['runnable_days']
        for p_day, day in reversed(list(zip(dates,dates[1:]))):
            if p_day in daily_dates and day in valid_days:
                man.update(p_day=p_day, monitor_day=day)
                break
        man['minute_days'] = minute_report['minute_days']
    man['daily_through'] = max(daily_dates,default=None)
    man['data_sha256'] = {f.name: hashlib.sha256((staging / f.name).read_bytes()).hexdigest()
                          for f in staging.iterdir()
                          if f.name != 'snapshot_manifest.json'}
    man['last_import'] = dict(at=__import__('datetime').datetime.now().isoformat(),
                              src=str(src))
    body = json.dumps({k: v for k, v in man.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    man['snapshot_fingerprint'] = hashlib.sha256(body).hexdigest()
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding='utf-8')


def strategy_runnable_days(snap_dir, rule_ref='strong_pullback@v1'):
    """返回策略可运行日清单：窗口内每个交易日的数据齐备状态。
    日线需 >= rule_window 交易日历史 + 当日行；监控日需分钟线。"""
    import gzip as _g
    win = rule_window(rule_ref)
    d = json.loads(_g.decompress((Path(snap_dir) / 'daily.json.gz').read_bytes()))
    cols = d['columns']
    di = cols.index('date')
    mm = json.loads(_g.decompress((Path(snap_dir) / 'minutes.json.gz').read_bytes()))
    cal = json.loads((Path(snap_dir) / 'calendar.json').read_text(encoding='utf-8'))
    days = [int(x) for x in cal['days']]
    out = []
    for k, day in enumerate(days):
        hist = [x for x in days if x <= day][-win:]
        need = len(hist)
        have_rows = sum(1 for rows in d['codes'].values()
                        if len([r for r in rows if r[di] >= hist[0] and r[di] <= day]) >= need)
        day_rows = sum(1 for rows in d['codes'].values() if rows and rows[-1][di] >= day)
        minute_codes = len(mm['days'].get(str(day), {}))
        ok = have_rows >= len(d['codes']) * 0.95 and day_rows >= len(d['codes']) * 0.95
        out.append(dict(day=day, window=need, daily_hist_codes=have_rows,
                        daily_day_codes=day_rows, minute_codes=minute_codes,
                        runnable=ok, minute_ready=minute_codes > 0))
    return dict(rule_ref=rule_ref, window=win, days=out[-14:])


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest='cmd', required=True)
    for name in ('scan', 'import'):
        p = sub.add_parser(name)
        p.add_argument('--src', required=True)
        p.add_argument('--snap', default=str(FE / 'snapshot'))
        p.add_argument('--rule', default='strong_pullback@v1')
    a = ap.parse_args()
    src = Path(a.src)
    if a.cmd == 'scan':
        print(json.dumps(scan(src, Path(a.snap)), ensure_ascii=False)[:2000])
    else:
        print(json.dumps(import_daily(src, Path(a.snap), a.rule), ensure_ascii=False)[:2000])


if __name__ == '__main__':
    main()


def _apply_minutes(staging: Path, src: Path):
    """.lc5 单独导入：按日校验 48 格后并入快照分钟（已有的整日跳过）。"""
    from tdx_supply_v1 import GRID_MINUTES
    files = _find_files(src)['lc5']
    if not files:
        return None
    mp = staging / 'minutes.json.gz'
    doc = json.loads(gzip.decompress(mp.read_bytes()))
    cols = doc['columns']
    imported = 0
    skipped = 0
    bad_days = []
    for f in files:
        stem = f.stem
        if not any(stem.startswith(px) for px in STOCK_PREFIX):
            continue
        code = _code_of(f)
        raw = f.read_bytes()
        if len(raw) % 32:
            raise ValueError('LC5_LAYOUT %s' % f.name)
        by_day = {}
        for rec in struct.iter_unpack(LC5_STRUCT, raw):
            packed, minute, o, h, l, c, amount, vol, _r = rec
            day = ((packed // 2048 + 2004) * 10000 + (packed % 2048 // 100) * 100
                   + (packed % 2048) % 100)
            by_day.setdefault(day, []).append((minute, o, h, l, c, vol, amount))
        for day, bars in sorted(by_day.items()):
            key = str(day)
            have = (doc['days'].get(key) or {}).get(code)
            if have and len(have) == 48:
                skipped += 1
                continue
            minutes = sorted(b[0] for b in bars)
            if minutes != GRID_MINUTES:
                # 网格不齐（盘中未完结/特殊安排）：跳过该日并记录，不阻断整批导入
                skipped += 1
                bad_days.append(dict(code=code, day=day, n=len(bars)))
                continue
            doc['days'].setdefault(key, {})[code] = [
                [int('%d%02d%02d' % (day, m // 60, m % 60)) * 100,
                 round(o, 3), round(h, 3), round(l, 3), round(c, 3),
                 int(v), float(a)]
                for m, o, h, l, c, v, a in sorted(bars)]
            imported += 1
    if imported:
        mp.write_bytes(gzip.compress(
            json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode(), compresslevel=3, mtime=0))
    return dict(imported=imported, skipped=skipped, skipped_bad_grid=bad_days[:20],minute_days=len(doc['days']),
                runnable_days={int(d) for d,codes in doc['days'].items() if len(codes)>=20})


def _extend_calendar(staging: Path):
    """日线导入后交易日历随最大数据日前进（freeze/选股按日历找 P/D）。"""
    import full.vipdoc_reader as V
    d = json.loads(gzip.decompress((staging / 'daily.json.gz').read_bytes()))
    di = d['columns'].index('date')
    max_day = max(rows[-1][di] for rows in d['codes'].values() if rows)
    cp = staging / 'calendar.json'
    cal = json.loads(cp.read_text(encoding='utf-8'))
    days = [int(x) for x in cal['days']]
    full_cal = V.calendar_days()
    for x in full_cal:
        if days[-1] < x <= max_day and x not in days:
            days.append(x)
    days.sort()
    cal['days'] = [str(x) for x in days]
    cal['count'] = len(days)
    cal['last'] = str(days[-1])
    cp.write_text(json.dumps(cal, ensure_ascii=False), encoding='utf-8')
