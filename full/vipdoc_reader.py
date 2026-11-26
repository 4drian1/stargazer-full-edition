"""本地行情只读适配器：统一目录 vipdoc（STARGAZER_MARKET_DATA 可指向测试快照）。

只读，绝不写入。日线用 tdx_daily_source_v2 的口径（uint32 定点价 /100），
分钟用 tdx_native_reader_v1 的口径（.lc5 原生解析）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_RISK_R0 = Path(__file__).resolve().parents[3] / 'stargazer_ml' / 'v5_hybrid' / 'risk_r0_v1'
_DESKTOP = Path(__file__).resolve().parents[3] / 'stargazer_desktop'
for _p in (str(_RISK_R0), str(_DESKTOP)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import market_data_config_v1 as md_cfg  # noqa: E402

STOCK_PREFIXES = ('sh60', 'sh68', 'sz00', 'sz30', 'bj')

import market_data_config_v1 as md_cfg  # noqa: E402


def vipdoc_root() -> Path:
    return md_cfg.vipdoc_root()


def calendar_days() -> list[int]:
    import tdx_daily_source_v2 as td
    return td.calendar_days()


def stock_stems() -> list[str]:
    """全市场股票代码（sh60/sh68/sz00/sz30/bj），不含指数/板块/基金。"""
    vip = vipdoc_root()
    out = []
    for ex in ('sh', 'sz', 'bj'):
        d = vip / ex / 'lday'
        if not d.is_file() and not d.is_dir():
            continue
        for f in sorted(d.glob('*.day')):
            stem = f.stem
            if any(stem.startswith(p) for p in STOCK_PREFIXES):
                out.append(stem)
    return out


def daily_rows(stem: str, last_n: int, end_day: int) -> list[dict]:
    """返回 end_day 往前 last_n 个交易日的日线行（含 end_day）。"""
    import tdx_daily_source_v2 as td
    path = md_cfg.vipdoc_root() / stem[:2] / 'lday' / (stem + '.day')
    frame, _meta = td.read_day(path, int(end_day))
    frame = frame.tail(last_n)
    rows = []
    for i, (_, r) in enumerate(frame.iterrows()):
        rows.append(dict(
            date=int(r['date']),
            open=float(r['open']),
            high=float(r['high']),
            low=float(r['low']),
            close=float(r['close']),
            volume=float(r['volume']),
            amount=float(r['amount']),
        ))
    return rows


def minute_bars(stem: str, day: int) -> list[dict] | None:
    from tdx_native_reader_v1 import parse_lc5
    f = vipdoc_root() / stem[:2] / 'fzline' / (stem + '.lc5')
    if not f.is_file():
        return None
    try:
        rows = parse_lc5(f.read_bytes())
    except Exception:
        return None
    out = []
    for r in rows:
        if r['day'] != int(day):
            continue
        out.append(dict(ts=int(r['ts']), open=r['open'], high=r['high'],
                        low=r['low'], close=r['close'],
                        volume=float(r['raw_volume']), amount=float(r['amount'])))
    return out or None


def float_shares(stem: str) -> float | None:
    import tdx_daily_source_v2 as td
    hq = vipdoc_root().parent / 'T0002' / 'hq_cache'
    try:
        recs = td.base_records(hq / 'base.dbf')
        v = recs.get(stem)
        return float(v['float_share']) if v else None
    except Exception:
        return None


def stock_names() -> dict[str, str]:
    import tdx_daily_source_v2 as td
    hq = vipdoc_root().parent / 'T0002' / 'hq_cache'
    out = {}
    for m in ('shs.tnf', 'szs.tnf', 'bjs.tnf'):
        p = hq / m
        if p.is_file():
            try:
                out.update(td.names_from_tnf(p))
            except Exception:
                pass
    return out
