"""盘中监控：触位事件、模拟回执、指标补充、中断恢复与去重（v1.1）。

v1.1 修复：
  1. 恢复以「回执账本」为准，先补发首条 TOUCH，INDICATOR_SUPPLEMENT 不得抢先；
     SENDING 不再当作不确定投递而放弃送达（仅适用于本地模拟收件箱）。
  2. 成交限制改在「模拟成交时点」（下一根已完成 bar）判定；NO_FILL 不保留成交价。
  3. 指标按字段给出可用率与缺失原因，并提供历史 K 线到齐后的补算诊断。

中断注入点仅用于验收：AFTER_EVENT_PERSIST / AFTER_RECEIPT_WRITE /
AFTER_STATUS_COMMIT，默认关闭，不影响正常回放。
"""
from __future__ import annotations

import hashlib

from .contract import FALSE, TRUE, UNKNOWN
from .factors import (TRIGGER_FACTORS, apply_formula_factors,
                      compute_trigger_factors)
from .interpreter import evaluate
from .paths import canonical_bytes

MAX_QUOTE_AGE_SECONDS = 300
INDICATOR_BARS = 3                 # 3 根 5 分钟 = 15 分钟
TOUCH = 'TOUCH'
INDICATOR_KIND = 'INDICATOR_SUPPLEMENT'
CRASH_POINTS = ('AFTER_EVENT_PERSIST', 'AFTER_RECEIPT_WRITE',
                'AFTER_STATUS_COMMIT')
INDICATOR_REQUIRED_BARS = dict(rsi6=7, rsi15=16, ma5=5, macd_dif=27)


class InjectedCrash(RuntimeError):
    """验收专用：在指定提交点注入进程中断。"""

    def __init__(self, point, event_id):
        super().__init__('INJECTED_CRASH:%s:%s' % (point, event_id))
        self.point = point
        self.event_id = event_id


def _event_id(day, code, ref, lower, upper):
    return hashlib.sha256(canonical_bytes(
        [int(day), code, ref, round(lower, 6), round(upper, 6)])).hexdigest()[:24]


def _daily_field(snap, code, field):
    rows = snap.daily.get(code) or []
    return rows[-1].get(field) if rows else None


def _rsi(closes, n=6):
    if len(closes) < n + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[-(n + 1):-1], closes[-n:]):
        diff = b - a
        gains.append(max(diff, 0.0))
        losses.append(max(-diff, 0.0))
    avg_gain = sum(gains) / n
    avg_loss = sum(losses) / n
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


def _limit_state(bar, pre_close):
    close, high, low = bar.get('close'), bar.get('high'), bar.get('low')
    if high is not None and low is not None and high == low:
        return 'SUSPENDED_OR_ONE_PRICE_BAR'
    if pre_close and close:
        pct = close / pre_close - 1.0
        if pct >= 0.098:
            return 'NEAR_LIMIT_UP'
        if pct <= -0.098:
            return 'NEAR_LIMIT_DOWN'
    return None


def build_indicators(closes, *, period_minutes=15):
    """按字段给出值、可用率与缺失原因；缺值不伪造、不 NaN→0。

    口径统一：rsi/macd 直接复用 factors 模块（触发判断同一实现），
    保证「提醒里显示的指标」与「触发时判定的指标」同名同值。
    """
    from .factors import _macd as _f_macd, _rsi as _f_rsi
    n = len(closes)
    macd = _f_macd(closes)
    values = dict(
        rsi6=_f_rsi(closes, 6),
        rsi15=_f_rsi(closes, 15),
        ma5=round(sum(closes[-5:]) / 5, 6) if n >= 5 else None,
        macd_dif=(macd[0] if macd else None),
    )
    fields = {}
    for name, value in values.items():
        required = INDICATOR_REQUIRED_BARS[name]
        ok = value is not None
        fields[name] = dict(
            value=value, available=ok, required_bars=required,
            available_bars=n,
            reason='OK' if ok else 'INSUFFICIENT_COMPLETED_5M_BARS',
            missing_bars=0 if ok else max(0, required - n))
    out = dict(values)
    out.update(
        field_coverage=fields,
        macd_status=('OK' if fields['macd_dif']['available']
                     else 'INSUFFICIENT_BARS_FOR_MACD'),
        bar_count=n, period_minutes=period_minutes,
        source='COMPLETED_5M_BARS_ONLY',
        coverage_note=('字段级可用率按「已完成 5 分钟 bar 数」判定；'
                       '缺失保留 None 与原因，不用 0 填充。'))
    return out


