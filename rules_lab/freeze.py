"""盘前冻结任务：固定规则版本、因子版本、观察区、参考收盘价与数据指纹。

参考收盘价取 D 日 event-adjusted ``pre_close``（盘前已知，除权后昨收/前收），
绝不使用 D 日的 open/high/low/close（那是未来数据）。
"""
from __future__ import annotations

from .factors import factor_catalog


def run_freeze(snap, rule, P, D, selection, run):
    P, D = int(P), int(D)
    ref = '%s@%s' % (rule['rule_id'], rule['version'])
    candidates, dropped = [], []
    for code in selection['accepted']:
        row = selection['rows'][code]
        drow = snap.daily_row(code, D)
        pre_close = drow.get('pre_close') if drow else None
        basis = 'EVENT_ADJUSTED_PRE_CLOSE_KNOWN_BEFORE_OPEN'
        if pre_close is None:
            # 草案模式：只有选股日数据时，D 日盘前参考价 = P 日收盘（盘前已知），
            # 照常冻结；D 日数据到齐后监控自然启用（缺分钟登记待核验）。
            prow = snap.daily_row(code, P)
            pre_close = prow.get('close') if prow else None
            basis = 'P_CLOSE_AS_PRE_OPEN_FALLBACK_D_DAILY_NOT_ARRIVED'
        if pre_close is None:
            dropped.append(dict(code=code, reason='NO_PRE_OPEN_REFERENCE_CLOSE'))
            continue
        zone = row['zone']
        candidates.append(dict(
            code=code, rule_ref=ref,
            factor_versions=dict(factor_catalog()),
            zone_lower=zone['zone_lower'], zone_upper=zone['zone_upper'],
            zone_width_atr=zone['zone_width_atr'], atr14=zone['atr14'],
            golden_ratio=zone.get('golden_ratio'), golden_node=zone.get('golden_node'),
            wave_low_day=zone.get('wave_low_day'), wave_high_day=zone.get('wave_high_day'),
            tolerance_atr=rule['zone']['tolerance_atr'],
            reference_close=zone['reference_close'],
            pre_close_D=pre_close,
            pre_close_fallback=(basis != 'EVENT_ADJUSTED_PRE_CLOSE_KNOWN_BEFORE_OPEN'),
            reference_close_basis=basis,
            source_date=P, available_at='%s092500' % D,
            board_source='boards@snapshot(single-date limitation)',
            history_days=row['history_days']))
    freeze = dict(
        contract='RULES_LAB_FREEZE_V1', day=D, observation_day=P,
        rule_ref=ref, rule_sha256=rule['sha256'],
        snapshot=snap.snapshot_ref(),
        selection_sha256=selection.get('selection_sha256'),
        data_fingerprint=snap.fingerprint,
        built_at='%s092500' % D,
        factor_count=len(factor_catalog()),
        candidate_count=len(candidates),
        candidates=candidates, dropped=dropped,
        ex_rights_basis=('D 日 pre_close 已含除权除息口径；标签另用 '
                         'pre_close 链式复利，避免 raw 价格在除权日假跌'),
        limitations=[
            '板块成分仅单一时点快照，历史成分不可证',
            '股票池来自快照 universe，存在成分前视与幸存者偏差',
            'is_st 历史不可用，未按历史 ST 过滤',
        ])
    run.write('freeze', '%s.json' % D, value=freeze)
    run.journal(D, 'FREEZE', dict(candidates=len(candidates),
                                  dropped=len(dropped)))
    return freeze
