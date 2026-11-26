"""指数日 K 只读器：本地通达信 vipdoc 的指数 .day 文件。

指数历史记录存在 0.01 级高低收不一致（如 1991 年记录），生产版个股严格校验
（TDX_DAY_BAD_VALUES）会整文件拒绝。指数只做展示用图，采用容忍口径：
  - 日期严格递增、正值校验保留；
  - 单条 l>min(o,c) 或 h<max(o,c) 的轻微不一致记录被剔除并计数（不静默当正常）。
沪指在本安装的 stem 是 sh999999（与生产版 day_path 映射一致）。
"""
from __future__ import annotations
import struct
from pathlib import Path

STEMS = {'SSE': 'sh999999', 'SZSE': 'sz399001', 'CYB': 'sz399006'}


def index_daily(symbol, last_n=120, vipdoc_root=None):
    from full.vipdoc_reader import vipdoc_root as _root
    stem = STEMS.get(str(symbol).upper())
    if not stem:
        raise ValueError('BAD_INDEX_SYMBOL')
    root = Path(vipdoc_root) if vipdoc_root else _root()
    path = root / stem[:2] / 'lday' / (stem + '.day')
    if not path.is_file():
        raise ValueError('INDEX_DAY_MISSING:' + str(path))
    raw = path.read_bytes()
    if not raw or len(raw) % 32:
        raise ValueError('TDX_DAY_LAYOUT')
    bars, dropped = [], 0
    prev = 0
    for d, o, h, l, c, a, v, _ in struct.iter_unpack('<IIIIIfII', raw):
        if d <= prev:
            continue
        prev = d
        o, h, l, c = o / 100, h / 100, l / 100, c / 100
        if min(o, h, l, c) <= 0:
            dropped += 1
            continue
        if l > min(o, c) + 0.02 or h < max(o, c) - 0.02:  # 容忍 0.02 级历史记录不一致
            dropped += 1
            continue
        bars.append(dict(ts=d, open=round(o, 2), high=round(h, 2), low=round(l, 2),
                         close=round(c, 2), volume=int(v)))
    return bars[-last_n:], dropped
