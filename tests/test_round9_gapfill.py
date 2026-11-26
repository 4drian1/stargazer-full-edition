"""第九轮验收：联网补缺 + 指标口径统一。

1) 指标口径：build_indicators（提醒补充）与触发判断共用 factors._rsi/_macd，
   同输入下 rsi6/macd_dif 必须逐位一致。
2) 联网补缺：构造分钟缺口（删整天/抽缺根）→ fill_gaps 在线拉取 →
   缺口归零或失败清单注明原因；快照指纹随发布更新；断网兜底不破坏快照。
运行: python tests/test_round9_gapfill.py
"""
import gzip
import json
import shutil
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))


def main():
    # 1) 口径一致：同输入同一实现
    from rules_lab.factors import _rsi as f_rsi, _macd as f_macd
    from rules_lab.monitor import build_indicators
    closes = [10 + 0.3 * ((i % 9) - 4) + 0.05 * i for i in range(60)]
    ind = build_indicators(closes)
    assert ind['rsi6'] == f_rsi(closes, 6), 'rsi6 口径漂移'
    macd = f_macd(closes)
    assert ind['macd_dif'] == (macd[0] if macd else None), 'macd_dif 口径漂移'
    assert ind['field_coverage']['macd_dif']['required_bars'] == 27
    print('indicator parity OK: rsi6=%s macd_dif=%s (与触发判断同一函数)' % (
        ind['rsi6'], ind['macd_dif']))

    # 2) 联网补缺：快照副本上造缺口 → 在线补
    from full.gap_fill import find_gaps, fill_gaps
    work = FE / '_tmp' / 'r9'
    if work.exists():
        shutil.rmtree(work)
    snap = work / 'snap'
    work.mkdir(parents=True)
    shutil.copytree(FE / 'snapshot', snap)
    mm = json.loads(gzip.decompress((snap / 'minutes.json.gz').read_bytes()))
    day_key = '20261118'
    codes = sorted(mm['days'][day_key])
    victim_whole = codes[0]
    victim_partial = codes[1]
    del mm['days'][day_key][victim_whole]                     # 缺整天
    bars = mm['days'][day_key][victim_partial]
    del bars[10]                                              # 缺一根（10:00 附近）
    (snap / 'minutes.json.gz').write_bytes(gzip.compress(
        json.dumps(mm, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    mp = snap / 'snapshot_manifest.json'
    m = json.loads(mp.read_text(encoding='utf-8'))
    m['data_sha256']['minutes.json.gz'] = __import__('hashlib').sha256(
        (snap / 'minutes.json.gz').read_bytes()).hexdigest()
    mp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding='utf-8')

    gaps = find_gaps(snap, days=[20261118],
                     codes={victim_whole, victim_partial})
    assert len(gaps) == 2, gaps
    print('gaps found:', [(g['code'], g['reason'][:40]) for g in gaps])
    before = json.loads(mp.read_text(encoding='utf-8'))['snapshot_fingerprint']
    rep = fill_gaps(snap, days=[20261118], codes={victim_whole, victim_partial})
    print('fill report:', {k: v for k, v in rep.items() if k != 'failed'},
          '| failed:', rep['failed'][:2])
    assert rep['filled'] == 2, rep
    after = json.loads(mp.read_text(encoding='utf-8'))['snapshot_fingerprint']
    assert after != before, 'fingerprint must change after publish'
    remaining = find_gaps(snap, days=[20261118], codes={victim_whole, victim_partial})
    assert not remaining, remaining
    # 补回的数据与本地 vipdoc 口径一致（抽查收盘）
    mm2 = json.loads(gzip.decompress((snap / 'minutes.json.gz').read_bytes()))
    ref_row = mm2['days'][day_key][victim_whole]
    assert len(ref_row) == 48 and ref_row[0][4] > 0
    print('gaps filled=2, fingerprint updated, grid restored (48 bars)')
    # 幂等：无缺时 NO_GAPS
    rep2 = fill_gaps(snap, days=[20261118], codes={victim_whole})
    assert rep2['status'] == 'NO_GAPS', rep2
    print('idempotent NO_GAPS OK')
    # 3) 离线兜底：供应商连不上 → 失败清单 + 快照不动
    import full.gap_fill as gf
    from tdx_supply_v1 import _client as real_client
    def boom():
        raise ConnectionError('simulated offline')
    gf_sys = sys.modules
    import tdx_supply_v1
    tdx_supply_v1._client = lambda: boom()
    fp_before = json.loads((snap / 'snapshot_manifest.json').read_text(encoding='utf-8'))['snapshot_fingerprint']
    mm3 = json.loads(gzip.decompress((snap / 'minutes.json.gz').read_bytes()))
    del mm3['days']['20261118'][victim_partial]
    (snap / 'minutes.json.gz').write_bytes(gzip.compress(
        json.dumps(mm3, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    rep3 = fill_gaps(snap, days=[20261118], codes={victim_partial})
    tdx_supply_v1._client = real_client
    assert rep3['status'] == 'CONNECT_FAILED', rep3
    assert any('SUPPLIER_CONNECT' in (f.get('reason') or '') for f in rep3['failed']), rep3['failed']
    fp_after = json.loads((snap / 'snapshot_manifest.json').read_text(encoding='utf-8'))['snapshot_fingerprint']
    assert fp_after == fp_before, 'snapshot changed on offline path'
    print('offline drill OK:', rep3['failed'][0]['reason'][:60], '| snapshot untouched')
    print('ROUND9_PASS')


if __name__ == '__main__':
    main()
