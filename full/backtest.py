"""策略回测 v1：触位信号 → 下一可交易时点成交 → T+1/停牌/涨跌停不可成交 →
费用/滑点 → 信号后价格表现与模拟盈亏分列。

输入用监控跑出的触位事件（事件账本已含 fill 判定：NEXT_COMPLETED_BAR + 涨跌停/
停牌 NO_FILL）。出场=入场后第 k 根已完成 5 分钟收盘；T+1 由持仓期>=1 日约束。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))


def backtest_run(run_dir, hold_days=(1, 3, 5), fee=0.00025, stamp_tax=0.0005,
                 slippage=0.001):
    """run_dir: 某次监控运行的目录（含 events/<D>.json）。返回逐信号表+汇总。"""
    from rules_lab.snapshot import open_snapshot
    run_dir = Path(run_dir)
    ev_files = sorted((run_dir / 'events').glob('*.json'))
    if not ev_files:
        return dict(ok=False, error='NO_EVENTS')
    snap = open_snapshot()
    day = int(ev_files[-1].stem)
    events = json.loads(ev_files[-1].read_text(encoding='utf-8'))['events']
    fills = json.loads((run_dir / 'freeze' / ('%s.json' % day)).read_text(encoding='utf-8'))
    exec_cfg = fills.get('candidates')  # fee/slippage 来自规则执行段（回测参数可覆盖）
    rows = []
    for e in events.values():
        code = e.get('code')
        f = e.get('fill') or {}
        if f.get('status') != 'FILLED' or not f.get('fill_price'):
            rows.append(dict(code=code, ts=e.get('bar', {}).get('ts'), status='NO_FILL',
                             reason=f.get('no_fill_reason') or 'NOT_FILLED'))
            continue
        entry = float(f['fill_price'])

        sig = dict(code=code, ts=e.get('bar', {}).get('ts'), entry_ts=f.get('next_bar_ts'),
                   entry=round(entry, 4), status='FILLED')
        drows = snap.daily.get(code) or []
        didx = {r['date']: i for i, r in enumerate(drows)}
        cal = snap.calendar
        d0 = int(f['next_bar_ts']) // 1000000
        later = [d for d in cal if d > d0]
        for k in hold_days:
            """严格 T+1：卖出=买入日之后第 k 个交易日收盘；数据不足 → 留空。"""
            if len(later) < k:
                sig['ret_%dd' % k] = None
                continue
            d_target = later[k - 1]
            i = didx.get(d_target)
            if i is None:
                sig['ret_%dd' % k] = None
                continue
            exitp = float(drows[i]['close'])
            gross = exitp / entry - 1
            cost = fee + slippage + (stamp_tax if gross > 0 else 0) + slippage
            sig['exit_%dd' % k] = round(exitp, 4)
            sig['ret_%dd' % k] = round(gross, 6)          # 信号后价格表现（无成本）
            sig['pnl_%dd' % k] = round(gross - cost, 6)   # 模拟盈亏（费用+滑点+印花税）
        rows.append(sig)
    filled = [r for r in rows if r['status'] == 'FILLED']
    summary = dict(signals=len(rows), filled=len(filled),
                   no_fill=len(rows) - len(filled),
                   no_fill_reasons=_count(r.get('reason') for r in rows if r['status'] == 'NO_FILL'))
    for k in hold_days:
        rets = [r['ret_%dd' % k] for r in filled if r.get('ret_%dd' % k) is not None]
        pnls = [r['pnl_%dd' % k] for r in filled if r.get('pnl_%dd' % k) is not None]
        if rets:
            summary['hold_%dd' % k] = dict(
                n=len(rets), win=round(sum(1 for x in rets if x > 0) / len(rets), 3),
                mean_ret=round(sum(rets) / len(rets), 6),
                mean_pnl=round(sum(pnls) / len(pnls), 6))
    summary['cost_basis'] = dict(fee=fee, stamp_tax=stamp_tax, slippage=slippage,
                                 note='卖出侧印花税只在盈利计；双边滑点')
    # 资金曲线：入场时间排序的累计净盈亏（含成本）
    curve_key = next((k for k in sorted(hold_days) if any(
        x.get('pnl_%dd' % k) is not None for x in filled)), None)
    eq = []
    cum = 0.0
    if curve_key is not None:
        for r in sorted([x for x in filled if x.get('pnl_%dd' % curve_key) is not None],
                        key=lambda x: x['entry_ts']):
            cum += r['pnl_%dd' % curve_key]
            eq.append(dict(ts=r['entry_ts'], cum_pnl=round(cum, 6)))
    summary['equity_key'] = 'pnl_%dd' % curve_key if curve_key is not None else None
    summary['equity_curve'] = eq[:120]
    summary['t_plus_1'] = '卖出日严格晚于买入日（显式 T+1）'
    return dict(ok=True, day=day, summary=summary, rows=rows[:200])


def _count(xs):
    out = {}
    for x in xs:
        out[x] = out.get(x, 0) + 1
    return out


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', required=True)
    a = ap.parse_args()
    print(json.dumps(backtest_run(Path(a.run_dir)), ensure_ascii=False)[:2500])


if __name__ == '__main__':
    main()
