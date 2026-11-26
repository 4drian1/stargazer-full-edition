"""因子注册表：只有这里登记过的因子才能被规则引用（name@version）。

所有因子都是纯函数：
  * 盘后/盘前因子只用 date <= P 的日线；
  * 盘中因子只用 D 日 ts <= asof_ts 的已完成 5 分钟 bar + 冻结观察区；
  * 数据不足返回 None（解释器归为「待核验」），绝不 NaN→0。
"""
from __future__ import annotations

from datetime import datetime

from .contract import FIELD_WHITELIST

FACTOR_VERSION = 'f1'


class FactorSpec:
    __slots__ = ('name', 'version', 'unit', 'window', 'granularity',
                 'available_at', 'direction', 'dependencies')

    def __init__(self, name):
        meta = FIELD_WHITELIST[name]
        self.name = name
        self.version = FACTOR_VERSION
        self.unit = meta['unit']
        self.window = meta['window']
        self.granularity = meta['granularity']
        self.available_at = meta['available_at']
        self.direction = meta['direction']
        self.dependencies = meta['dependencies']

    def as_dict(self):
        return {k: getattr(self, k) for k in self.__slots__}


FACTOR_REGISTRY = {name: FactorSpec(name) for name in FIELD_WHITELIST}
DAILY_FIELDS = ('close', 'open', 'high', 'low', 'pre_close', 'volume',
                'amount', 'pct_chg', 'float_mv', 'is_st')
SELECTION_FACTORS = ('close', 'open', 'high', 'low', 'pre_close', 'volume',
                     'amount', 'pct_chg', 'float_mv', 'is_st', 'history_days',
                     'ret_60d', 'ret_5d', 'ret_20d', 'dist_60h', 'dist_20h',
                     'atr14', 'ma20', 'ma60', 'rsi6', 'macd_dif', 'macd_dea',
                     'macd_hist', 'ma20_slope5', 'close_above_ma20',
                     'vol_ratio_5d', 'zone_lower', 'zone_upper',
                     'zone_width_atr', 'daily_turnover_proxy')
TRIGGER_FACTORS = ('quote_fresh', 'in_session', 'bar_close', 'bar_low',
                   'bar_high', 'bar_volume', 'bar_age_seconds',
                   'decline_from_prev_close', 'zone_distance_atr',
                   'price_in_zone', 'dist_to_zone_upper_pct',
                   'dist_to_zone_lower_pct', 'minute_volume_ratio',
                   'same_time_vol_ratio', 'cum_turnover_proxy', 'bar_count',
                   'm5_rsi6', 'm5_ma5', 'm5_ma20', 'm5_macd_dif', 'm5_macd_dea',
                   'm5_macd_hist', 'm15_rsi6', 'm15_ma5', 'm15_ma20',
                   'm15_macd_dif', 'm15_macd_dea', 'm15_macd_hist',
                   'm30_rsi6', 'm30_ma5', 'm30_ma20', 'm30_macd_dif',
                   'm30_macd_dea', 'm30_macd_hist')


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _atr(rows, period=14):
    trs = []
    for r in rows[-(period + 1):]:
        high, low, pre = r.get('high'), r.get('low'), r.get('pre_close')
        if high is None or low is None or pre is None:
            continue
        trs.append(max(high - low, abs(high - pre), abs(low - pre)))
    if len(trs) < period:
        return None
    return _mean(trs[-period:])


def _ma(rows, n, field='close'):
    vals = [r.get(field) for r in rows[-n:]]
    if len(vals) < n or any(v is None for v in vals):
        return None
    return _mean(vals)


def _rsi(closes, n=6):
    """Cutler 风格简单均值 RSI（与 monitor.build_indicators 同口径）。"""
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-(n + 1):-1], closes[-n:]):
        diff = b - a
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain, avg_loss = sum(gains) / n, sum(losses) / n
    if avg_gain + avg_loss == 0:
        return 50.0
    return round(100.0 * avg_gain / (avg_gain + avg_loss), 4)


