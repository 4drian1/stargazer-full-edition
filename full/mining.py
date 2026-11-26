"""受控因子挖掘（借鉴 tick-stock-panel mining 口径，简化实现）。

流水线：因子矩阵 → 嵌套 walk-forward（purge/embargo 防前视）→ 训练折内
学方向 + 相关去重 + beam 组合搜索 → inner test 选候选 → outer test 一次性评估。
候选库记录逐折证据与晋级门槛判断（IC 口径：|IC|>0.03 且正 IC 折占比>=2/3）。
数据窗口短（可标记日少）时折数少，机制优先于结论。
"""
from __future__ import annotations

import itertools
import json
import math
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

from full.factor_lib import FACTOR_LIBRARY, _spearman  # noqa: E402


def _expr_of(name):
    for fam in FACTOR_LIBRARY.values():
        if name in fam:
            return fam[name][0]
    raise ValueError('UNKNOWN_FACTOR:' + name)


def build_panel(rule_ref, p_days, factor_names):
    """{p_day: {code: {f: value}}} + labels {p_day: {code: ret_1d}}。"""
    from full.factor_research import _forward_labels
    from rules_lab import rules
    from rules_lab.factors import compute_selection_factors
    from rules_lab.selection import run_selection
    from rules_lab.snapshot import open_snapshot
    from rules_lab.state_store import RunContext
    import hashlib
    rule = rules.get(rule_ref)
    snap = open_snapshot()
    panel, labels = {}, {}
    for p in p_days:
        run = RunContext('mining_%s' % hashlib.sha256(
            (rule_ref + str(p)).encode()).hexdigest()[:8], meta=dict(scratch=True))
        sel = run_selection(snap, rule, int(p), run, day=None)
        day_panel = {}
        for code in sel['accepted']:
            srow = sel['rows'][code]
            env = {c['field']: c.get('actual') for c in srow.get('conditions', [])}
            fac = compute_selection_factors(snap.daily[code], rule['zone'])
            for k, v in fac.items():
                env.setdefault(k, v)
            from rules_lab.safe_expr import evaluate_formula
            from rules_lab.factors import factor_catalog
            allowed = set(factor_catalog())
            row = {}
            for fn in factor_names:
                try:
                    row[fn] = evaluate_formula(_expr_of(fn), env, allowed_names=allowed)
                except Exception:
                    row[fn] = None
            day_panel[code] = row
        lab = {c: (_forward_labels(snap, c, int(p)).get('ret_1d'))
               for c in day_panel}
        panel[int(p)], labels[int(p)] = day_panel, lab
    return panel, labels


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    r = [0.0] * len(values)
    i = 0
    while i < len(values):
        j = i
        while j + 1 < len(values) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2.0 + 1
        i = j + 1
    return r


def daily_ic(panel, labels, day, factor, direction=1):
    rows = [(v[factor], labels[day].get(c)) for c, v in panel[day].items()
            if v.get(factor) is not None and labels[day].get(c) is not None]
    if len(rows) < 8:
        return None
    xs = [direction * x for x, _ in rows]
    return _spearman(xs, [y for _, y in rows])


def learn_directions(panel, labels, days, factors):
    """训练折内学每个因子的方向：IC 均值符号。"""
    out = {}
    for f in factors:
        ics = [daily_ic(panel, labels, d, f) for d in days]
        ics = [x for x in ics if x is not None]
        out[f] = 1 if (sum(ics) / len(ics) if ics else 0) >= 0 else -1
    return out


def dedup(panel, labels, days, factors, directions, thresh=0.7):
    """pairwise Spearman 去重：相关>=thresh 保留 |IC| 高者。"""
    def pooled(f):
        xs, ys = [], []
        for d in days:
            for c, v in panel[d].items():
                if v.get(f) is not None and labels[d].get(c) is not None:
                    xs.append(directions[f] * v[f])
                    ys.append(labels[d][c])
        return xs, ys
    score = {}
    for f in factors:
        xs, ys = pooled(f)
        score[f] = abs(_spearman(xs, ys)) if len(xs) >= 8 else 0.0
    keep = list(factors)
    for a, b in itertools.combinations(factors, 2):
        if a not in keep or b not in keep:
            continue
        xa, _ = pooled(a)
        xb, _ = pooled(b)
        if len(xa) >= 8 and len(xb) >= 8 and xa and xb:
            corr = _spearman(xa[:min(len(xa), len(xb))], xb[:min(len(xa), len(xb))])
            if corr is not None and abs(corr) >= thresh:
                drop = b if score[a] >= score[b] else a
                keep.remove(drop)
    return keep


def combo_ic(panel, labels, day, combo, directions):
    rows = []
    for c, v in panel[day].items():
        if labels[day].get(c) is None:
            continue
        vals = [v.get(f) for f in combo]
        if any(x is None for x in vals):
            continue
        rows.append((sum(directions[f] * x for f, x in zip(combo, vals)), labels[day][c]))
    if len(rows) < 8:
        return None
    xs = [x for x, _ in rows]
    return _spearman(xs, [y for _, y in rows])


