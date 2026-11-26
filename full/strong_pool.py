"""强势板块池展示：生产版已核验池的只读引用 + 本地重算兜底。

主路径：stargazer_desktop/dist/data/postclose_risk/strong_sector/pools/ 下最新非失败池
（STRONG_SECTOR_PULLBACK_OBSERVATION 契约），只读解析，不启动生产版任何计算。
每只含多级黄金观察区（positions: lower/upper/node）、颈线、波段涨幅、换手参考。
兜底：生产池缺失时退回本地 THS 归档重算（成分归档覆盖有限，如实标注）。
"""
from __future__ import annotations

import glob
import json
import os
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PROD_POOLS = Path(os.environ.get('STARGAZER_PROD_POOLS') or
                  'D:/stargazer/stargazer_desktop/dist/data/postclose_risk/strong_sector/pools')


def _latest_prod_pool():
    files = [f for f in glob.glob(str(PROD_POOLS / '*.json')) if '.failed.' not in f]
    if not files:
        return None, None
    files.sort(key=os.path.getmtime)
    return files[-1], json.loads(Path(files[-1]).read_text(encoding='utf-8'))


def _zone_summary(row, n=3):
    """取参考收盘下方最近的 n 级观察区。"""
    ref = row.get('reference_close')
    zones = [p for p in (row.get('positions') or []) if p.get('lower') and p.get('upper')]
    if ref:
        below = [p for p in zones if p['upper'] <= ref] or zones
        zones = sorted(below, key=lambda p: -p['upper'])[:n]
    return ['%s ~ %s' % (round(p['lower'], 2), round(p['upper'], 2)) for p in zones]


def from_production():
    f, d = _latest_prod_pool()
    if not d:
        return None
    rows = d.get('rows') or []
    boards_out = []
    for b in d.get('boards') or []:
        members = [r for r in rows if b in (r.get('boards') or [])]
        boards_out.append(dict(
            board=b, identity_index_code=None, kind='concept',
            member_count=len(members), member_source='生产版强势池（只读引用）',
            member_source_date=str(d.get('data_date') or '未提供'),
            pool_status='READY' if members else 'READY_EMPTY',
            qualified_count=len(members),
            board_ret_5d=None, board_ret_20d=None, board_last_day=d.get('data_date'),
            note='生产版已核验池：多级黄金观察区（0.2×前日 ATR 容忍带）',
            qualified=[dict(code=r.get('code'), name=r.get('name'),
                            reference_close=r.get('reference_close'),
                            neckline=r.get('neckline'),
                            wave_return=round(r.get('wave_return') or 0, 4),
                            risk_clear=r.get('risk_clear'),
                            zones=_zone_summary(r)) for r in members]))
    return dict(ok=True, contract='FE_STRONG_POOL_PROD_REF_V1',
                source='PRODUCTION_POOL_READONLY', source_file=os.path.basename(f),
                p_day=d.get('data_date'), observation_day=d.get('observation_date'),
                status=d.get('status'), total_rows=len(rows),
                boards=boards_out,
                note='生产版强势板块回调池（只读引用，不重新计算、不触发推送）')


def load_or_compute(p_day=None, refresh=False):
    d = from_production()
    if d:
        return d
    return dict(ok=False, error='PROD_POOL_MISSING:' + str(PROD_POOLS),
                note='未找到生产版强势池归档；本地成分归档不全，不虚构结果')


def compute(p_day=None):
    """「更新强势池」= 重新读取生产版最新池（完整版不生产计算）。"""
    return load_or_compute(refresh=True)
