"""单因子研究（第十一轮）：定义公式 → 覆盖/缺失 → 同机会集分层 → 按日留出 → 逐笔复算。

复用 rules_lab.safe_expr 受限表达式与选股因子环境；机会集 = 指定规则在某 P 日
的筛中名单（同机会集对照）。标签用快照日线前向收益，窗口未成熟如实标
INCOMPLETE_FORWARD_WINDOW（1/3/5 日）。结果落盘 reports/factor_<name>_<P>.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

BUCKETS = 3          # 先三分层；更多层沿同一目录接
HOLD_DAYS = (1, 3, 5)


def evaluate_factor(rule_ref, p_day, expr, *, d_day=None, buckets=BUCKETS):
    from rules_lab import rules
    from rules_lab.factors import compute_selection_factors, factor_catalog
    from rules_lab.safe_expr import FactorMissing, evaluate_formula
    from rules_lab.selection import run_selection
    from rules_lab.snapshot import open_snapshot
    from rules_lab.state_store import RunContext
    import hashlib

    rule = rules.get(rule_ref)
    snap = open_snapshot()
    run = RunContext('research_%s' % hashlib.sha256(
        (rule_ref + str(p_day) + expr).encode()).hexdigest()[:10], meta=dict(scratch=True))
    sel = run_selection(snap, rule, int(p_day), run, day=d_day)
    cat = factor_catalog()
    allowed = set(cat) | {c['field'] for c in rule.get('selection', {}).get('children', [])}

    rows, missing = [], []
    for code in sorted(sel['accepted']):
        srow = sel['rows'][code]
        env = {c['field']: c.get('actual') for c in srow.get('conditions', [])}
        # 条件里只带本规则字段；补齐目录字段（覆盖/缺失口径来自同一因子层）
        daily = snap.daily.get(code) or []
        zone_cfg = rule['zone']
        fac = compute_selection_factors(daily, zone_cfg)
        for k, v in fac.items():
            env.setdefault(k, v)
        try:
            value = evaluate_formula(expr, env, allowed_names=allowed)
        except FactorMissing as exc:
            missing.append(dict(code=code, reason=str(exc)[:120]))
            continue
        except ValueError as exc:
            return dict(ok=False, error='FORMULA:%s' % str(exc)[:120])
        if value is None or value != value:
            missing.append(dict(code=code, reason='FORMULA_RESULT_MISSING'))
            continue
        fwd = _forward_labels(snap, code, int(p_day))
        rows.append(dict(code=code, value=round(float(value), 8), **fwd))

    n = len(rows)
    cov = round(n / max(1, len(sel['accepted'])), 4)
    ranked = sorted(rows, key=lambda r: r['value'])
    layer_n = max(1, n // buckets) if n else 0
    layers = []
    for b in range(buckets):
        part = ranked[b * layer_n:(b + 1) * layer_n] if layer_n else []
        if b == buckets - 1 and layer_n:
            part = ranked[b * layer_n:]
        layers.append(dict(
            layer=b + 1, n=len(part),
            value_min=part[0]['value'] if part else None,
            value_max=part[-1]['value'] if part else None,
            mean_fwd_1d=_mean([r.get('ret_1d') for r in part]),
            labels_mature=sum(1 for r in part if r.get('labels_mature')),
            labels_incomplete=sum(1 for r in part if not r.get('labels_mature')),
            codes=[r['code'] for r in part][:20]))
    out = dict(
        ok=True, rule_ref=rule_ref, p_day=int(p_day), expr=expr,
        opportunity_n=len(sel['accepted']), covered_n=n, coverage=cov,
        missing_n=len(missing), missing=missing[:50],
        hold_days=HOLD_DAYS,
        layers=layers,
        rows=[dict(code=r['code'], value=r['value'],
                   ret_1d=r.get('ret_1d'), ret_3d=r.get('ret_3d'), ret_5d=r.get('ret_5d'),
                   labels_mature=r.get('labels_mature'))
              for r in ranked],
        limitation='前向窗口未走完的样本标 INCOMPLETE；正式研究用后续窗口完整的数据',
    )
    name = 'f%s' % hashlib.sha256(expr.encode()).hexdigest()[:8]
    p_out = FE / 'reports' / ('factor_%s_%s.json' % (name, p_day))
    p_out.parent.mkdir(parents=True, exist_ok=True)
    p_out.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    out['report_path'] = str(p_out)
    return out


def _forward_labels(snap, code, p_day):
    """P 收盘后买入视角的前向收益：P+k 收盘 / P 收盘 - 1。窗口不齐 → 未成熟。"""
    rows = snap.daily.get(code) or []
    idx = {int(r['date']): r for r in rows}
    if p_day not in idx:
        return dict(labels_mature=False)
    base = idx[p_day].get('close')
    out = dict(labels_mature=True)
    cal = snap.calendar
    later = [d for d in cal if d > p_day]
    for k in HOLD_DAYS:
        if len(later) >= k and later[k - 1] in idx and base:
            out['ret_%dd' % k] = round(idx[later[k - 1]]['close'] / base - 1, 6)
        else:
            out['ret_%dd' % k] = None
            out['labels_mature'] = False
    return out


def _mean(vals):
    vs = [v for v in vals if v is not None]
    return round(sum(vs) / len(vs), 6) if vs else None


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--rule', default='strong_pullback@v1')
    ap.add_argument('--p-day', type=int, required=True)
    ap.add_argument('--expr', required=True)
    a = ap.parse_args()
    print(json.dumps(evaluate_factor(a.rule, a.p_day, a.expr),
                     ensure_ascii=False)[:2500])


if __name__ == '__main__':
    main()