def _ema(values, n):
    if not values:
        return None
    k = 2.0 / (n + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def _macd(closes):
    """DIF=EMA12-EMA26（与指标补充 build_indicators 同口径，全序列递推）；
    DEA=DIF 序列的 EMA9；柱=2*(DIF-DEA)。序列不足返回待核验。"""
    if len(closes) < 27:
        return None
    k12, k26 = 2.0 / 13, 2.0 / 27
    ema12 = ema26 = closes[0]
    difs = []
    for v in closes:
        ema12 = v * k12 + ema12 * (1 - k12)
        ema26 = v * k26 + ema26 * (1 - k26)
        difs.append(ema12 - ema26)
    if len(difs) < 9:
        return None
    dea = _ema(difs, 9)
    dif = difs[-1]
    return round(dif, 6), round(dea, 6), round(2 * (dif - dea), 6)


def _grid_pos(hhmm):
    """5 分钟 bar 在交易日固定网格中的位置 0..47。
    上午 09:35-11:30 共 24 根，下午 13:05-15:00 共 24 根；网格不被午休跨越。"""
    h, m = divmod(int(hhmm), 100)
    mins = h * 60 + m
    if 575 <= mins <= 690:
        return (mins - 575) // 5
    if 785 <= mins <= 900:
        return 24 + (mins - 785) // 5
    return None


def _bucketize(bars, factor):
    """按交易日固定时段网格把 5 分钟 bar 聚合成 factor 分钟收盘序列。

    bars: [(ts, close), ...]（单日）。桶边界与午休对齐（24 可被 3/6 整除）。
    缺一根即标记缺桶：该桶及之后不参与指标，返回 (closes, missing_buckets)。
    """
    grid = {}
    for ts, c in bars:
        if c is None:
            continue
        pos = _grid_pos(hhmm(ts))
        if pos is not None:
            grid[pos] = c
    closes, missing = [], []
    for b in range(48 // factor):
        idxs = range(b * factor, (b + 1) * factor)
        if all(i in grid for i in idxs):
            closes.append(grid[(b + 1) * factor - 1])
        else:
            missing.append(b)
            break
    return closes, missing


def _aggregate(closes, factor):
    """旧按序号分组保留给单测对照；新口径用 _bucketize（按时间网格）。"""
    full = len(closes) // factor * factor
    if full == 0:
        return []
    return [closes[i + factor - 1] for i in range(0, full, factor)]


def _minute_indicators(out, prefix, series):
    """RSI/MA/MACD 从当时已可见序列（含跨日预热）计算；窗口不足保持待核验。"""
    if len(series) >= 7:
        out[prefix + '_rsi6'] = _rsi(series, 6)
    if len(series) >= 5:
        out[prefix + '_ma5'] = round(sum(series[-5:]) / 5, 6)
    if len(series) >= 20:
        out[prefix + '_ma20'] = round(sum(series[-20:]) / 20, 6)
    macd = _macd(series)
    if macd:
        out[prefix + '_macd_dif'], out[prefix + '_macd_dea'], out[prefix + '_macd_hist'] = macd


def zone_geometry(rows, zone_cfg):
    """冻结观察区：全部由 P 收盘前数据派生。
    ATR_BAND：宽度随波动结构变化；GOLDEN_FIB：最近确认波段的黄金分割回撤位。"""
    if len(rows) < 21:
        return None
    last = rows[-1]
    close, atr = last.get('close'), _atr(rows)
    if close is None or atr is None or atr <= 0:
        return None
    if zone_cfg.get('type') == 'GOLDEN_FIB':
        from .golden_zone import golden_zone
        return golden_zone(rows, atr, float(zone_cfg['tolerance_atr']))
    highs = [r.get('high') for r in rows[-20:]]
    lows = [r.get('low') for r in rows[-20:]]
    if any(v is None for v in highs + lows):
        return None
    range_atr = (max(highs) - min(lows)) / atr
    structural = min(max(range_atr / 10.0, 0.0), 2.0)
    k_lower = float(zone_cfg['k_lower'])
    k_upper = float(zone_cfg['k_upper'])
    width_atr = max(k_lower - k_upper + structural, 0.05)
    upper = close - k_upper * atr
    lower = upper - width_atr * atr
    return dict(zone_lower=round(lower, 6), zone_upper=round(upper, 6),
                zone_width_atr=round(width_atr, 6), atr14=round(atr, 6),
                reference_close=close, structural_range_atr=round(range_atr, 6),
                source='DERIVED_AT_P_CLOSE_FROM_LAST_20_DAILY_BARS')


def compute_selection_factors(rows, zone_cfg):
    out = {name: None for name in SELECTION_FACTORS}
    if not rows:
        return out
    last = rows[-1]
    for f in DAILY_FIELDS:
        out[f] = last.get(f)
    out['history_days'] = len(rows)
    if len(rows) >= 61 and last.get('close') and rows[-61].get('close'):
        out['ret_60d'] = last['close'] / rows[-61]['close'] - 1.0
    highs = [r.get('high') for r in rows[-60:]]
    if len(highs) >= 60 and all(v is not None for v in highs) and last.get('close'):
        out['dist_60h'] = last['close'] / max(highs) - 1.0
    out['atr14'] = _atr(rows)
    out['ma20'] = _ma(rows, 20)
    out['ma60'] = _ma(rows, 60)
    closes = [r.get('close') for r in rows if r.get('close') is not None]
    out['rsi6'] = _rsi(closes, 6) if len(closes) == len(rows) else None
    macd = _macd(closes)
    if macd:
        out['macd_dif'], out['macd_dea'], out['macd_hist'] = macd
    for n, key in ((5, 'ret_5d'), (20, 'ret_20d')):
        if len(rows) >= n + 1 and last.get('close') and rows[-(n + 1)].get('close'):
            out[key] = last['close'] / rows[-(n + 1)]['close'] - 1.0
    h20 = [r.get('high') for r in rows[-20:]]
    if len(h20) >= 20 and all(v is not None for v in h20) and last.get('close'):
        out['dist_20h'] = last['close'] / max(h20) - 1.0
    ma20_now = _ma(rows, 20)
    ma20_prev = _ma(rows[:-5], 20)
    if ma20_now and ma20_prev:
        out['ma20_slope5'] = ma20_now / ma20_prev - 1.0
    if ma20_now is not None and last.get('close') is not None:
        out['close_above_ma20'] = 1 if last['close'] > ma20_now else 0
    v5, v20 = _mean([r.get('volume') for r in rows[-5:]]), \
        _mean([r.get('volume') for r in rows[-20:]])
    if v5 is not None and v20 and v20 > 0:
        out['vol_ratio_5d'] = v5 / v20
    geo = zone_geometry(rows, zone_cfg)
    if geo:
        for k in ('zone_lower', 'zone_upper', 'zone_width_atr', 'atr14'):
            out[k] = geo[k]
    if last.get('amount') and last.get('float_mv'):
        out['daily_turnover_proxy'] = last['amount'] / last['float_mv']
    return out


def ts_to_seconds(ts):
    return datetime.strptime(str(int(ts)), '%Y%m%d%H%M%S').timestamp()


def hhmm(ts):
    return int(str(int(ts))[8:12])


def yyyymmdd(ts):
    return int(str(int(ts))[:8])


def in_session(ts):
    hm = hhmm(ts)
    return (935 <= hm <= 1130) or (1305 <= hm <= 1500)


def compute_trigger_factors(bar, ctx):
    """ctx: day, asof_ts, max_age_seconds, zone_lower/upper, atr14, pre_close,
    prev_bar, completed_volumes, bar_count.
    """
    out = {name: None for name in TRIGGER_FACTORS}
    ts = int(bar['ts'])
    if yyyymmdd(ts) != int(ctx['day']):
        return out
    age = ts_to_seconds(ctx['asof_ts']) - ts_to_seconds(ts)
    if ctx.get('prev_bar') and ts < int(ctx['prev_bar']['ts']):
        return out                    # 非单调 bar：整体不可判定
    out['quote_fresh'] = 1 if (age >= 0 and age <= ctx['max_age_seconds']
                               and in_session(ts)) else 0
    out['in_session'] = 1 if in_session(ts) else 0
    out['bar_age_seconds'] = age
    out['bar_close'] = bar.get('close')
    out['bar_low'] = bar.get('low')
    out['bar_high'] = bar.get('high')
    out['bar_volume'] = bar.get('volume')
    out['bar_count'] = ctx.get('bar_count', 0)
    pre_close = ctx.get('pre_close')
    close = bar.get('close')
    if pre_close and close:
        out['decline_from_prev_close'] = 1.0 - close / pre_close
    lower, upper, atr = ctx.get('zone_lower'), ctx.get('zone_upper'), ctx.get('atr14')
    low = bar.get('low')
    if lower is not None and upper is not None and atr and close is not None and low is not None:
        tol = float(ctx.get('tolerance_atr', 0.2)) * atr
        out['price_in_zone'] = 1 if (low <= upper + tol and close >= lower - tol) else 0
        if close > upper:
            out['zone_distance_atr'] = (close - upper) / atr
        elif close < lower:
            out['zone_distance_atr'] = (lower - close) / atr
        else:
            out['zone_distance_atr'] = 0.0
    vols = ctx.get('completed_volumes') or []
    if vols and bar.get('volume') is not None:
        avg = _mean(vols)
        if avg and avg > 0:
            out['minute_volume_ratio'] = bar['volume'] / avg
    # —— 分钟指标：当日按时间网格对齐（缺一根即缺桶），历史用于跨日预热 ——
    today = ctx.get('today_bars') or []          # [(ts, close, volume), ...] 当日已走完（含当根）
    hist = ctx.get('hist_minutes') or []         # [(day, ts, close, volume), ...] D 日之前
    diag = {}
    today_pairs = [(t, c) for t, c, _v in today if c is not None]
    grid_closes = [c for _t, c in sorted(today_pairs, key=lambda x: _grid_pos(hhmm(x[0])) or 99)]
    hist_days = {}
    for d, t, c, v in hist:
        hist_days.setdefault(d, []).append((t, c, v))
    hist5 = []
    hist15 = []
    hist30 = []
    for d in sorted(hist_days):
        pairs = [(t, c) for t, c, _v in sorted(hist_days[d]) if c is not None]
        a15, m15 = _bucketize(pairs, 3)
        a30, m30 = _bucketize(pairs, 6)
        hist5.extend(c for _t, c in pairs)
        hist15.extend(a15)
        hist30.extend(a30)
        if m15:
            diag.setdefault('hist_missing_buckets', {})['m15_%s' % d] = m15
        if m30:
            diag.setdefault('hist_missing_buckets', {})['m30_%s' % d] = m30
    _minute_indicators(out, 'm5', hist5 + grid_closes)
    a15, miss15 = _bucketize(today_pairs, 3)
    a30, miss30 = _bucketize(today_pairs, 6)
    if miss15:
        diag['m15_missing_bucket'] = miss15[0]
    if miss30:
        diag['m30_missing_bucket'] = miss30[0]
    _minute_indicators(out, 'm15', hist15 + a15)
    _minute_indicators(out, 'm30', hist30 + a30)
    # —— 同刻量比 B：今日同刻累计量 ÷ 历史同刻累计量均值 ——
    lookback = int(ctx.get('vol_ratio_lookback_days') or 10)
    pos = _grid_pos(hhmm(ts))
    if pos is not None and bar.get('volume') is not None:
        today_vols = [v for _t, _c, v in today]
        today_vols = [v for v in today_vols if v is not None]
        idx = len(today_vols)
        if idx >= 1:
            cum_today = sum(today_vols)
            hist_cums = []
            for d in sorted(hist_days):
                dvs = [v for _t, _c, v in sorted(hist_days[d]) if v is not None]
                if len(dvs) >= idx:
                    hist_cums.append(sum(dvs[:idx]))
            if len(hist_cums) >= 5:
                m = _mean(hist_cums)
                if m and m > 0:
                    out['same_time_vol_ratio'] = round(cum_today / m, 4)
            else:
                diag['same_time_vol_ratio_reason'] = \
                    'LOOKBACK_INSUFFICIENT(%d<5)，已声明口径：今日同刻累计 ÷ 历史同刻累计均值(回看%d日)' % (
                        len(hist_cums), lookback)
        else:
            diag['same_time_vol_ratio_reason'] = '今日无已完成量'
    else:
        diag['same_time_vol_ratio_reason'] = 'bar 不在交易网格内或无成交量'
    out['__agg__'] = diag
    cum_amount = ctx.get('cum_amount_so_far')
    float_mv = ctx.get('float_mv')
    if cum_amount and float_mv:
        out['cum_turnover_proxy'] = round(cum_amount / float_mv, 6)
    if upper and close:
        out['dist_to_zone_upper_pct'] = round((close - upper) / upper * 100, 4)
    if lower and close:
        out['dist_to_zone_lower_pct'] = round((close - lower) / lower * 100, 4)
    return out


def apply_formula_factors(env, rule):
    """受限公式因子：表达式来自规则配置，不引入源码变化。"""
    for spec in rule.get('formula_factors') or []:
        from .safe_expr import FactorMissing, evaluate_formula
        try:
            env[spec['name']] = evaluate_formula(spec['expr'], env)
        except (FactorMissing, ValueError):
            env[spec['name']] = None
    return env


def factor_catalog():
    return {name: spec.as_dict() for name, spec in FACTOR_REGISTRY.items()}


def factor_refs(names=SELECTION_FACTORS):
    return ['%s@%s' % (n, FACTOR_VERSION) for n in names]
