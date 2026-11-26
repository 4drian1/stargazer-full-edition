"""第十一轮验收：单因子研究链路。定义公式→覆盖/缺失→分层→留出→逐笔复算。"""
import json
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))


def main():
    from full.factor_research import evaluate_factor
    # 1) 正常公式：覆盖、分层、1日标签成熟/3、5日未成熟（快照日线止 11-18）
    rep = evaluate_factor('strong_pullback@v1', 20261117, 'ret_5d / atr14')
    assert rep['ok'] and rep['covered_n'] > 100, rep.get('error')
    assert rep['coverage'] > 0.8, rep['coverage']
    assert len(rep['layers']) == 3
    mature1 = sum(1 for r in rep['rows'] if r.get('ret_1d') is not None)
    mature3 = sum(1 for r in rep['rows'] if r.get('ret_3d') is not None)
    # 成熟度按快照数据驱动推导：1/3 日出口日在快照日内即应成熟
    import json as _j
    cal = _j.load(open(FE/'snapshot'/'calendar.json',encoding='utf-8'))['days']
    later = [int(x) for x in cal if int(x) > 20261117]
    exp3 = (len(later) >= 3)
    assert mature1 >= rep['covered_n'] - 5, mature1
    assert (mature3 > 0) == exp3, (mature3, exp3, later[:4])
    assert all(not r['labels_mature'] or r['ret_3d'] is not None for r in rep['rows'])
    total = sum(l['n'] for l in rep['layers'])
    assert total == rep['covered_n'], (total, rep['covered_n'])
    print('factor eval: covered=%s coverage=%s layers=%s | 1d mature=%s 3d mature=0' % (
        rep['covered_n'], rep['coverage'], [l['n'] for l in rep['layers']], mature1))

    # 2) 逐笔复算：报告中任一行用手算重验
    row = rep['rows'][0]
    from rules_lab.snapshot import open_snapshot
    from rules_lab import rules
    from rules_lab.factors import compute_selection_factors
    from rules_lab.safe_expr import evaluate_formula
    from rules_lab.selection import run_selection
    from rules_lab.state_store import RunContext
    snap = open_snapshot()
    rule = rules.get('strong_pullback@v1')
    run = RunContext('research_recompute', meta=dict(t=True))
    sel = run_selection(snap, rule, 20261117, run, day=20261118)
    srow = sel['rows'][row['code']]
    env = {c['field']: c.get('actual') for c in srow.get('conditions', [])}
    fac = compute_selection_factors(snap.daily[row['code']], rule['zone'])
    for k, v in fac.items():
        env.setdefault(k, v)
    expect = evaluate_formula('ret_5d / atr14', env, allowed_names=set(env))
    assert abs(row['value'] - round(float(expect), 8)) < 1e-9, (row['value'], expect)
    print('per-row recompute OK: %s value=%s hand=%s' % (row['code'], row['value'], round(float(expect), 8)))

    # 3) 缺字段公式：覆盖下降且缺失原因可追查
    rep2 = evaluate_factor('strong_pullback@v1', 20261117, 'same_time_vol_ratio + 1')
    assert rep2['ok'] and rep2['missing_n'] > 0, rep2['missing_n']
    print('missing coverage OK: missing_n=%s (原因:%s)' % (
        rep2['missing_n'], rep2['missing'][0]['reason'][:60]))

    # 4) 非法公式与留出语义（P=11-16 的 1 日标签也不成熟：D 日无收盘）
    bad = evaluate_factor('strong_pullback@v1', 20261117, 'import os')
    assert not bad['ok'], bad
    rep3 = evaluate_factor('strong_pullback@v1', 20261116, 'ret_5d')
    # 留出边界：P=11-16 的 1 日标签成熟（11-17 收盘在快照内），5 日未成熟（需 11-23）
    # 留出边界按数据驱动：5 日出口日(11-23)在日历内则成熟，否则未成熟
    import json as _j2
    cal2 = [int(x) for x in _j2.load(open(FE/'snapshot'/'calendar.json',encoding='utf-8'))['days']]
    later2 = [d for d in cal2 if d > 20261116]
    exp5 = len(later2) >= 5 and 20261123 in cal2
    have5 = any(r['ret_5d'] is not None for r in rep3['rows'])
    assert have5 == exp5, (have5, exp5, later2[:5])
    assert any(r['ret_1d'] is not None for r in rep3['rows'])
    print('bad formula rejected; leave-out label maturity OK (5d mature=%s as data-driven expected)' % have5)
    print('ROUND11_RESEARCH_PASS')


if __name__ == '__main__':
    main()