def _history_closes(snap, day, code):
    """截至 D 日收盘的跨交易日 5 分钟收盘序列，仅供补算诊断。"""
    out = []
    for d in snap.minute_days():
        di = int(d)
        if di > int(day):
            break
        bars = snap.minute_bars(di, code) or []
        out.extend(b['close'] for b in bars if b.get('close') is not None)
    return out


def backfill_indicator_fields(snap, event):
    """历史 K 线到齐后的补算诊断：不改事件、不新增回执、不进规则求值。

    验证「缺字段是否因历史 bar 不足」，列出可补算字段与所需/已得 bar 数。
    """
    day, code = int(event['day']), event['code']
    closes = _history_closes(snap, day, code)
    full = build_indicators(closes)
    before = (event.get('indicators') or {}).get('field_coverage', {})
    recovered, still_missing = {}, {}
    for name, meta in full['field_coverage'].items():
        was_ok = bool(before.get(name, {}).get('available'))
        info = dict(value=meta['value'], required_bars=meta['required_bars'],
                    bars_at_touch=(before.get(name) or {}).get('available_bars'),
                    bars_after_history=meta['available_bars'])
        if meta['available']:
            if not was_ok:
                recovered[name] = info
        else:
            still_missing[name] = dict(reason=meta['reason'],
                                       missing_bars=meta['missing_bars'])
    return dict(
        contract='RULES_LAB_INDICATOR_BACKFILL_V1',
        event_id=event['event_id'], day=day, code=code,
        usage='DIAGNOSTIC_ONLY_NOT_IN_SIGNALS_NOT_RESENT',
        bars_at_touch=(before.get('rsi6') or {}).get('available_bars'),
        history_bars_available=len(closes),
        recovered_fields=recovered, still_missing=still_missing,
        backfillable=bool(recovered),
        note=('补算使用截至 D 日收盘的全部已完成 5 分钟 bar（含前序交易日），'
              '只证明字段可补算；不得回填进触发时点的因子值或样本外收益。'))


def _receipt(event, kind, *, body_extra=None, sent_at=None):
    rec = dict(
        contract='RULES_LAB_INBOX_RECEIPT_V1',
        delivery='SENT', kind=kind, event_id=event['event_id'],
        rule_ref=event['rule_ref'], day=event['day'], code=event['code'],
        title='%s %s %s %s' % (kind, event['code'], event['day'], event['rule_ref']),
        body=dict(event=event['event_id'], kind=kind,
                  price=event.get('bar_close'), zone=[event.get('zone_lower'),
                                                     event.get('zone_upper')],
                  extra=body_extra or {}),
        sent_at=sent_at or event.get('first_observed_at'),
        attempt_id=hashlib.sha256(canonical_bytes(
            [event['event_id'], kind, event.get('first_observed_at')])).hexdigest()[:16],
        address='LOCAL_SIMULATED_INBOX',
    )
    return rec


def reconcile_touch_delivery(events, inbox, *, fail_on=None):
    """恢复对账：以回执账本为准，首条 TOUCH 必须先于任何指标补充落账。

    * 回执已存在但状态未提交 → 幂等确认 SENT，不重发；
    * 回执不存在且状态 PENDING/SENDING → 补发一次（去重键保证唯一）；
    * 显式模拟发送失败 UNKNOWN → 保持终态，不重发、不发指标。
    """
    fail_on = set(fail_on or ())
    keys = inbox.keys()
    actions = []
    for eid, event in events.events().items():
        status = event.get('delivery_status')
        if (eid, TOUCH) in keys:
            if status != 'SENT':
                event['delivery_status'] = 'SENT'
                event['sent_at'] = event.get('sent_at') or \
                    str(event.get('first_observed_at'))
                event['recovery_note'] = 'RECEIPT_LEDGER_CONFIRMED'
                events.put(event)
                actions.append(dict(event_id=eid, action='LEDGER_CONFIRMED',
                                    delivery='SENT'))
            continue
        if status not in ('PENDING', 'SENDING') or eid in fail_on:
            continue
        event['delivery_status'] = 'SENDING'
        events.put(event)
        rec = inbox.send(_receipt(event, TOUCH))
        event['delivery_status'] = ('SENT' if rec['delivery'] in
                                    ('SENT', 'DUPLICATE_SKIPPED') else 'UNKNOWN')
        event['sent_at'] = str(event.get('first_observed_at'))
        event['recovery_note'] = 'RESUMED_FIRST_TOUCH'
        events.put(event)
        actions.append(dict(event_id=eid, action='FIRST_TOUCH_DELIVERED',
                            delivery=rec['delivery']))
    return actions


