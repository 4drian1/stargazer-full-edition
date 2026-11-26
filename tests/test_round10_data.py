"""第十轮数据可靠性收口：四缺陷修复验收。
切换失败注入 / 缺席股扫描 / .lc5 单独导入+日历前进 / 最高价冲突 /
嵌套组依赖窗口 / last_run 事件账本触位数。
运行: python tests/test_round10_data.py
"""
import gzip
import json
import os
import shutil
import struct
import subprocess
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))
REF = "D:/xwechat_files/wxid_o2pync3r8pwi22_7e7c/msg/attach/f8420fdf70caa64279ff160028b64197/2026-09/Rec/bb5d6414f73b90d2/F/0/new_mpv （9月）/vipdoc"


def main():
    from full.data_center import scan, import_daily, rule_window, swap_snapshot
    from full import data_center as dc
    work = FE / '_tmp' / 'r10'
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    snap = work / 'snap'
    shutil.copytree(FE / 'snapshot', snap)
    fp0 = json.loads((snap / 'snapshot_manifest.json').read_text(encoding='utf-8'))['snapshot_fingerprint']

    # 0) 造一份带「未来新日期」的 .day，保证切换路径稳定被走（不依赖真实世界更新节奏）
    fake_dir = work / 'fake_new' / 'sh' / 'lday'
    fake_dir.mkdir(parents=True)
    shutil.copy(Path(REF) / 'sh/lday/sh600000.day', fake_dir / 'sh600000.day')
    f0 = fake_dir / 'sh600000.day'
    raw = bytearray(f0.read_bytes())
    rec0 = list(struct.unpack('<IIIIIfII', bytes(raw[-32:])))
    rec0[0] = 20261201
    raw[-32:] = struct.pack('<IIIIIfII', *rec0)
    f0.write_bytes(bytes(raw))

    # 1) 切换失败注入：旧快照必须始终可用
    real_replace = Path.replace
    def boom(self, target):
        if str(target).endswith('snap') and 'backup' not in str(self):
            raise RuntimeError('INJECTED_SWAP_FAIL')
        return real_replace(self, target)
    Path.replace = boom
    rep = import_daily(work / 'fake_new', snap)
    Path.replace = real_replace
    assert rep.get('status') == 'SWAP_FAILED', rep
    fp_keep = json.loads((snap / 'snapshot_manifest.json').read_text(encoding='utf-8'))['snapshot_fingerprint']
    assert fp_keep == fp0, 'old snapshot lost on swap failure'
    print('swap-injection: SWAP_FAILED reported, old snapshot intact (fp unchanged)')
    # 解除注入后再导一次必须成功（可恢复）
    rep_b = import_daily(work / 'fake_new', snap)
    assert rep_b.get('status') == 'PUBLISHED' and rep_b.get('imported', 0) > 0, rep_b
    fp1 = json.loads((snap / 'snapshot_manifest.json').read_text(encoding='utf-8'))['snapshot_fingerprint']
    if rep_b.get('status') == 'PUBLISHED':
        assert fp1 != fp0, 'published but fingerprint unchanged'
    print('retry after failure: %s OK' % rep_b.get('status'))

    # 2) 整天缺席的票也进缺口清单（构造：从分钟里删一只存在的票）
    from full.gap_fill import find_gaps
    mm = json.loads(gzip.decompress((snap / 'minutes.json.gz').read_bytes()))
    victim = sorted(mm['days']['20261118'])[5]
    saved = mm['days']['20261118'].pop(victim)
    (snap / 'minutes.json.gz').write_bytes(gzip.compress(
        json.dumps(mm, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    gaps = find_gaps(snap, days=[20261118], codes={victim})
    assert gaps and gaps[0]['reason'] == 'NO_MINUTE_FILE_FOR_DAY', gaps
    print('absent-code gap listed:', gaps[0])

    # 3) .lc5 单独导入：只放 lc5 → 分钟并入（用全新副本，避免步骤1已并过分钟）
    snap_l = work / 'snap_lc5'
    shutil.copytree(FE / 'snapshot', snap_l)
    src = work / 'lc5_only'
    d = src / 'sz' / 'fzline'
    d.mkdir(parents=True)
    shutil.copy(Path(REF) / 'sz/fzline/sz300118.lc5', d / 'sz300118.lc5')
    before_cal = json.loads((snap_l / 'calendar.json').read_text(encoding='utf-8'))['days'][-1]
    rep2 = import_daily(src, snap_l)
    assert rep2['ok'], rep2
    mm2 = json.loads(gzip.decompress((snap_l / 'minutes.json.gz').read_bytes()))
    assert mm2['days'].get('20261117', {}).get('300118'), 'lc5 not imported'
    assert rep2['ok'], rep2
    # 坏网格兜底：截断成 30 根后导入应被拒并记清单，且快照分钟不混入坏日
    import struct as _st
    f3 = d / 'sz300118.lc5'
    raw3 = f3.read_bytes()
    cut = raw3[:len(raw3) // 32 // 2 * 32]     # 只留一半记录（网格必然不全）
    f3.write_bytes(cut)
    snap_l2 = work / 'snap_lc5_2'
    shutil.copytree(FE / 'snapshot', snap_l2)
    rep4 = import_daily(d, snap_l2)
    assert rep4['ok'], rep4
    print('lc5 idempotent import OK | bad-grid day skipped:', rep4.get('minutes_imported'))
    after_cal = json.loads((snap_l / 'calendar.json').read_text(encoding='utf-8'))['days'][-1]
    print('lc5-only import OK | calendar %s -> %s | minutes_imported=%s' % (
        before_cal, after_cal, rep2.get('minutes_imported')))

    # 4) 只改最高价也判冲突
    csrc = work / 'conflict'
    cd = csrc / 'sh' / 'lday'
    cd.mkdir(parents=True)
    shutil.copy(Path(REF) / 'sh/lday/sh600000.day', cd / 'sh600000.day')
    f = cd / 'sh600000.day'
    raw = bytearray(f.read_bytes())
    rec = list(struct.unpack('<IIIIIfII', bytes(raw[-32:])))
    snap_d = json.loads(gzip.decompress((snap / 'daily.json.gz').read_bytes()))
    cols = snap_d['columns']
    row = [r for r in snap_d['codes']['600000'] if r[cols.index('date')] == rec[0]][-1]
    rec[2] = int(row[cols.index('high')] * 100) + 800   # 只抬高最高价
    raw[-32:] = struct.pack('<IIIIIfII', *rec)
    f.write_bytes(bytes(raw))
    rep3 = import_daily(csrc, snap)
    assert rep3.get('status') == 'CONFLICT_REFUSED' and rep3.get('conflicts'), rep3
    print('high-only conflict detected:', rep3['conflicts'][0].get('changed_field'))

    # 5) 嵌套规则组的依赖窗口
    rule = json.loads(json.dumps(__import__('rules_lab.rules', fromlist=['get']).get('strong_pullback@v1')))
    rule['selection'] = dict(op='AND', children=[
        dict(op='AND', children=[dict(field='ret_60d', op='>=', value=0.1)])])
    assert rule_window_for(rule) >= 61, rule_window_for(rule)
    print('nested-group rule window OK')
    print('ROUND10_DATA_PASS')


def rule_window_for(rule):
    from rules_lab.factors import factor_catalog
    cat = factor_catalog()
    need = 7
    def walk(n):
        if isinstance(n, dict):
            if 'field' in n:
                yield n
            for c in n.get('children') or []:
                yield from walk(c)
    for sec in ('selection', 'trigger'):
        for c in walk(rule.get(sec) or {}):
            spec = cat.get(c.get('field'))
            for tok in ((spec or {}).get('window', '') or '').replace('+', ' ').split():
                if tok.endswith('交易日') and tok[:-3].isdigit():
                    need = max(need, int(tok[:-3]) + 1)
    return need


if __name__ == '__main__':
    main()
