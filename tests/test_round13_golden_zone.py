"""ROUND13 GOLDEN_FIB 观察区回归：
1) 合成波段：低点 10、高点 20（涨幅 100%），收盘落在不同位置时观察区应锁定正确的回撤档；
2) 收盘跌破最浅档（0.236 位）→ 不冻结（None）；
3) 端点右侧不足 confirm 根 → 波段不确认 → None；
4) 真实快照抽样：zone_lower <= reference_close 且 golden_ratio 在允许集合内。
"""
import json
import sys
import gzip
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE))

from rules_lab.golden_zone import golden_zone, confirmed_wave, RATIOS  # noqa: E402
from rules_lab.factors import zone_geometry  # noqa: E402


def mk_wave_rows(low=10.0, high=20.0):
    """构造确认波段：前段下探到 low（左右各 2 根不更低），再拉到 high（右侧 2 根不更高），尾部横盘。"""
    rows = []
    seq = [12, 11, 10, 11, 12, 14, 16, 18, 20, 18.5, 18.5, 18.2, 18.1, 18.0, 18.0, 18.0, 18.0]
    for i, px in enumerate(seq):
        rows.append(dict(date=20261001 + i, open=px, high=px, low=px, close=px,
                         pre_close=seq[i - 1] if i else px))
    return rows


def main():
    rows = mk_wave_rows()
    w = confirmed_wave(rows)
    assert w and abs(w['low'] - 10.0) < 1e-9 and abs(w['high'] - 20.0) < 1e-9, f'wave anchor wrong: {w}'
    print('wave anchors OK:', w['low_day'], '->', w['high_day'])

    atr = 0.5
    tol = 1.0
    # 收盘 18.0：0.236 位 17.64 <= 18 → 锁 0.236 档
    r = golden_zone(rows + [dict(date=20261101, open=18.0, high=18.1, low=17.9, close=18.0, pre_close=18.0)], atr, tol)
    assert r and abs(r['golden_ratio'] - 0.236) < 1e-9, f'ratio wrong: {r}'
    assert abs(r['zone_lower'] - (20 - 0.236 * 10)) < 1e-6
    assert r['zone_lower'] <= r['zone_upper'] <= 18.0  # 观察区在收盘下方，等回调触达
    print('nearest-below level OK:', r['golden_ratio'], r['zone_lower'], r['zone_upper'])

    # 收盘 17.5：0.236 位 17.64 > 收盘 → 顺移到 0.382 位 16.18
    r2 = golden_zone(rows + [dict(date=20261101, open=17.5, high=17.6, low=17.4, close=17.5, pre_close=18.0)], atr, tol)
    assert r2 and abs(r2['golden_ratio'] - 0.382) < 1e-9, f'ratio wrong: {r2}'
    print('step-down level OK:', r2['golden_ratio'], r2['zone_lower'])

    # 收盘 12.0：跌破最深档（0.786 位 12.14）→ 波段失效不冻结
    r3 = golden_zone(rows + [dict(date=20261101, open=12.0, high=12.1, low=11.9, close=12.0, pre_close=18.0)], atr, tol)
    assert r3 is None, 'below deepest retracement must not freeze'
    print('below-deepest rejected OK')

    # 末端高点右侧不足 2 根：不确认
    tail = rows[:-3] + [dict(date=20261101, open=21, high=21.5, low=20.8, close=21.2, pre_close=18.0)]
    r4 = confirmed_wave(tail)
    assert r4 is None or r4['high'] <= 20.0, f'unconfirmed high leaked: {r4}'
    print('unconfirmed high rejected OK')

    # zone_geometry 走 GOLDEN_FIB 分支（>=21 根）
    many = [dict(date=20260701 + i, open=15, high=15.3, low=14.8, close=15.0, pre_close=15.0) for i in range(30)] + rows
    z = zone_geometry(many + [dict(date=20261101, open=17.5, high=17.6, low=17.4, close=17.5, pre_close=18.0)],
                      dict(type='GOLDEN_FIB', tolerance_atr=2.0))
    assert z and z['source'] == 'GOLDEN_FIB_FROM_CONFIRMED_WAVE_P_CLOSE'
    print('zone_geometry GOLDEN_FIB branch OK')

    # 真实快照抽样验证
    d = json.loads(gzip.decompress((FE / 'snapshot' / 'daily.json.gz').read_bytes()))
    cols = d['columns']
    idx = {c: cols.index(c) for c in ('date', 'open', 'high', 'low', 'close', 'pre_close')}
    n_ok, n_none = 0, 0
    for code in list(d['codes'])[:60]:
        raw = [r for r in d['codes'][code] if r[idx['date']] <= 20261117][-62:]
        if len(raw) < 25:
            continue
        srows = [dict(date=int(r[idx['date']]), open=r[idx['open']], high=r[idx['high']],
                      low=r[idx['low']], close=r[idx['close']], pre_close=r[idx['pre_close']]) for r in raw]
        z = zone_geometry(srows, dict(type='GOLDEN_FIB', tolerance_atr=0.2))
        if z is None:
            n_none += 1
            continue
        assert z['zone_lower'] <= z['zone_upper'] <= z['reference_close']
        assert z['golden_ratio'] in RATIOS
        assert z['zone_upper'] >= z['zone_lower'] > 0
        n_ok += 1
    print(f'real snapshot sample: {n_ok} frozen-zone OK, {n_none} not frozen (no wave / below shallowest)')
    assert n_ok > 0
    print('ROUND13_GOLDEN_ZONE_PASS')


if __name__ == '__main__':
    main()