def _field_coverage_summary(events):
    """验收口径：按字段统计可用率与缺失原因（分母=已落盘指标载荷事件）。"""
    names = sorted(INDICATOR_REQUIRED_BARS)
    out = {name: dict(events=0, available=0, missing=0, reasons={},
                      missing_bars_distribution={})
           for name in names}
    for event in events.values():
        cov = (event.get('indicators') or {}).get('field_coverage')
        if not cov:
            continue
        for name in names:
            meta = cov.get(name)
            if not meta:
                continue
            slot = out[name]
            slot['events'] += 1
            if meta['available']:
                slot['available'] += 1
            else:
                slot['missing'] += 1
                reason = meta['reason']
                slot['reasons'][reason] = slot['reasons'].get(reason, 0) + 1
                key = str(meta['missing_bars'])
                slot['missing_bars_distribution'][key] = \
                    slot['missing_bars_distribution'].get(key, 0) + 1
    for name, slot in out.items():
        slot['coverage'] = (slot['available'] / slot['events']
                            if slot['events'] else None)
        slot['required_bars'] = INDICATOR_REQUIRED_BARS[name]
    return dict(contract='RULES_LAB_INDICATOR_FIELD_COVERAGE_V1',
                denominator='PERSISTED_TOUCH_EVENTS_WITH_INDICATOR_PAYLOAD',
                fields=out,
                note=('分母为已写出指标载荷的触位事件；'
                      'WAITING（缺 15 分钟 bar）事件无载荷，单列 indicator_waiting。'))


