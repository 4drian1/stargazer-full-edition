"""黄金分割观察区（GOLDEN_FIB）：全部输入为 P 日收盘前日线，与 ATR_BAND 并列的第二种观察区类型。

口径与 full/golden.py 的参考档一致：最近确认波段（端点前后各 2 根不更极值，
涨幅 >= 20%），斐波那契回撤位 0.236/0.382/0.5/0.618/0.786。
取「不高于 P 日收盘的最浅回撤位」为观察区下沿（最接近收盘的下档位），
上沿 = min(收盘, 档位 + tolerance_atr×ATR14)。收盘跌破最深档（0.786 位）= 波段失效，
不冻结；其余只要存在确认波段就冻结观察区，等待盘中回调触达。
本模块不依赖 vendor，可独立回归。
"""
from __future__ import annotations

RATIOS = (0.236, 0.382, 0.5, 0.618, 0.786)


def confirmed_wave(daily_rows, lookback=60, confirm=2):
    """最近显著波段：(确认低点, 确认高点)。高点须在低点之后，且涨幅>=20%。
    与 full/golden.py confirmed_wave 同口径（端点右侧必须 confirm 根完整 K 线）。"""
    rows = daily_rows[-(lookback + confirm):]
    if len(rows) < 2 * confirm + 3:
        return None
    n = len(rows)

    def low_confirmed(i):
        if i - confirm < 0 or i + confirm >= n:
            return False
        lo = rows[i]['low']
        if any(rows[j]['low'] <= lo for j in range(i - confirm, i)):
            return False
        return not any(rows[j]['low'] < lo for j in range(i + 1, i + 1 + confirm))

    def high_confirmed(i):
        if i - confirm < 0 or i + confirm >= n:
            return False
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


def golden_zone(daily_rows, atr, tolerance_atr):
    """GOLDEN_FIB 观察区几何。返回 None = 不冻结（无确认波段/收盘跌破最深档/输入缺）。"""
    if not daily_rows or atr is None or atr <= 0:
        return None
    close = daily_rows[-1].get('close')
    if close is None or close <= 0:
        return None
    wave = confirmed_wave(daily_rows)
    if not wave:
        return None
    width = wave['high'] - wave['low']
    node, ratio = None, None
    for r in sorted(RATIOS):  # 从浅往深：取不高于收盘的最浅回撤位（最接近收盘的下档位）
        lv = wave['high'] - r * width
        if lv <= close:
            node, ratio = lv, r
            break
    if node is None:
        return None  # 收盘已跌破最深回撤档（0.786 位）：波段失效，不冻结
    zone_lower = round(node, 6)
    zone_upper = round(min(close, node + float(tolerance_atr) * atr), 6)
    return dict(zone_lower=zone_lower, zone_upper=zone_upper,
                zone_width_atr=round((zone_upper - zone_lower) / atr, 6),
                atr14=round(atr, 6), reference_close=close,
                golden_ratio=ratio, golden_node=round(node, 6),
                wave_low=wave['low'], wave_high=wave['high'],
                wave_low_day=wave['low_day'], wave_high_day=wave['high_day'],
                source='GOLDEN_FIB_FROM_CONFIRMED_WAVE_P_CLOSE')
