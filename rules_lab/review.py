"""复盘样本账本：入池未触发 / 触发 / 缺数据 / 过滤掉 / 未成交全部保留。

标签口径：
  * 主收益 = D+1..D+h 的 pre_close 链式复利（除权后昨收/前收），
    天然消化除权除息，不把 raw 价格在除权日的假跌当收益；
  * 另存 raw 收盘价比作为「信号之后价格表现」对照，并标记 EX_RIGHTS_IN_WINDOW；
  * 成交模拟收益区分：下一根已完成 bar 成交 + 滑点 + 双边费用 + T+1；
  * 基准 = 快照 universe 等权同窗收益（非全市场，明确标注）。
"""
from __future__ import annotations

import json

from .paths import atomic_write_bytes
from .contract import SAMPLE_KINDS

HORIZONS = (1, 3, 5)


def _chain_return(snap, code, days):
    value = 1.0
    for d in days:
        row = snap.daily_row(code, d)
        if not row or not row.get('close') or not row.get('pre_close'):
            return None
        value *= row['close'] / row['pre_close']
    return value - 1.0


def _raw_price_ratio(snap, code, day, days):
    base = snap.daily_row(code, day)
    end = snap.daily_row(code, days[-1])
    if not base or not end or not base.get('close') or not end.get('close'):
        return None
    return end['close'] / base['close'] - 1.0


def _proxy_returns(snap, days, codes):
    out = {}
    for h in HORIZONS:
        window = days[:h]
        vals = [r for r in (_chain_return(snap, c, window) for c in codes)
                if r is not None]
        out[h] = sum(vals) / len(vals) if vals else None
    return out


def _mfe_mae(snap, code, days, entry):
    highs, lows = [], []
    for d in days:
        row = snap.daily_row(code, d)
        if row and row.get('high') is not None:
            highs.append(row['high'])
        if row and row.get('low') is not None:
            lows.append(row['low'])
    if not highs or not lows or not entry:
        return None, None
    return max(highs) / entry - 1.0, min(lows) / entry - 1.0


