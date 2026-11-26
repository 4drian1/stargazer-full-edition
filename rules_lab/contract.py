"""规则契约：字段白名单、算子白名单、单位/窗口/时点元数据、状态枚举。

字段白名单是「可编辑规则」的边界：表单与导入文件只能引用这里登记过的字段，
未登记字段、未登记算子、类型不符一律在发布前拒绝（fail-closed），
不靠 eval、不靠动态属性、不执行任意表达式。
"""
from __future__ import annotations

RULE_CONTRACT = 'STARGAZER_RULES_LAB_RULE_V1'

# ---------------------------------------------------------------- 状态枚举
TRUE, FALSE, UNKNOWN = 'TRUE', 'FALSE', 'UNKNOWN'

DECISION = 'DECIDED'          # 规则已判定
ABSTAIN = 'ABSTAIN'           # 安全门未过，拒绝判定
PENDING = 'PENDING'           # 待核验（输入缺失/不可判定）

DELIVERY_PENDING = 'PENDING'
DELIVERY_SENDING = 'SENDING'
DELIVERY_SENT = 'SENT'
DELIVERY_UNKNOWN = 'UNKNOWN'
DELIVERY_EXPIRED = 'EXPIRED'

SAMPLE_KINDS = (
    'POOLED_NOT_TRIGGERED', 'TRIGGERED', 'MISSING_DATA',
    'FILTERED_OUT', 'NO_FILL',
)

# ---------------------------------------------------------------- 算子白名单
OPERATORS = {
    '>=': dict(arity=2, dtype='number'),
    '>': dict(arity=2, dtype='number'),
    '<=': dict(arity=2, dtype='number'),
    '<': dict(arity=2, dtype='number'),
    '==': dict(arity=2, dtype='any'),
    '!=': dict(arity=2, dtype='any'),
    'between': dict(arity=2, dtype='number'),
    'cross_up': dict(arity=2, dtype='number', needs_window=True),
    'cross_down': dict(arity=2, dtype='number', needs_window=True),
}
LOGIC_OPS = ('AND', 'OR', 'NOT')


def _f(unit, window, granularity, available_at, dtype='number',
       ops=None, direction='HIGHER_IS_MORE', deps=(), note=''):
    return dict(unit=unit, window=window, granularity=granularity,
                available_at=available_at, dtype=dtype,
                ops=ops or ('>=', '>', '<=', '<', '==', '!='),
                direction=direction, dependencies=list(deps), note=note)


