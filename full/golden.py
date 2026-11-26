"""黄金档参考：复用 golden_ladder_v3.wave_levels（现行价格规则，非学习分数）。

波段端点确认口径与现行工具一致：端点前后各 2 根不更极值才算确认。
产出标注为 temporary/reference-only，不与规则观察区（ATR 带）混用。
"""
from __future__ import annotations

import sys
from pathlib import Path

_GREEN = Path(__file__).resolve().parents[3] / 'stargazer_ml' / 'v5_hybrid' / 'green_common_profile'
_VENDOR = Path(__file__).resolve().parent / 'vendor'
for _p in (str(_GREEN), str(_VENDOR)):    # 包内 vendor 优先，主仓库兜底
    if _p not in sys.path:
        sys.path.insert(0, _p)

from golden_ladder_v3 import wave_levels  # noqa: E402


def confirmed_wave(daily_rows, lookback=60, confirm=2):
    """最近显著波段：(确认低点, 确认高点)。高点须在低点之后，且涨幅>=20%。"""
    rows = daily_rows[-(lookback + confirm):]
    if len(rows) < 2 * confirm + 3:
        return None
    n = len(rows)

    def low_confirmed(i):
        if i - confirm < 0 or i + confirm >= n:
            return False    # 边界：左右各需 confirm 根完整 K 线
        lo = rows[i]['low']
        if any(rows[j]['low'] <= lo for j in range(i - confirm, i)):
            return False
        return not any(rows[j]['low'] < lo for j in range(i + 1, i + 1 + confirm))

    def high_confirmed(i):
        if i - confirm < 0 or i + confirm >= n:
            return False    # 边界：左右各需 confirm 根完整 K 线
        hi = rows[i]['high']
        if any(rows[j]['high'] >= hi for j in range(i - confirm, i)):
            return False
        return not any(rows[j]['high'] > hi for j in range(i + 1, i + 1 + confirm))

    low_i = None
    for i in range(confirm, n - confirm):
        if low_confirmed(i) and (low_i is None or rows[i]['low'] < rows[low_i]['low']):
            low_i = i
    if low_i is None:
        return None
    high_i = None
    for i in range(low_i + 1, n - confirm):
        if high_confirmed(i) and (high_i is None or rows[i]['high'] > rows[high_i]['high']):
            high_i = i
    if high_i is None:
        return None
    low, high = rows[low_i]['low'], rows[high_i]['high']
    if high <= low * 1.20:
        return None
    return dict(low=low, high=high, low_day=rows[low_i]['date'], high_day=rows[high_i]['date'])


def golden_references(daily_rows, close, atr):
    """返回 [{node, ratio, zone_lower, zone_upper}]（价格 <= close 的回撤档）。"""
    wave = confirmed_wave(daily_rows)
    if not wave:
        return []
    out = []
    for v in wave_levels(wave['low'], wave['high']):
        if v['kind'] == 'RETRACEMENT' and v['price'] <= close:
            out.append(dict(node=round(v['price'], 4), ratio=v['ratio'],
                            zone_lower=round(v['price'], 4),
                            zone_upper=round(min(close, v['price'] + 0.2 * atr), 4),
                            wave_low=wave['low'], wave_high=wave['high'],
                            wave_low_day=wave['low_day'], wave_high_day=wave['high_day'],
                            status='TEMPORARY_REFERENCE_ONLY'))
    return sorted(out, key=lambda x: -x['node'])