def build_review(snap, run, days, rule_ref):
    samples = []
    proxy_cache = {}
    for D in days:
        P = snap.prev_trading_day(D)
        sel = run.read('out', 'selection_%s.json' % P)
        freeze = run.read('freeze', '%s.json' % D)
        monitor = run.read('out', 'monitor_%s.json' % D)
        events = run.read('events', '%s.json' % D,
                          default=dict(events={}))['events']
        if sel is None or freeze is None or monitor is None:
            run.journal(D, 'REVIEW', dict(status='SKIPPED_MISSING_STAGE_DATA'))
            continue
        events_by_code = {e['code']: e for e in events.values()}
        minute_codes = set(snap.minute_day_codes(D))
        forward = snap.forward_trading_days(D, max(HORIZONS))
        if forward is None:
            forward = [d for d in snap.calendar if d > D]
        if D not in proxy_cache:
            proxy_cache[D] = _proxy_returns(snap, forward, snap.codes())
        for code, row in sorted(sel['rows'].items()):
            event = events_by_code.get(code)
            if row['decision'] == 'FALSE':
                kind, reasons = 'FILTERED_OUT', row['reasons']
            elif row['decision'] == 'UNKNOWN':
                kind, reasons = 'MISSING_DATA', row['reasons']
            elif code not in minute_codes:
                kind, reasons = 'MISSING_DATA', ['MISSING_MINUTE_BARS_FOR_DAY']
            elif event and event.get('fill', {}).get('status') == 'NO_FILL':
                kind, reasons = 'NO_FILL', [event.get('limit_state') or 'NO_NEXT_BAR']
            elif event:
                kind, reasons = 'TRIGGERED', []
            else:
                kind, reasons = 'POOLED_NOT_TRIGGERED', []
            labels = {}
            for h in HORIZONS:
                window = forward[:h]
                if len(window) < h or not window:
                    labels[h] = dict(outcome_status='INCOMPLETE_FORWARD_WINDOW',
                                     note='样本末端，持有窗口跨过快照末日')
                    continue
                chain = _chain_return(snap, code, window)
                proxy = proxy_cache[D].get(h)
                if chain is None or proxy is None:
                    labels[h] = dict(outcome_status='NO_FORWARD_DATA')
                    continue
                ex = snap.corporates(code, D + 1, window[-1])
                ex_rights = [x for x in ex if x[1] == 1]
                fill = (event or {}).get('fill') or {}
                fill_status = fill.get('status')
                # v1.1：只有真正 FILLED 才允许模拟成交收益；NO_FILL 的
                # 成交价、MFE/MAE、成交盈亏一律留空（信号后价格表现照常保留）。
                entry = fill.get('fill_price') if fill_status == 'FILLED' else None
                mfe, mae = _mfe_mae(snap, code, window, entry)
                end_row = snap.daily_row(code, window[-1])
                fee = fill.get('fee_rate', 0.0003)
                pnl = None
                if entry and end_row and end_row.get('close'):
                    pnl = end_row['close'] * (1 - fee) / entry - 1.0
                labels[h] = dict(
                    outcome_status='COMPLETE',
                    window_days=window,
                    chain_return=chain,
                    proxy_return=proxy,
                    excess_return=chain - proxy,
                    raw_price_ratio=_raw_price_ratio(snap, code, D, window),
                    raw_price_basis='UNADJUSTED_COMPARISON_ONLY',
                    ex_rights_in_window=[x[0] for x in ex_rights],
                    ex_rights_handling='PRE_CLOSE_CHAIN',
                    mfe=mfe, mae=mae,
                    tradable_pnl=pnl,
                    tradable_pnl_status=(
                        'FILLED' if fill_status == 'FILLED' and entry
                        else ('NO_FILL_EXCLUDED' if fill_status == 'NO_FILL'
                              else 'NO_FILLED_EVENT')),
                    signal_forward_return_kept=True,
                    t_plus_1=bool(fill.get('t_plus_1')),
                    fill_status=fill_status,
                    fill_price=entry,
                )
            frozen = next((c for c in freeze['candidates'] if c['code'] == code), None)
            samples.append(dict(
                contract='RULES_LAB_REVIEW_SAMPLE_V1',
                day=int(D), P=int(P), code=code, rule_ref=rule_ref,
                sample_kind=kind, triggered=bool(event),
                event_id=(event or {}).get('event_id'),
                fill_status=(event or {}).get('fill', {}).get('status'),
                delivery_status=(event or {}).get('delivery_status'),
                observation_ts='%s093000' % D,
                reasons=reasons,
                factor_snapshot=dict(
                    zone_width_atr=(frozen or {}).get('zone_width_atr'),
                    atr14=(frozen or {}).get('atr14'),
                    ret_60d=row['factors'].get('ret_60d'),
                    dist_60h=row['factors'].get('dist_60h'),
                    vol_ratio_5d=row['factors'].get('vol_ratio_5d'),
                    ma20_slope5=row['factors'].get('ma20_slope5'),
                    history_days=row.get('history_days')),
                labels=labels,
            ))
        run.journal(D, 'REVIEW', dict(samples=len(samples)))
    counts = {k: sum(s['sample_kind'] == k for s in samples) for k in SAMPLE_KINDS}
    label_coverage = {}
    for h in HORIZONS:
        triggered = [s for s in samples if s['triggered']]
        complete = [s for s in triggered if s['labels'][h]['outcome_status'] == 'COMPLETE']
        label_coverage[h] = dict(triggered=len(triggered), complete=len(complete),
                                 coverage=(len(complete) / len(triggered)
                                           if triggered else 0.0))
    no_fill_audit = dict(
        contract='RULES_LAB_NO_FILL_PNL_AUDIT_V1',
        no_fill_samples=counts['NO_FILL'],
        no_fill_labels=0, no_fill_with_tradable_pnl=0,
        no_fill_with_fill_price=0, no_fill_with_mfe_mae=0,
        no_fill_with_signal_forward_return=0,
        filled_labels_with_tradable_pnl=0,
        ok=True)
    for s in samples:
        for label in s['labels'].values():
            if label.get('outcome_status') != 'COMPLETE':
                continue
            status = label.get('tradable_pnl_status')
            if status == 'NO_FILL_EXCLUDED':
                no_fill_audit['no_fill_labels'] += 1
                if label.get('tradable_pnl') is not None:
                    no_fill_audit['no_fill_with_tradable_pnl'] += 1
                if label.get('fill_price') is not None:
                    no_fill_audit['no_fill_with_fill_price'] += 1
                if label.get('mfe') is not None or label.get('mae') is not None:
                    no_fill_audit['no_fill_with_mfe_mae'] += 1
                if label.get('excess_return') is not None:
                    no_fill_audit['no_fill_with_signal_forward_return'] += 1
            elif label.get('tradable_pnl') is not None:
                no_fill_audit['filled_labels_with_tradable_pnl'] += 1
    no_fill_audit['ok'] = (no_fill_audit['no_fill_with_tradable_pnl'] == 0
                           and no_fill_audit['no_fill_with_fill_price'] == 0
                           and no_fill_audit['no_fill_with_mfe_mae'] == 0)
    path = run.path('out', 'review_samples.jsonl')
    raw = ''.join(json.dumps(s, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':'), allow_nan=False) + '\n'
                  for s in samples)
    atomic_write_bytes(path, raw.encode('utf-8'))
    summary = dict(contract='RULES_LAB_REVIEW_SUMMARY_V1_1', rule_ref=rule_ref,
                   days=[int(d) for d in days], sample_count=len(samples),
                   sample_kind_counts=counts,
                   label_coverage=label_coverage,
                   no_fill_pnl_audit=no_fill_audit,
                   proxy_basis='SNAPSHOT_UNIVERSE_EQUAL_WEIGHT_PRE_CLOSE_CHAIN',
                   limitations=['快照 universe 等权代理，非全市场',
                                '样本末端持有窗口不完整，已标 INCOMPLETE_FORWARD_WINDOW',
                                'raw 价格比只作对照，除权日会假跌',
                                'NO_FILL 仅保留信号后价格表现，成交收益一律留空'])
    run.write('out', 'review_summary.json', value=summary)
    return summary, samples