# ---------------------------------------------------------------- 字段白名单
FIELD_WHITELIST = {
    # --- 盘后 / 盘前可算（P 日收盘切片，日线粒度） ---
    'close': _f('元', 'P', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'open': _f('元', 'P', 'DAILY', 'P_CLOSE', deps=['daily.open']),
    'high': _f('元', 'P', 'DAILY', 'P_CLOSE', deps=['daily.high']),
    'low': _f('元', 'P', 'DAILY', 'P_CLOSE', deps=['daily.low']),
    'pre_close': _f('元', 'P-1', 'DAILY', 'P_CLOSE', deps=['daily.pre_close']),
    'volume': _f('股', 'P', 'DAILY', 'P_CLOSE', deps=['daily.volume']),
    'amount': _f('元', 'P', 'DAILY', 'P_CLOSE', deps=['daily.amount']),
    'pct_chg': _f('百分比', 'P', 'DAILY', 'P_CLOSE', deps=['daily.pct_chg']),
    'float_mv': _f('元', 'P', 'DAILY', 'P_CLOSE', deps=['daily.float_mv'],
                   note='当前快照字段，历史时点可用但非 PIT 权威'),
    'is_st': _f('0/1', 'P', 'DAILY', 'P_CLOSE', dtype='bool',
                ops=('==', '!='), deps=['daily.is_st'],
                note='仅 P 当日快照有值，历史 ST 状态不可用'),
    'history_days': _f('交易日', '全历史', 'DAILY', 'P_CLOSE',
                       deps=['daily.date']),
    'ret_60d': _f('比率', '60交易日', 'DAILY', 'P_CLOSE',
                  deps=['daily.close'], note='close/close[-60]-1'),
    'dist_60h': _f('比率', '60交易日', 'DAILY', 'P_CLOSE',
                   deps=['daily.high'], note='close/max(high,60)-1，通常<=0'),
    'atr14': _f('元', '14交易日', 'DAILY', 'P_CLOSE',
                deps=['daily.high', 'daily.low', 'daily.pre_close'],
                note='Wilder 未平滑的 14 日真实波幅均值'),
    'ma20': _f('元', '20交易日', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'ma20_slope5': _f('比率', '20+5交易日', 'DAILY', 'P_CLOSE',
                      deps=['daily.close'], note='ma20 / ma20[-5] - 1'),
    'close_above_ma20': _f('0/1', '20交易日', 'DAILY', 'P_CLOSE', dtype='bool',
                           ops=('==', '!='), deps=['daily.close']),
    'vol_ratio_5d': _f('比率', '5/20交易日', 'DAILY', 'P_CLOSE',
                       deps=['daily.volume'], note='5日均量/20日均量'),
    'zone_lower': _f('元', '派生', 'DAILY', 'P_CLOSE',
                     deps=['atr14', 'close', 'rule.zone'],
                     note='冻结观察区下沿'),
    'zone_upper': _f('元', '派生', 'DAILY', 'P_CLOSE',
                     deps=['atr14', 'close', 'rule.zone'],
                     note='冻结观察区上沿'),
    'zone_width_atr': _f('比率', '派生', 'DAILY', 'P_CLOSE',
                         deps=['zone_lower', 'zone_upper', 'atr14'],
                         note='(zone_upper-zone_lower)/atr14；样本外单因子'),
    'daily_turnover_proxy': _f('比率', 'P', 'DAILY', 'P_CLOSE',
                               deps=['daily.volume', 'daily.float_mv'],
                               note='近似换手，无历史流通股本表'),
    # --- 第一批扩展：日 K 选股（MA/RSI/MACD/近N日涨跌幅/距N日高点） ---
    'ma60': _f('元', '60交易日', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'rsi6': _f('RSI点', '6交易日', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'macd_dif': _f('元', '12/26/9', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'macd_dea': _f('元', '12/26/9', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'macd_hist': _f('元', '12/26/9', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'ret_5d': _f('比率', '5交易日', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'ret_20d': _f('比率', '20交易日', 'DAILY', 'P_CLOSE', deps=['daily.close']),
    'dist_20h': _f('比率', '20交易日', 'DAILY', 'P_CLOSE',
                   deps=['daily.high'], note='close/max(high,20)-1，通常<=0'),
    # --- 盘中触发（D 日已完成 5 分钟 bar 切片） ---
    'quote_fresh': _f('0/1', '当根bar', 'MINUTE_5M', 'D_BAR', dtype='bool',
                      ops=('==', '!='), deps=['bar.ts', 'asof_ts'],
                      note='bar 时间戳须属于 D 且新鲜度不超限'),
    'in_session': _f('0/1', '当根bar', 'MINUTE_5M', 'D_BAR', dtype='bool',
                     ops=('==', '!='), deps=['bar.ts']),
    'bar_close': _f('元', '当根bar', 'MINUTE_5M', 'D_BAR', deps=['bar.close']),
    'bar_low': _f('元', '当根bar', 'MINUTE_5M', 'D_BAR', deps=['bar.low']),
    'bar_high': _f('元', '当根bar', 'MINUTE_5M', 'D_BAR', deps=['bar.high']),
    'bar_volume': _f('股', '当根bar', 'MINUTE_5M', 'D_BAR', deps=['bar.volume']),
    'bar_age_seconds': _f('秒', '当根bar', 'MINUTE_5M', 'D_BAR',
                          deps=['bar.ts', 'asof_ts']),
    'decline_from_prev_close': _f('比率', 'D日内', 'MINUTE_5M', 'D_BAR',
                                  deps=['bar.close', 'daily.pre_close'],
                                  note='1 - bar_close/pre_close（原始价）'),
    'zone_distance_atr': _f('ATR倍数', 'D日内', 'MINUTE_5M', 'D_BAR',
                            deps=['bar.low', 'bar.close', 'zone_lower',
                                  'zone_upper', 'atr14'],
                            note='bar 与冻结观察区的距离/ATR，带内为 0'),
    'price_in_zone': _f('0/1', 'D日内', 'MINUTE_5M', 'D_BAR', dtype='bool',
                        ops=('==', '!='), deps=['bar.low', 'bar.close',
                                               'zone_lower', 'zone_upper']),
    'minute_volume_ratio': _f('比率', 'D日内', 'MINUTE_5M', 'D_BAR',
                              deps=['bar.volume'],
                              note='当根量/当日已完成bar均量；同刻历史不足，不用于首个因子'),
    'bar_count': _f('根', 'D日内', 'MINUTE_5M', 'D_BAR', deps=['bars']),
    # --- 第一批扩展：5/15/30 分钟指标（15/30 由本地 5 分钟线聚合，
    #     只用已收盘聚合 K 线；窗口不足返回待核验） ---
    'm5_rsi6': _f('RSI点', '6根5分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm5_ma5': _f('元', '5根5分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm5_ma20': _f('元', '20根5分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm5_macd_dif': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm5_macd_dea': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm5_macd_hist': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm15_rsi6': _f('RSI点', '6根15分钟', 'MINUTE_5M', 'D_BAR',
                   deps=['bars.close'], note='5分钟线聚合，仅完整聚合K线'),
    'm15_ma5': _f('元', '5根15分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm15_ma20': _f('元', '20根15分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm15_macd_dif': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm15_macd_dea': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm15_macd_hist': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm30_rsi6': _f('RSI点', '6根30分钟', 'MINUTE_5M', 'D_BAR',
                   deps=['bars.close'], note='5分钟线聚合，仅完整聚合K线'),
    'm30_ma5': _f('元', '5根30分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm30_ma20': _f('元', '20根30分钟', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm30_macd_dif': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm30_macd_dea': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    'm30_macd_hist': _f('元', '12/26/9', 'MINUTE_5M', 'D_BAR', deps=['bars.close']),
    # --- 量比口径二选一 ---
    'minute_volume_ratio': _f('比率', 'D日内', 'MINUTE_5M', 'D_BAR',
                              deps=['bar.volume'],
                              note='口径A：当根量 ÷ 今日已走完K线均量'),
    'same_time_vol_ratio': _f('比率', '历史同刻', 'MINUTE_5M', 'D_BAR',
                              deps=['history.minutes'],
                              note='口径B：今日同刻累计量 ÷ 历史同刻均量；'
                                   '需要多日分钟线，当前快照为待核验'),
    # --- 当日累计口径 ---
    'cum_turnover_proxy': _f('比率', 'D日内累计', 'MINUTE_5M', 'D_BAR',
                             deps=['bars.amount', 'daily.float_mv'],
                             note='当日累计成交额 ÷ 流通市值（近似换手）'),
    # --- 黄金观察区 ---
    'price_in_zone': _f('0/1', 'D日内', 'MINUTE_5M', 'D_BAR', dtype='bool',
                        ops=('==', '!='), deps=['bar.low', 'bar.close',
                                               'zone_lower', 'zone_upper']),
    'dist_to_zone_upper_pct': _f('百分比', 'D日内', 'MINUTE_5M', 'D_BAR',
                                 deps=['bar.close', 'zone_upper'],
                                 note='(收盘-上沿)/上沿；带内为负'),
    'dist_to_zone_lower_pct': _f('百分比', 'D日内', 'MINUTE_5M', 'D_BAR',
                                 deps=['bar.close', 'zone_lower'],
                                 note='(收盘-下沿)/下沿；带内为正'),
}

RULE_REQUIRED = ('contract', 'rule_id', 'version', 'title', 'status', 'scope',
                 'evaluation_time', 'factor_refs', 'selection', 'zone',
                 'trigger', 'events', 'notification', 'execution')
RULE_ALLOWED = set(RULE_REQUIRED) | {
    'sha256', 'created_at', 'published_at', 'notes', 'formula_factors'
}
NOTIFICATION_CHANNELS = ('local_inbox',)
UNIVERSES = ('SNAPSHOT_MINUTE_CODES',)
EVALUATION_TIMES = ('POST_CLOSE_P',)
ZONE_TYPES = ('ATR_BAND', 'GOLDEN_FIB')
FILL_MODES = ('NEXT_COMPLETED_BAR',)


class RuleContractError(ValueError):
    pass


def validate_condition(node, path='selection', *, depth=0):
    """发布前结构校验：字段/算子/类型/必填项全部白名单化。"""
    if depth > 8:
        raise RuleContractError(path + ':NESTING_TOO_DEEP')
    if not isinstance(node, dict):
        raise RuleContractError(path + ':NODE_NOT_OBJECT')
    op = node.get('op')
    if op in LOGIC_OPS:
        extra = set(node) - {'op', 'children'}
        if extra:
            raise RuleContractError(path + ':UNKNOWN_KEYS:' + ','.join(sorted(extra)))
        children = node.get('children') or []
        if not children or len(children) > 24:
            raise RuleContractError(path + ':CHILDREN_COUNT')
        if op == 'NOT' and len(children) != 1:
            raise RuleContractError(path + ':NOT_ARITY')
        for i, child in enumerate(children):
            validate_condition(child, path + '.%d' % i, depth=depth + 1)
        return
    if op not in OPERATORS:
        raise RuleContractError(path + ':UNKNOWN_OPERATOR:' + str(op))
    extra = set(node) - {'op', 'field', 'value', 'window'}
    if extra:
        raise RuleContractError(path + ':UNKNOWN_KEYS:' + ','.join(sorted(extra)))
    field = node.get('field')
    if field not in FIELD_WHITELIST:
        raise RuleContractError(path + ':UNKNOWN_FIELD:' + str(field))
    spec = FIELD_WHITELIST[field]
    allowed = set(spec['ops'])
    if spec['dtype'] == 'number':
        allowed |= {'between', 'cross_up', 'cross_down'}
    if op not in allowed:
        raise RuleContractError(path + ':OPERATOR_NOT_ALLOWED:' + op)
    value = node.get('value')
    if op == 'between':
        if not isinstance(value, list) or len(value) != 2 or \
                not all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
            raise RuleContractError(path + ':BETWEEN_VALUE')
    elif not isinstance(value, (int, float, bool)):
        raise RuleContractError(path + ':VALUE_NOT_SCALAR')
    if spec['dtype'] == 'number' and isinstance(value, bool):
        raise RuleContractError(path + ':BOOL_VALUE_FOR_NUMBER_FIELD')
    if op in ('cross_up', 'cross_down') and not isinstance(node.get('window'), int):
        raise RuleContractError(path + ':CROSS_NEEDS_WINDOW')


def validate_rule(rule):
    if not isinstance(rule, dict):
        raise RuleContractError('RULE_NOT_OBJECT')
    if rule.get('contract') != RULE_CONTRACT:
        raise RuleContractError('CONTRACT_MISMATCH')
    missing = set(RULE_REQUIRED) - set(rule)
    unknown = set(rule) - RULE_ALLOWED
    if missing:
        raise RuleContractError('MISSING_KEYS:' + ','.join(sorted(missing)))
    if unknown:
        raise RuleContractError('UNKNOWN_KEYS:' + ','.join(sorted(unknown)))
    if rule['status'] not in ('DRAFT', 'PUBLISHED'):
        raise RuleContractError('BAD_STATUS')
    if not isinstance(rule['rule_id'], str) or not rule['rule_id'].replace('_', '').isalnum():
        raise RuleContractError('BAD_RULE_ID')
    if not isinstance(rule['version'], str) or not rule['version'].startswith('v'):
        raise RuleContractError('BAD_VERSION')
    scope = rule['scope']
    if not isinstance(scope, dict) or scope.get('universe') not in UNIVERSES:
        raise RuleContractError('BAD_UNIVERSE')
    if rule['evaluation_time'] not in EVALUATION_TIMES:
        raise RuleContractError('BAD_EVALUATION_TIME')
    if not isinstance(rule['factor_refs'], list) or not rule['factor_refs']:
        raise RuleContractError('MISSING_FACTOR_REFS')
    validate_condition(rule['selection'], 'selection')
    validate_condition(rule['trigger'], 'trigger')
    zone = rule['zone']
    if not isinstance(zone, dict) or zone.get('type') not in ZONE_TYPES:
        raise RuleContractError('BAD_ZONE_TYPE')
    for k in (('k_lower', 'k_upper', 'tolerance_atr') if zone.get('type') == 'ATR_BAND'
              else ('tolerance_atr',)):
        if not isinstance(zone.get(k), (int, float)) or isinstance(zone.get(k), bool):
            raise RuleContractError('BAD_ZONE_PARAM:' + k)
    events = rule['events']
    if not isinstance(events, dict) or not isinstance(events.get('touch'), dict):
        raise RuleContractError('BAD_EVENTS')
    nt = rule['notification']
    if not isinstance(nt, dict) or nt.get('channel') not in NOTIFICATION_CHANNELS:
        raise RuleContractError('BAD_NOTIFICATION_CHANNEL')
    ex = rule['execution']
    if not isinstance(ex, dict) or ex.get('fill') not in FILL_MODES:
        raise RuleContractError('BAD_FILL_MODE')
    for k in ('fee_rate', 'slippage_rate'):
        if not isinstance(ex.get(k), (int, float)) or isinstance(ex.get(k), bool) or ex[k] < 0:
            raise RuleContractError('BAD_EXEC_PARAM:' + k)
    for spec in rule.get('formula_factors') or []:
        if not isinstance(spec, dict) or not spec.get('name') or not spec.get('expr'):
            raise RuleContractError('BAD_FORMULA_FACTOR')
    return True


def field_catalog():
    """给表单用的字段目录（含单位、窗口、粒度、时点、方向与依赖）。"""
    return {name: dict(spec) for name, spec in FIELD_WHITELIST.items()}
