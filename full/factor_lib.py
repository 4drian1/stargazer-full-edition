"""因子库与多日截面检验（借鉴 tick-stock-panel 的挖掘口径，简化版）。

- 因子按「因子族」组织（动量/波动/量能/深度/流动性/位置），每个因子=名字+受限公式；
- 检验：对每个 P 日做全截面 Spearman Rank IC（对 1 日前向收益）+ 三层多空价差
  （顶-底前向收益差，只衡量区分度，不代表可执行卖空）；
- 交易日轴用全局日历（停牌不会把下一行误当下一交易日），缺失剔除不填 0；
- 多日汇总：每日 IC 序列、均值/胜率、 pooled 分层价差。
受控组合搜索/相关去重/嵌套 walk-forward 按方案属阶段四后续，本轮先把单因子检验做对。
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

FACTOR_LIBRARY = {
    # family: {name: (formula, 说明)}
    '动量': {
        'mom_5d': ('ret_5d', '近5日涨幅'),
        'mom_20d': ('ret_20d', '近20日涨幅'),
        'mom_60d': ('ret_60d', '近60日涨幅'),
    },
    '深度': {
        'drawdown_60h': ('0 - dist_60h', '距60日高点回撤深度(正=深)'),
        'drawdown_20h': ('0 - dist_20h', '距20日高点回撤深度'),
    },
    '波动': {
        'vol_atr14': ('atr14', '14日真实波幅'),
        'vol_adj_mom': ('ret_5d / atr14', '波动调整动量'),
    },
    '量能': {
        'volratio_5_20': ('vol_ratio_5d', '5日量/20日量'),
        'turnover_proxy': ('daily_turnover_proxy', '成交额/流通市值(近似换手)'),
    },
    '趋势': {
        'ma20_above': ('close_above_ma20', '收盘>20日均线'),
        'ma20_slope': ('ma20_slope5', '20日均线5日斜率'),
    },
    '位置': {
        'rsi6_d': ('rsi6', '日K RSI(6)'),
        'macd_hist_d': ('macd_hist', '日K MACD柱'),
    },
}


def _spearman(xs, ys):
    n = len(xs)
    if n < 8:
        return None
    def ranks(vs):
        order = sorted(range(n), key=lambda i: vs[i])
        r = [0.0] * n
        i = 0
        while i < n:
            j = i
            while j + 1 < n and vs[order[j + 1]] == vs[order[i]]:
                j += 1
            for k in range(i, j + 1):
                r[order[k]] = (i + j) / 2.0 + 1
            i = j + 1
        return r
    rx, ry = ranks(xs), ranks(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = math.sqrt(sum((a - mx) ** 2 for a in rx))
    vy = math.sqrt(sum((b - my) ** 2 for b in ry))
    if not vx or not vy:
        return None
    return round(cov / (vx * vy), 4)


def evaluate_days(rule_ref, expr, p_days, *, buckets=3):
    """多日截面检验：逐日 Rank IC(1d) + 三层多空价差；返回逐日表与汇总。"""
    from full.factor_research import evaluate_factor
    days_report = []
    for p in p_days:
        rep = evaluate_factor(rule_ref, int(p), expr)
        if not rep.get('ok'):
            return rep
        rows = [r for r in rep['rows'] if r.get('ret_1d') is not None]
        ic = _spearman([r['value'] for r in rows], [r['ret_1d'] for r in rows]) if len(rows) >= 8 else None
        ln = rep['layers']
        spread = None
        if ln and ln[0]['mean_fwd_1d'] is not None and ln[-1]['mean_fwd_1d'] is not None:
            spread = round(ln[0]['mean_fwd_1d'] - ln[-1]['mean_fwd_1d'], 6)
        days_report.append(dict(p_day=int(p), n=rep['covered_n'], n_labeled=len(rows),
                                rank_ic_1d=ic, ls_spread_1d=spread,
                                top_mean=ln[0]['mean_fwd_1d'] if ln else None,
                                bottom_mean=ln[-1]['mean_fwd_1d'] if ln else None))
    ics = [d['rank_ic_1d'] for d in days_report if d['rank_ic_1d'] is not None]
    spreads = [d['ls_spread_1d'] for d in days_report if d['ls_spread_1d'] is not None]
    pooled_spread = round(sum(spreads) / len(spreads), 6) if spreads else None
    summary = dict(
        days=len(days_report),
        ic_mean=round(sum(ics) / len(ics), 4) if ics else None,
        ic_winrate=round(sum(1 for x in ics if x > 0) / len(ics), 3) if ics else None,
        ic_n=len(ics),
        ls_spread_mean=pooled_spread,
        note='IC=按日截面Spearman(因子值 vs 1日前向收益)；多空价差=顶层-底层，'
             '只衡量区分度，不代表可执行卖空；label 未成熟的日被剔除统计')
    return dict(ok=True, rule_ref=rule_ref, expr=expr,
                factor_library=FACTOR_LIBRARY, days=days_report, summary=summary)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--rule', default='strong_pullback@v1')
    ap.add_argument('--expr', required=True)
    ap.add_argument('--days', type=int, nargs='+', required=True)
    a = ap.parse_args()
    print(json.dumps(evaluate_days(a.rule, a.expr, a.days), ensure_ascii=False)[:2500])


if __name__ == '__main__':
    main()
