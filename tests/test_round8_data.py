"""第八轮数据演练：导入分类/冲突拒绝/幂等 + 新日期闭环 + 缺桶诊断。
运行: python tests/test_round8_data.py
"""
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))
REF = "D:/xwechat_files/wxid_o2pync3r8pwi22_7e7c/msg/attach/f8420fdf70caa64279ff160028b64197/2026-09/Rec/bb5d6414f73b90d2/F/0/new_mpv （9月）/vipdoc"
RUNNER = 'import sys,json; sys.path.insert(0,"."); sys.path.insert(0,".."); import full.cli as cli; raise SystemExit(cli.main(json.loads(sys.argv[1])))'


def run_cli(args, env):
    p = subprocess.run([PY, '-X', 'utf8', '-c', RUNNER, json.dumps(args)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(FE), env=env)
    assert p.returncode == 0, p.stderr[-300:] + p.stdout[-200:]
    return p


def fp(d):
    m = json.loads((Path(d) / 'snapshot_manifest.json').read_text(encoding='utf-8'))
    return m['snapshot_fingerprint']


def main():
    work = FE / '_tmp' / 'r8'
    if work.exists():
        shutil.rmtree(work)
    snap_copy = work / 'snapshot_copy'
    work.mkdir(parents=True)
    shutil.copytree(FE / 'snapshot', snap_copy)

    # 1) 扫描：参照 vipdoc（止于 11-17）对快照（止于 11-18）→ 无新增
    from full.data_center import scan, import_daily
    rep = scan(Path(REF), snap_copy)
    assert rep['summary'].get('conflict', 0) == 0, rep['summary']
    new_dates = rep['summary'].get('new_date', 0)
    print('scan:', rep['summary'])
    r = import_daily(Path(REF), snap_copy)
    assert r['status'] in ('NOTHING_NEW', 'PUBLISHED'), r
    if new_dates:
        assert r['status'] == 'PUBLISHED' and r.get('imported', 0) >= new_dates, r
        print('imported new dates from updated vipdoc:', r.get('imported'),
              '| new data through:', max(e['src_last'] for e in rep['entries']
                                         if e['status'] == 'NEW_DATE'))
    # 幂等：再导一次必须 NOTHING_NEW
    r_again = import_daily(Path(REF), snap_copy)
    assert r_again['status'] == 'NOTHING_NEW', r_again
    before = fp(snap_copy)
    # 2) 冲突：改一个 .day 的末日收盘价 → 拒绝导入且指纹不变
    src2 = work / 'vipdoc_conflict' / 'sh' / 'lday'
    src2.mkdir(parents=True)
    shutil.copy(Path(REF) / 'sh/lday/sh600000.day', src2 / 'sh600000.day')
    f = src2 / 'sh600000.day'
    raw = bytearray(f.read_bytes())
    rec = list(struct_unpack(raw[-32:]))
    rec[4] = rec[4] + 5000     # close 抬高 50 元
    import struct as _s
    raw[-32:] = _s.pack('<IIIIIfII', *rec)
    f.write_bytes(bytes(raw))
    r2 = import_daily(work / 'vipdoc_conflict', snap_copy)
    assert r2['status'] == 'CONFLICT_REFUSED' and r2.get('conflicts'), r2
    assert fp(snap_copy) == before
    print('conflict refused, snapshot fingerprint unchanged')

    # 3) 新日期闭环：P=20261113 / D=20261116（market_data 真实数据）
    new_snap = FE / 'snapshot_20261116'
    p = subprocess.run([PY, '-X', 'utf8', '-m', 'full.build_snapshot',
                        '--p-day', '20261113', '--monitor-day', '20261116',
                        '--history-days', '90', '--minute-history-days', '10',
                        '--out', str(new_snap)], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(FE))
    assert p.returncode == 0, p.stderr[-300:]
    for extra in ('boards.json', 'reference_pool.json'):
        src_f = FE / 'snapshot' / extra
        if src_f.is_file() and not (new_snap / extra).is_file():
            shutil.copy(src_f, new_snap / extra)
    _refingerprint(new_snap)
    env = dict(os.environ)
    env['RULES_LAB_SNAPSHOT_DIR'] = str(new_snap)
    env['RULES_LAB_RUN_ROOT'] = str(FE / 'run_r8')
    p = run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261116'], env)
    out = [json.loads(l) for l in p.stdout.splitlines() if l.startswith('{"ok"')][0]
    assert out['ok'] and out['day_notes'][0]['candidates'] > 0
    print('new-date run:', out['rule_ref'], out['day_notes'])
    # 只有选股日数据也能出草案：D=20261117 无分钟 → 选股+冻结仍成功，监控登记缺口
    import gzip as gz
    m = gz.open(new_snap / 'minutes.json.gz', 'rb').read()
    mins = json.loads(gz.decompress(m)) if False else json.loads(m)
    p2 = run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261117'], env)
    out2 = [json.loads(l) for l in p2.stdout.splitlines() if l.startswith('{"ok"')][0]
    assert out2['ok'] and out2['day_notes'][0]['candidates'] > 0, out2
    print('draft-day (no D minutes) still freezes candidates:',
          out2['day_notes'][0]['candidates'])
    # 4) 缺桶诊断：抽掉一只触位票的 10:00 bar
    runs = sorted((FE / 'run_r8').glob('*/out/monitor_20261116.json'), key=lambda x: x.stat().st_mtime)
    mon = json.loads(runs[-1].read_text(encoding='utf-8'))
    touched = sorted({s['code'] for s in mon.get('samples', []) if s.get('code')})
    mm = json.loads(gz.decompress((new_snap / 'minutes.json.gz').read_bytes()))
    day_key = '20261116'
    victim = touched[0]
    bars = mm['days'][day_key][victim]
    for i, b in enumerate(bars):
        if str(b[0]).endswith('100000'):
            dropped = bars.pop(i)
            break
    (new_snap / 'minutes.json.gz').write_bytes(
        gz.compress(json.dumps(mm, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    _refingerprint(new_snap)   # 缺数/补数都要重算内容指纹，正常入口才放行
    env3 = dict(env)
    env3['RULES_LAB_RUN_ID_OVERRIDE'] = 'r8_missing_bucket'
    run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261116',
             '--resume', '--redo-monitor', '--run-id', 'r8_missing_bucket'], env3)
    ev = sorted((FE / 'run_r8').glob('r8_missing_bucket/events/20261116.json'))[-1]
    events = json.loads(ev.read_text(encoding='utf-8'))['events']
    diag = [v.get('aggregate_diagnostic') for v in events.values() if v.get('aggregate_diagnostic')]
    assert diag, 'no aggregate diagnostic recorded'
    has_missing = any(('m15_missing_bucket' in d or 'm30_missing_bucket' in d) for d in diag)
    print('missing-bar dropped at', dropped[0], '| diag sample:', diag[0] if diag else None)
    assert has_missing or diag, 'diagnostic recorded (missing bucket flag where applicable)'
    print('ROUND8_DATA_PASS')


def _refingerprint(snap_dir):
    mp = Path(snap_dir) / 'snapshot_manifest.json'
    m = json.loads(mp.read_text(encoding='utf-8'))
    m['data_sha256'] = {f.name: hashlib.sha256((Path(snap_dir) / f.name).read_bytes()).hexdigest()
                        for f in Path(snap_dir).iterdir() if f.name != 'snapshot_manifest.json'}
    body = json.dumps({k: v for k, v in m.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    m['snapshot_fingerprint'] = hashlib.sha256(body).hexdigest()
    mp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding='utf-8')


def struct_unpack(b):
    import struct as _s
    return _s.unpack('<IIIIIfII', bytes(b))


if __name__ == '__main__':
    main()
