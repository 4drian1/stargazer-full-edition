"""免费联网补缺：缺口扫描 → eltdx 在线拉分钟线 → 补进快照；拉不到出清单。

- 分钟缺口：候选股在监控日（或历史分钟日）缺线/缺根，用 eltdx(TDX 7709) 拉取，
  校验 48 格时间网格后写入快照 minutes.json.gz（暂存+校验+原子发布+新指纹）。
- 日线缺口：在线全量 zip 过重，不出现在按钮内联；列出明确清单建议走本地导入。
- 断网/供应商未发布 → 对应条目进失败清单并注明原因，快照不受影响。
用法:
  python -m full.gap_fill --snap snapshot --day 20261118 [--codes 300118,000001]
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

sys.path.insert(0, str(FE.parents[1] / 'stargazer_desktop'))
sys.path.insert(0, str(FE.parents[1] / 'stargazer_ml' / 'v5_hybrid' / 'risk_r0_v1'))


def _load_minutes(snap_dir: Path):
    return json.loads(gzip.decompress((snap_dir / 'minutes.json.gz').read_bytes()))


def _save_minutes(snap_dir: Path, doc: dict):
    (snap_dir / 'minutes.json.gz').write_bytes(gzip.compress(
        json.dumps(doc, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    mp = snap_dir / 'snapshot_manifest.json'
    man = json.loads(mp.read_text(encoding='utf-8'))
    man['data_sha256'] = {f.name: hashlib.sha256((snap_dir / f.name).read_bytes()).hexdigest()
                          for f in snap_dir.iterdir() if f.name != 'snapshot_manifest.json'}
    man['last_gap_fill'] = {'at': __import__('datetime').datetime.now().isoformat()}
    body = json.dumps({k: v for k, v in man.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    man['snapshot_fingerprint'] = hashlib.sha256(body).hexdigest()
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding='utf-8')


def find_gaps(snap_dir: Path, days: list[int] | None = None,
              codes: set[str] | None = None):
    """返回 [{'day':, 'code':, 'reason':}] —— 缺整天或缺根（网格不齐）。"""
    from tdx_supply_v1 import GRID_MINUTES
    doc = _load_minutes(Path(snap_dir))
    cols = doc['columns']
    ti = cols.index('ts')
    out = []
    daily_codes = set()
    dp = Path(snap_dir) / 'daily.json.gz'
    if dp.is_file():
        d0 = json.loads(gzip.decompress(dp.read_bytes()))
        daily_codes = set(d0['codes'])
        del d0
    for dkey, day_codes in doc['days'].items():
        if days and int(dkey) not in days:
            continue
        # scope = 指定代码 ∪ 快照日线宇宙（默认按钮也要发现整天缺席的票）
        scope = (codes if codes is not None else daily_codes) | set(day_codes)
        for code in sorted(scope):
            bars = day_codes.get(code)
            if not bars:
                out.append(dict(day=int(dkey), code=code, reason='NO_MINUTE_FILE_FOR_DAY'))
                continue
            # yyyymmddHHMMSS -> 当日分钟（HHMM）：去秒再取低四位
            minutes = sorted({(((int(b[ti]) // 100) % 10000) // 100) * 60
                              + ((int(b[ti]) // 100) % 10000) % 100
                              for b in bars})
            if minutes != GRID_MINUTES:
                miss = sorted(set(GRID_MINUTES) - set(minutes))
                out.append(dict(day=int(dkey), code=code,
                                reason='GRID_INCOMPLETE missing=%s' % miss[:8]))
    return out


def fill_gaps(snap_dir: Path, days: list[int] | None = None,
              codes: set[str] | None = None, lookback_days: int = 0):
    """在线拉取并补分钟缺口；返回报告。仅修改分钟缺口，日线缺口出清单。"""
    import market_data_config_v1 as md_cfg
    from tdx_supply_v1 import _client, validate_day_bars, fetch_bars_for
    snap_dir = Path(snap_dir)
    gaps = find_gaps(snap_dir, days=days, codes=codes)
    report = dict(gaps_found=len(gaps), filled=0, failed=[], daily_gap_note=(
        '日线缺口不在线补齐（官方 zip 体积过大）；请用「本地导入」或数据中心清单'))
    if not gaps:
        report['status'] = 'NO_GAPS'
        return report
    by_code = {}
    for g in gaps:
        by_code.setdefault(g['code'], set()).add(g['day'])
    doc = _load_minutes(snap_dir)
    cols = doc['columns']
    staging = Path(str(snap_dir) + '_gapfill')
    if staging.exists():
        shutil.rmtree(staging)
    shutil.copytree(snap_dir, staging)
    doc = _load_minutes(staging)
    vip = md_cfg.vipdoc_root()
    try:
        client_ctx = _client()
        client = client_ctx.__enter__()
    except Exception as exc:
        shutil.rmtree(staging, ignore_errors=True)
        report['status'] = 'CONNECT_FAILED'
        report['failed'].append(dict(code='*', reason='SUPPLIER_CONNECT:%s:%s' % (
            type(exc).__name__, str(exc)[:100])))
        report['note'] = '免费链路暂不可达；快照未改动，可稍后重试或走本地导入'
        return report
    with client_ctx:
        for code, want in sorted(by_code.items()):
            stem = ('sh' if code.startswith('6') else 'sz') + code
            if not (vip / stem[:2] / 'lday' / (stem + '.day')).is_file():
                report['failed'].append(dict(code=code, reason='NOT_IN_VIPDOC_UNIVERSE'))
                continue
            try:
                got = fetch_bars_for(client, stem, want)
            except Exception as exc:
                report['failed'].append(dict(code=code, reason='%s:%s' % (type(exc).__name__, str(exc)[:80])))
                continue
            ok = True
            for day in sorted(want):
                bars = got.get(day)
                err = validate_day_bars(day, bars) if bars else 'NO_BARS_FROM_SUPPLIER'
                if err:
                    report['failed'].append(dict(code=code, day=day, reason=err))
                    ok = False
                    break
            if not ok:
                continue
            for day in sorted(want):
                rows = [[int('%d%02d%02d' % (day, m // 60, m % 60)) * 100,
                         o, h, l, c, int(v), amt]
                        for m, o, h, l, c, amt, v in sorted(got[day])]
                doc['days'].setdefault(str(day), {})[code] = rows
            report['filled'] += 1
    if report['filled']:
        _save_minutes(staging, doc)
        from full.data_center import swap_snapshot
        swap_snapshot(staging, snap_dir)
        report['status'] = 'PUBLISHED'
        report['snapshot_fingerprint'] = json.loads(
            (snap_dir / 'snapshot_manifest.json').read_text(encoding='utf-8')
        )['snapshot_fingerprint'][:16]
    else:
        shutil.rmtree(staging, ignore_errors=True)
        report['status'] = 'NOTHING_FILLED'
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--snap', default=str(FE / 'snapshot'))
    ap.add_argument('--day', type=int, nargs='*')
    ap.add_argument('--codes', default=None)
    a = ap.parse_args()
    codes = set(a.codes.split(',')) if a.codes else None
    print(json.dumps(fill_gaps(Path(a.snap), days=a.day or None, codes=codes),
                     ensure_ascii=False)[:2000])


if __name__ == '__main__':
    main()
