"""生产版归档只读引用：指数黄金地图 + 全球市场快照。

完整版不伪造实时信息：只读引用生产版已落盘的归档文件，
页面明确标注来源与生成时间；归档缺失时如实返回未就绪。
路径可用 STARGAZER_PROD_DATA 覆盖（默认指向本机生产版数据目录）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

PROD_DATA = Path(os.environ.get('STARGAZER_PROD_DATA') or
                 'D:/stargazer/stargazer_desktop/dist/data')

INDEX_MAPS = PROD_DATA / 'local_records' / 'risk_outlook' / 'overview_index_maps.json'
GLOBAL_QUOTES = PROD_DATA / 'isolated' / 'global_overview_snapshot.json'

SYMBOL_MAP = {'SSE': 'sse', 'SZSE': 'szse', 'CYB': 'cyb'}
SYMBOL_LABEL = {'sse': '沪指', 'szse': '深证成指', 'cyb': '创业板指'}


def index_maps():
    if not INDEX_MAPS.is_file():
        return dict(ok=False, error='INDEX_MAPS_MISSING:' + str(INDEX_MAPS))
    d = json.loads(INDEX_MAPS.read_text(encoding='utf-8'))
    rows = []
    for key, v in (d.get('maps') or {}).items():
        rows.append(dict(symbol=key, label=SYMBOL_LABEL.get(key, key),
                         asof=v.get('asof'), selection_day=v.get('selection_day'),
                         close=v.get('close'), session_low=v.get('session_low'),
                         upper=(v.get('nearby') or {}).get('upper'),
                         lower=(v.get('nearby') or {}).get('lower'),
                         following=(v.get('nearby') or {}).get('following'),
                         short_wave_levels=(v.get('short_wave_levels') or [])[:24]))
    return dict(ok=True, requested_day=d.get('requested_day'), maps=rows,
                source='PRODUCTION_INDEX_MAPS_READONLY', file=str(INDEX_MAPS),
                note='来源：生产版指数黄金地图归档（只读引用）；asof 之后行情可能已变化，仅作参考')


def global_quotes():
    if not GLOBAL_QUOTES.is_file():
        return dict(ok=False, error='GLOBAL_QUOTES_MISSING:' + str(GLOBAL_QUOTES))
    d = json.loads(GLOBAL_QUOTES.read_text(encoding='utf-8'))
    return dict(ok=True, generated_at=d.get('generated_at'),
                items=d.get('items') or [],
                source='PRODUCTION_GLOBAL_QUOTES_READONLY', file=str(GLOBAL_QUOTES),
                note='来源：生产版全球市场快照归档（只读引用，非实时）')