def beam_search(panel, labels, days, factors, directions, max_k=3, beam_width=8):
    """贪心 beam：单因子 → 逐层加因子，按训练折均值 IC 截断。"""
    def mean_ic(combo):
        ics = [combo_ic(panel, labels, d, combo, directions) for d in days]
        ics = [x for x in ics if x is not None]
        return sum(ics) / len(ics) if ics else 0.0
    scored = sorted(((mean_ic((f,)), (f,)) for f in factors), key=lambda x: -x[0])
    beam = [c for _, c in scored[:beam_width]]
    best = beam[0]
    for _ in range(max_k - 1):
        cands = []
        for combo in beam:
            for f in factors:
                if f in combo:
                    continue
                nc = tuple(sorted(combo + (f,)))
                cands.append((mean_ic(nc), nc))
        if not cands:
            break
        cands.sort(key=lambda x: -x[0])
        if cands[0][0] <= mean_ic(best) + 1e-9 and len(best) >= 2:
            break
        beam = [c for _, c in cands[:beam_width]]
        best = beam[0]
    return best, mean_ic(best)


def mine(rule_ref, factor_names=None, p_days=None, purge=1, max_k=3):
    factor_names = factor_names or [n for fam in FACTOR_LIBRARY.values() for n in fam]
    if not p_days:
        # 缺省取快照日历中不晚于 P 日的最近 5 个交易日（数据更新后自动跟随）
        man = json.loads((FE / 'snapshot' / 'snapshot_manifest.json').read_text(encoding='utf-8'))
        cal = json.loads((FE / 'snapshot' / 'calendar.json').read_text(encoding='utf-8'))['days']
        p_days = [int(d) for d in cal if int(d) <= int(man['p_day'])][-5:]
    p_days = [int(d) for d in p_days]
    panel, labels = build_panel(rule_ref, p_days, factor_names)
    labeled = sorted(d for d in p_days if labels.get(d) and any(v is not None for v in labels[d].values()))
    if len(labeled) < 4:
        return dict(ok=False, error='LABELED_DAYS_INSUFFICIENT(%d<4)' % len(labeled),
                    note='前向标签未成熟的日不够，先导入更多数据')
    # 简化嵌套留出：outer 折 = 每个测试日；其训练窗 = 之前 purge 日以外的已标记日
    folds, candidates = [], {}
    for i, test_day in enumerate(labeled):
        train = [d for d in labeled if d < test_day and
                 labeled.index(d) < labeled.index(test_day) - purge + 1 and
                 (labeled.index(test_day) - labeled.index(d)) > purge]
        if len(train) < 2:
            folds.append(dict(test_day=test_day, skipped='TRAIN_DAYS_INSUFFICIENT'))
            continue
        directions = learn_directions(panel, labels, train, factor_names)
        keep = dedup(panel, labels, train, factor_names, directions)
        combo, train_ic = beam_search(panel, labels, train, keep, directions, max_k=max_k)
        test_ic = combo_ic(panel, labels, test_day, combo, directions)
        folds.append(dict(test_day=test_day, train_days=train, factors=list(combo),
                          train_ic=round(train_ic, 4),
                          test_ic=round(test_ic, 4) if test_ic is not None else None))
        key = '+'.join(combo)
        candidates.setdefault(key, dict(factors=list(combo), folds=[]))['folds'].append(test_day)
    # 候选晋级门槛（IC 口径）
    out_candidates = []
    for key, cand in candidates.items():
        rows = [f for f in folds if f.get('factors') == cand['factors'] and f.get('test_ic') is not None]
        if not rows:
            continue
        mean_ic = sum(f['test_ic'] for f in rows) / len(rows)
        pos_ratio = sum(1 for f in rows if f['test_ic'] > 0) / len(rows)
        gates = dict(folds_n=len(rows) >= 2, ic_abs=abs(mean_ic) > 0.03, pos=pos_ratio >= 2 / 3)
        out_candidates.append(dict(factors=cand['factors'], folds_n=len(rows),
                                   mean_ic=round(mean_ic, 4), pos_ratio=round(pos_ratio, 3),
                                   gates=gates, status='pending',
                                   promotable=all(gates.values())))
    out_candidates.sort(key=lambda c: -abs(c['mean_ic']))
    result = dict(ok=True, rule_ref=rule_ref, factor_names=factor_names,
                  labeled_days=labeled, purge=purge, folds=folds,
                  candidates=out_candidates,
                  note='嵌套留出：训练窗与测试日之间隔 purge 个交易日；候选默认 pending，'
                       'promotable=IC 口径门槛(|IC|>0.03 且正 IC 折>=2/3)')
    p = FE / 'reports' / ('mining_%s.json' % rule_ref.replace('@', '_'))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding='utf-8')
    result['report_path'] = str(p)
    return result


if __name__ == '__main__':
    print(json.dumps(mine('strong_pullback@v1'), ensure_ascii=False)[:2500])
