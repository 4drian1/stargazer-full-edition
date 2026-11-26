"""生产版交易计划只读引用：当前生效计划（模型）+ 待确认草案。

来源：stargazer_desktop/dist/data/assistant/confirmed_*.json（生效计划）与
plan_*.json（草案），只读解析，不在完整版重新计算模型。
路径可用 STARGAZER_PROD_DATA 覆盖。
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

PROD_ASSISTANT = Path(os.environ.get('STARGAZER_PROD_DATA') or
                      'D:/stargazer/stargazer_desktop/dist/data') / 'assistant'

ENTRY_TEXT = {
    'POSITION_REQUIRED_REPAIR_MOMENTUM_ONE_CROSS_OVERSOLD20_ANY_TWO_WITH_RISK':
        '三选二：三重位置到达为必要；修复、动能、曾严重超卖任意两项成立（盘后选股计划规则，非强势股回调规则）',
}


def _names():
    try:
        from full.vipdoc_reader import stock_names
        return stock_names()
    except Exception:
        return {}


def load_production_plan():
    confs = sorted(glob.glob(str(PROD_ASSISTANT / 'confirmed_*.json')), key=os.path.getmtime)
    if not confs:
        return dict(ok=False, error='PROD_PLAN_MISSING:' + str(PROD_ASSISTANT))
    c = json.loads(Path(confs[-1]).read_text(encoding='utf-8'))
    plan = c.get('plan') or {}
    draft_id = c.get('draft_id') or plan.get('assistant_plan_id')
    names = _names()
    stocks = []
    for r in (plan.get('rows') or []):
        zone = r.get('zone') or {}
        entry = r.get('entry_rule') or {}
        cond = entry.get('condition') if isinstance(entry, dict) else None
        code = r.get('code')
        stocks.append(dict(
            code=code, name=names.get(code, ''),
            observe_only=not bool(zone.get('zone_lower')),
            zone_lower=zone.get('zone_lower'), zone_upper=zone.get('zone_upper'),
            entry_condition=ENTRY_TEXT.get(cond, cond or '未提供'),
            valid_until='%s150000' % plan.get('monitor_date') if plan.get('monitor_date') else None,
            source='MODEL', distance_atr=r.get('distance_atr'), atr_ref=r.get('atr_ref')))
    # 待确认草案：与生效计划同批的草案；若无未确认草案则显示最近一份
    draft = None
    df = PROD_ASSISTANT / ('plan_%s.json' % draft_id)
    if df.is_file():
        d = json.loads(df.read_text(encoding='utf-8'))
        draft = dict(id=draft_id, created_at=d.get('created_at'), origin=d.get('origin'),
                     selected_codes=d.get('selected_codes') or [],
                     stock_count=len(d.get('rows') or []), confirmed=True,
                     boards=[b.get('name') if isinstance(b, dict) else b for b in (d.get('boards') or [])])
    return dict(ok=True, source='PRODUCTION_PLAN_READONLY',
                formal=dict(plan_id=plan.get('assistant_plan_id'),
                            version=plan.get('schema') or 'v1',
                            data_date=plan.get('data_date'), monitor_date=plan.get('monitor_date'),
                            rule_version=plan.get('rule_version'), plan_ready=plan.get('plan_ready'),
                            model_status=plan.get('model_status'),
                            confirmed_at=c.get('confirmed_at'), stocks=stocks),
                draft=draft,
                note='来源：生产版交易计划归档（只读引用，模型计划不在完整版重算）')
