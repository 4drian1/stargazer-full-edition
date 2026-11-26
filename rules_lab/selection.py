"""盘后选股：同一规则解释器吃 P 日收盘切片，逐项记录值与未命中原因。"""
from __future__ import annotations

from .contract import ABSTAIN, DECISION, FALSE, TRUE, UNKNOWN
from .factors import (SELECTION_FACTORS, apply_formula_factors,
                      compute_selection_factors, factor_catalog, zone_geometry)
from .interpreter import evaluate
from .paths import canonical_sha


def run_selection(snap, rule, P, run, *, universe=None, day=None):
    P = int(P)
    task_day = int(day or P)
    codes = universe or snap.codes()
    rows_out = {}
    for code in codes:
        rows = snap.daily_asof(code, P)
        if not rows:
            rows_out[code] = dict(
                code=code, decision=UNKNOWN, status=ABSTAIN,
                sample_kind='MISSING_DATA', reasons=['NO_DAILY_ROWS_ASOF_P'],
                conditions=[], factors={}, history_days=0)
            continue
        factors = compute_selection_factors(rows, rule['zone'])
        apply_formula_factors(factors, rule)
        ev = evaluate(rule['selection'], factors)
        if ev.verdict == TRUE:
            kind, status = 'POOLED', DECISION
            geo = zone_geometry(rows, rule['zone'])
            if not geo:
                rows_out[code] = dict(
                    code=code, decision=UNKNOWN, status=ABSTAIN,
                    sample_kind='MISSING_DATA',
                    reasons=['ZONE_GEOMETRY_UNAVAILABLE'],
                    conditions=ev.leaves,
                    factors={k: factors.get(k) for k in SELECTION_FACTORS},
                    history_days=len(rows))
                continue
            rows_out[code] = dict(
                code=code, decision=TRUE, status=status, sample_kind=kind,
                reasons=[], conditions=ev.leaves,
                factors={k: factors.get(k) for k in SELECTION_FACTORS},
                history_days=len(rows), zone=geo)
        elif ev.verdict == FALSE:
            rows_out[code] = dict(
                code=code, decision=FALSE, status=DECISION,
                sample_kind='FILTERED_OUT', reasons=ev.reasons,
                conditions=ev.leaves,
                factors={k: factors.get(k) for k in SELECTION_FACTORS},
                history_days=len(rows))
        else:
            rows_out[code] = dict(
                code=code, decision=UNKNOWN, status=ABSTAIN,
                sample_kind='MISSING_DATA', reasons=ev.reasons,
                conditions=ev.leaves,
                factors={k: factors.get(k) for k in SELECTION_FACTORS},
                history_days=len(rows))
    accepted = sorted(c for c, v in rows_out.items() if v['decision'] == TRUE)
    payload = dict(
        contract='RULES_LAB_SELECTION_V1', day=P,
        rule_ref='%s@%s' % (rule['rule_id'], rule['version']),
        rule_sha256=rule['sha256'], snapshot=snap.snapshot_ref(),
        factor_catalog=factor_catalog(),
        universe_count=len(codes), accepted_count=len(accepted),
        decision_counts=dict(
            TRUE=sum(v['decision'] == TRUE for v in rows_out.values()),
            FALSE=sum(v['decision'] == FALSE for v in rows_out.values()),
            UNKNOWN=sum(v['decision'] == UNKNOWN for v in rows_out.values())),
        accepted=accepted, rows=rows_out)
    payload['selection_sha256'] = canonical_sha(payload)
    run.write('out', 'selection_%s.json' % P, value=payload)
    run.journal(task_day, 'SELECTION', dict(P=P, accepted=len(accepted),
                                            universe=len(codes)))
    return payload


def selection_diff(before, after):
    """阈值可编辑验收：结果与未命中原因是否真的发生变化。"""
    b = {c: v for c, v in before['rows'].items()}
    a = {c: v for c, v in after['rows'].items()}
    changed = []
    for code in sorted(set(b) | set(a)):
        vb, va = b.get(code), a.get(code)
        if (vb or {}).get('decision') != (va or {}).get('decision') or \
                (vb or {}).get('reasons') != (va or {}).get('reasons'):
            changed.append(dict(code=code,
                                before=(vb or {}).get('decision'),
                                after=(va or {}).get('decision'),
                                before_reasons=(vb or {}).get('reasons'),
                                after_reasons=(va or {}).get('reasons')))
    return dict(contract='RULES_LAB_SELECTION_DIFF_V1',
                before_accepted=before['accepted_count'],
                after_accepted=after['accepted_count'],
                changed_rows=len(changed), examples=changed[:20])
