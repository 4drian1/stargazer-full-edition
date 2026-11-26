"""波段端点确认边界回归：高点右侧 0、1、2 根 K 线的确认行为。

规则：端点前后各需 confirm(=2) 根完整 K 线才算确认。
- 右侧 0 根（高点就是最后一根）→ 不确认
- 右侧 1 根 → 不确认（修复前的 bug：循环边界放行了这种情况）
- 右侧 2 根 → 确认
低点固定在索引 2（左侧恰好 2 根可确认），高点位置随场景变化。
运行: python tests/test_round3_wave_boundary.py
"""
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE))
sys.path.insert(0, str(FE.parent))

from full.golden import confirmed_wave  # noqa: E402


def make_rows(high_index, n, base=10.0, high=15.0):
    out = []
    for i in range(n):
        h = high if i == high_index else base + (0.1 if i % 2 else -0.1)
        out.append(dict(date=20261000 + i, open=base, high=round(h, 2),
                        low=base - 0.5, close=base))
    out[2]['low'] = 5.0      # 低点（左侧索引 0,1 共 2 根可确认）
    out[2]['high'] = 6.0
    return out


def main():
    # 右侧 2 根：high@5，n=8 → 右邻 6,7 两根更低 → 确认
    ok = confirmed_wave(make_rows(5, 8))
    assert ok and ok['high_day'] == 20261005 and ok['low_day'] == 20261002, \
        'right-2-bars must confirm: %s' % ok
    print('right-2-bars: confirmed ✓')

    # 右侧 1 根：high@5，n=7 → 只有索引 6 一根 → 不确认
    bad1 = confirmed_wave(make_rows(5, 7))
    assert bad1 is None or bad1['high_day'] != 20261005, \
        'right-1-bar must NOT confirm (off-by-one bug): %s' % bad1
    print('right-1-bar: rejected ✓ (修复前此处被放行)')

    # 右侧 0 根：high@7，n=8（最后一根）→ 不确认
    bad0 = confirmed_wave(make_rows(7, 8))
    assert bad0 is None or bad0['high_day'] != 20261007, \
        'right-0-bar must NOT confirm: %s' % bad0
    print('right-0-bar: rejected ✓')

    print('WAVE_BOUNDARY_PASS')


if __name__ == '__main__':
    main()
