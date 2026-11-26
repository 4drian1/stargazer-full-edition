"""唯一的条件树解释器：盘后选股、盘中监控、历史回放、表单预览共用。

三值语义（缺失绝不静默当 0）：
  TRUE / FALSE / UNKNOWN(待核验)
  AND: 有 FALSE 即 FALSE（短路）；否则有 UNKNOWN 即 UNKNOWN；否则 TRUE
  OR : 有 TRUE 即 TRUE（短路）；否则有 UNKNOWN 即 UNKNOWN；否则 FALSE
  NOT: UNKNOWN 保持 UNKNOWN
"""
from __future__ import annotations

from .contract import (FALSE, TRUE, UNKNOWN, FIELD_WHITELIST, OPERATORS,
                       LOGIC_OPS)


class InterpreterError(ValueError):
    pass


class Evaluation:
    def __init__(self, verdict, leaves, reasons, path='root'):
        self.verdict = verdict
        self.leaves = leaves
        self.reasons = reasons
        self.path = path

    @property
    def decided(self):
        return self.verdict != UNKNOWN

    def as_dict(self):
        return dict(path=self.path, verdict=self.verdict,
                    leaves=self.leaves, reasons=self.reasons)

    def __repr__(self):
        return '<Evaluation %s reasons=%d>' % (self.verdict, len(self.reasons))


def _leaf(node, env, path, explain_all):
    field = node['field']
    meta = FIELD_WHITELIST[field]
    actual = env.get(field)
    entry = dict(path=path, field=field, op=node['op'], expected=node.get('value'),
                 actual=actual, unit=meta['unit'], window=meta['window'],
                 granularity=meta['granularity'], available_at=meta['available_at'],
                 result=UNKNOWN, reason='')
    if actual is None:
        entry['reason'] = 'MISSING:%s (%s/%s)' % (field, meta['unit'], meta['window'])
        return UNKNOWN, entry
    op, value = node['op'], node.get('value')
    try:
        if op == '>=':
            ok = actual >= value
        elif op == '>':
            ok = actual > value
        elif op == '<=':
            ok = actual <= value
        elif op == '<':
            ok = actual < value
        elif op == '==':
            ok = actual == value
        elif op == '!=':
            ok = actual != value
        elif op == 'between':
            ok = value[0] <= actual <= value[1]
        elif op in ('cross_up', 'cross_down'):
            prev = env.get(field + '__prev')
            if prev is None:
                entry['reason'] = 'MISSING_PREVIOUS:%s' % field
                return UNKNOWN, entry
            ok = (prev < value <= actual) if op == 'cross_up' \
                else (prev > value >= actual)
        else:
            raise InterpreterError('UNSUPPORTED_OP:' + str(op))
    except (TypeError, ValueError, IndexError, KeyError) as exc:
        entry['reason'] = 'TYPE_MISMATCH:%s:%s' % (field, type(exc).__name__)
        return UNKNOWN, entry
    entry['result'] = TRUE if ok else FALSE
    if not ok:
        entry['reason'] = 'NOT_MET:%s=%s %s %s' % (field, actual, op, value)
    return entry['result'], entry


def evaluate(node, env, *, path='root', short_circuit=True, explain_all=False):
    """返回 Evaluation；explain_all=True 时不短路，逐项给全量诊断（表单用）。"""
    if not isinstance(node, dict) or 'op' not in node:
        raise InterpreterError('BAD_NODE')
    op = node['op']
    if op not in LOGIC_OPS:
        verdict, leaf = _leaf(node, env, path, explain_all)
        return Evaluation(verdict, [leaf],
                          [leaf['reason']] if leaf['reason'] else [], path)
    children = node.get('children') or []
    leaves, reasons, results = [], [], []
    skipped_from = None
    for i, child in enumerate(children):
        if skipped_from is not None:
            leaves.append(dict(path='%s.%d' % (path, i),
                               field=child.get('field'), op=child.get('op'),
                               result='SKIPPED_BY_SHORT_CIRCUIT',
                               reason='SHORT_CIRCUIT', actual=None, expected=None,
                               unit='', window='', granularity='', available_at=''))
            continue
        sub = evaluate(child, env, path='%s.%d' % (path, i),
                       short_circuit=short_circuit, explain_all=explain_all)
        leaves.extend(sub.leaves)
        reasons.extend(sub.reasons)
        results.append(sub.verdict)
        if short_circuit and not explain_all:
            if op == 'AND' and sub.verdict == FALSE:
                skipped_from = i
            elif op == 'OR' and sub.verdict == TRUE:
                skipped_from = i
            elif op == 'NOT':
                skipped_from = i
    if op == 'NOT':
        verdict = {TRUE: FALSE, FALSE: TRUE, UNKNOWN: UNKNOWN}[results[0]] \
            if results else UNKNOWN
        if verdict == UNKNOWN:
            reasons.append('NOT_OF_UNKNOWN')
    elif op == 'AND':
        if FALSE in results:
            verdict = FALSE
        elif UNKNOWN in results:
            verdict = UNKNOWN
        else:
            verdict = TRUE
    elif op == 'OR':
        if TRUE in results:
            verdict = TRUE
        elif UNKNOWN in results:
            verdict = UNKNOWN
        else:
            verdict = FALSE
    else:
        raise InterpreterError('BAD_LOGIC_OP')
    if verdict == UNKNOWN and not reasons:
        reasons.append('UNKNOWN_INPUT_AT:' + path)
    for leaf in leaves:
        if leaf.get('result') == UNKNOWN and leaf.get('reason'):
            if leaf['reason'] not in reasons:
                reasons.append(leaf['reason'])
    return Evaluation(verdict, leaves, reasons, path)


def preview(rule, env, section='selection'):
    """表单逐项预览：当前值 / 结果 / 未命中原因 / 待核验原因。"""
    ev = evaluate(rule[section], env, short_circuit=False, explain_all=True)
    return dict(contract='RULES_LAB_PREVIEW_V1', section=section,
                verdict=ev.verdict, decided=ev.decided,
                conditions=ev.leaves, reasons=ev.reasons)