def run_monitor(snap, rule, freeze, run, inbox, events, *, restart=False,
                max_age_seconds=MAX_QUOTE_AGE_SECONDS, fail_on=None, tag='',
                crash_points=None):
    day = int(freeze['day'])
    ref = freeze['rule_ref']
    fail_on = set(fail_on or ())
    crash_points = set(crash_points or ())
    unknown_points = crash_points - set(CRASH_POINTS)
    if unknown_points:
        raise ValueError('UNKNOWN_CRASH_POINTS:' + ','.join(sorted(unknown_points)))
    recovery = events.recover() if restart else None

    # (1) 恢复对账必须先于任何回执，保证首条触位先送达。
    resumed = reconcile_touch_delivery(events, inbox, fail_on=fail_on)
    crash_armed = bool(crash_points)
    touch_keys = set(inbox.keys())

    by_code = {c['code']: dict(c, float_mv=_daily_field(snap, c['code'], 'float_mv'))
               for c in freeze['candidates']}
    streams, missing_minute = [], []
    hist_days = sorted(int(d) for d in snap.minutes.keys() if int(d) < int(day))
    hist_minutes = {}
    for code in by_code:
        acc = []
        for d in hist_days[-10:]:
            for b in snap.minute_bars(d, code) or []:
                if b.get('close') is not None:
                    acc.append((d, int(b['ts']), b.get('close'), b.get('volume')))
        hist_minutes[code] = acc
    for code, frozen in by_code.items():
        bars = snap.minute_bars(day, code)
        if bars is None:
            missing_minute.append(dict(code=code, reason='NO_MINUTE_FILE_FOR_DAY'))
            continue
        for i, bar in enumerate(bars):
            streams.append((int(bar['ts']), code, bar, i))
    streams.sort(key=lambda x: (x[0], x[1]))

    cursor = {code: dict(prev_bar=None, volumes=[], bar_count=0,
                         after_touch=[], bars=[], amounts=[], amount=0.0)
              for code in by_code}
    samples, unknown_examples, stale_examples = [], [], []
    counts = dict(bars=len(streams), true=0, false=0, unknown=0,
                  stale=0, missing_minute=len(missing_minute),
                  touch_events=0, no_fill=0, indicator_done=0,
                  indicator_waiting=0, duplicate_trigger_bars=0,
                  resumed_first_touch=len(resumed))

    for ts, code, bar, idx in streams:
        state = cursor[code]
        frozen = by_code[code]
        ctx = dict(day=day, asof_ts=ts, max_age_seconds=max_age_seconds,
                   zone_lower=frozen['zone_lower'], zone_upper=frozen['zone_upper'],
                   atr14=frozen['atr14'], pre_close=frozen['pre_close_D'],
                   tolerance_atr=frozen['tolerance_atr'],
                   prev_bar=state['prev_bar'], completed_volumes=state['volumes'],
                   bar_count=state['bar_count'],
                   today_bars=state['bars'] + [(int(ts), bar.get('close'), bar.get('volume'))],
                   hist_minutes=hist_minutes.get(code, []),
                   vol_ratio_lookback_days=10,
                   cum_amount_so_far=state['amount'] + (bar.get('amount') or 0.0),
                   float_mv=frozen.get('float_mv'))
        factors = compute_trigger_factors(bar, ctx)
        apply_formula_factors(factors, rule)
        state['prev_bar'] = bar
        state['volumes'].append(bar.get('volume') or 0)
        state['bars'].append((int(ts), bar.get('close'), bar.get('volume')))
        state['amount'] += bar.get('amount') or 0.0
        state['bar_count'] += 1
        if factors.get('quote_fresh') == 0:
            counts['stale'] += 1
            if len(stale_examples) < 8:
                stale_examples.append(dict(code=code, ts=ts,
                                           age=factors.get('bar_age_seconds')))
            continue
        ev = evaluate(rule['trigger'], factors)
        if ev.verdict == UNKNOWN:
            counts['unknown'] += 1
            if len(unknown_examples) < 8:
                unknown_examples.append(dict(code=code, ts=ts, reasons=ev.reasons))
            continue
        if ev.verdict == FALSE:
            counts['false'] += 1
            continue
        counts['true'] += 1
        eid = _event_id(day, code, ref, frozen['zone_lower'], frozen['zone_upper'])
        if events.get(eid):
            counts['duplicate_trigger_bars'] += 1
            continue

        # (2) 成交能力在「模拟成交时点」（下一根已完成 bar）判定。
        trigger_limit_state = _limit_state(bar, frozen['pre_close_D'])
        all_bars = snap.minute_bars(day, code)
        next_bar = all_bars[idx + 1] if idx + 1 < len(all_bars) else None
        fill_limit_state = _limit_state(next_bar, frozen['pre_close_D']) \
            if next_bar else 'NO_NEXT_COMPLETED_BAR'
        exec_cfg = rule['execution']
        slippage = float(exec_cfg['slippage_rate'])
        fee = float(exec_cfg['fee_rate'])
        can_fill = (next_bar is not None and fill_limit_state is None
                    and next_bar.get('close') is not None)
        fill_price = round(next_bar['close'] * (1 + slippage), 6) \
            if can_fill else None
        event = dict(
            contract='RULES_LAB_TOUCH_EVENT_V1', event_id=eid, day=day, code=code,
            rule_ref=ref, rule_sha256=rule['sha256'],
            scan_started_at='%s093000' % day,
            quote_attempt_at=str(ts), quote_received_at=str(ts),
            first_observed_at=str(ts), event_recorded_at=str(ts),
            bar=bar, bar_index=idx,
            bar_close=bar.get('close'), zone_lower=frozen['zone_lower'],
            zone_upper=frozen['zone_upper'], zone_width_atr=frozen['zone_width_atr'],
            atr14=frozen['atr14'], pre_close_D=frozen['pre_close_D'],
            factor_values={k: factors.get(k) for k in TRIGGER_FACTORS},
            aggregate_diagnostic=factors.get('__agg__') or {},
            computed_cutoff=str(ts),
            conditions=ev.leaves, reasons=ev.reasons,
            limit_state_at_trigger=trigger_limit_state,
            limit_state_at_fill=fill_limit_state,
            fill=dict(mode=exec_cfg['fill'], next_bar_ts=(next_bar or {}).get('ts'),
                      fill_price=fill_price, fee_rate=fee, slippage_rate=slippage,
                      t_plus_1=bool(exec_cfg.get('t_plus_1')),
                      status='FILLED' if can_fill else 'NO_FILL',
                      fill_basis='NEXT_COMPLETED_BAR_LIMIT_CHECK',
                      no_fill_reason=None if can_fill else fill_limit_state),
            delivery_status='PENDING', delivery_queued=False,
            indicator_status='WAITING', indicator_queued=False,
            indicator_cutoff=str(ts),
        )
        if not can_fill:
            counts['no_fill'] += 1
        events.put(event)
        counts['touch_events'] += 1
        if crash_armed and 'AFTER_EVENT_PERSIST' in crash_points:
            raise InjectedCrash('AFTER_EVENT_PERSIST', eid)

        event['delivery_status'] = 'SENDING'
        events.put(event)
        receipt = inbox.send(_receipt(event, TOUCH))
        touch_keys.add((eid, TOUCH))
        if crash_armed and 'AFTER_RECEIPT_WRITE' in crash_points:
            raise InjectedCrash('AFTER_RECEIPT_WRITE', eid)
        event['delivery_status'] = 'UNKNOWN' if \
            (eid in fail_on or receipt['delivery'] == 'UNKNOWN') else 'SENT'
        event['sent_at'] = str(ts)
        events.put(event)
        if crash_armed and 'AFTER_STATUS_COMMIT' in crash_points:
            raise InjectedCrash('AFTER_STATUS_COMMIT', eid)

        samples.append(dict(code=code, event_id=eid, bar_ts=ts,
                            kind='TRIGGERED',
                            fill_status=event['fill']['status'],
                            limit_state=trigger_limit_state,
                            delivery_status=event['delivery_status']))

    # 指标补充：必须等首条 TOUCH 已落账，且 15 分钟窗口 bar 到齐。
    indicator_receipts = []
    for eid, event in events.events().items():
        if event.get('indicator_status') != 'WAITING':
            continue
        if (eid, TOUCH) not in touch_keys:
            event['indicator_reason'] = 'TOUCH_NOT_DELIVERED'
            counts['indicator_waiting'] += 1
            events.put(event)
            continue
        code, idx = event['code'], event['bar_index']
        bars = snap.minute_bars(day, code) or []
        later = bars[idx + 1:idx + 1 + INDICATOR_BARS]
        if len(later) < INDICATOR_BARS:
            event['indicator_status'] = 'WAITING'
            event['indicator_reason'] = 'MISSING_15M_COMPLETED_BARS'
            counts['indicator_waiting'] += 1
            events.put(event)
            continue
        closes = [b['close'] for b in bars[:idx + 1 + INDICATOR_BARS]
                  if b.get('close') is not None]
        indicators = build_indicators(closes)
        attach = (rule.get('notification') or {}).get('attach_fields') or []
        if attach:
            # 「随提醒显示」的指标：按触发时点的因子值附在提醒里，不参与判断
            indicators = dict(indicators)
            indicators['attach_fields'] = {
                f: (event.get('factor_values') or {}).get(f) for f in attach}
        rec = inbox.send(_receipt(
            event, INDICATOR_KIND, body_extra=indicators,
            sent_at=str(later[-1]['ts'])))
        event['indicator_status'] = 'DONE' if rec['delivery'] in (
            'SENT', 'DUPLICATE_SKIPPED') else 'UNKNOWN'
        event['indicator_at'] = str(later[-1]['ts'])
        event['indicators'] = indicators
        events.put(event)
        counts['indicator_done'] += 1
        indicator_receipts.append(dict(event_id=eid, code=code,
                                       delivery=rec['delivery'],
                                       indicators=indicators))

    coverage = _field_coverage_summary(events.events())
    backfill_probes = []
    for event in events.events().values():
        cov = (event.get('indicators') or {}).get('field_coverage') or {}
        if any(not m.get('available') for m in cov.values()):
            backfill_probes.append(backfill_indicator_fields(snap, event))
            if len(backfill_probes) >= 3:
                break

    summary = dict(
        contract='RULES_LAB_MONITOR_V1_1', day=day, rule_ref=ref,
        snapshot=snap.snapshot_ref(), candidate_count=len(by_code),
        counts=counts, recovery=recovery, resent=resumed,
        resumed_first_touch=resumed,
        samples=samples, missing_minute=missing_minute,
        unknown_examples=unknown_examples, stale_examples=stale_examples,
        indicator_receipts=indicator_receipts,
        indicator_field_coverage=coverage,
        indicator_backfill_probes=backfill_probes,
        inbox_counts=inbox.counts(),
        note=('恢复以回执账本为准，首条 TOUCH 先于 INDICATOR_SUPPLEMENT；'
              '成交限制在下一根已完成 bar 判定；NO_FILL 不保留成交价；'
              '指标按字段记录可用率与缺失原因；补算仅为诊断。'),
    )
    run.write('out', 'monitor_%s%s.json' % (day, tag), value=summary)
    if not tag:
        run.journal(day, 'MONITOR', dict(touches=counts['touch_events']))
        run.journal(day, 'INDICATOR', dict(done=counts['indicator_done'],
                                           waiting=counts['indicator_waiting']))
    return summary
