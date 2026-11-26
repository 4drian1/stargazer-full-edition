"""第三轮：真实 --resume 续跑演练。

场景：先有缺口并送达部分提醒 → 中断 → 正常入口 --resume 恢复 →
补齐行情 → --resume --redo-monitor 重判。
验收：旧回执零重复；补到的数据产生预期的新结果（与完整基线逐键对齐）。

snapshot_drill：完整快照抽掉 5 只「基线里有触位回执」的候选的分钟线，
并给它独立的 manifest 指纹（修第二轮共用指纹问题）。
运行: python tests/test_round3_resume.py
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
RUN_ID = 'resume_drill_run'
DROPPED = 5

RUNNER = r'''
import sys, json
sys.path.insert(0, ".")
sys.path.insert(0, "..")
import full.cli as cli
raise SystemExit(cli.main(json.loads(sys.argv[1])))
'''


def run_cli(args, env_extra, expect_rc):
    env = dict(os.environ)
    env.update(env_extra)
    for attempt in range(3):
        p = subprocess.run([PY, '-X', 'utf8', '-c', RUNNER, json.dumps(args)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, cwd=str(FE))
        tail = (p.stdout or '')[-150:] + (p.stderr or '')[-150:]
        if p.returncode == expect_rc or 'PermissionError' not in tail:
            break
        import time
        time.sleep(10)
    print('  rc=%s %s' % (p.returncode, ' '.join(str(x) for x in args[:3])), tail.replace('\n', ' ')[-120:])
    assert p.returncode == expect_rc, 'expected %s: %s' % (expect_rc, tail)
    return p


def receipts(run_root):
    """返回 (dict, 原始行数, 唯一键数)：重复行在断言前不会被覆盖。"""
    f = Path(run_root) / RUN_ID / 'inbox' / 'receipts.jsonl'
    if not f.is_file():
        return {}, 0, 0
    out = {}
    n_lines = 0
    for line in f.read_text(encoding='utf-8').splitlines():
        if line.strip():
            n_lines += 1
            r = json.loads(line)
            out[(r['event_id'], r['kind'])] = r
    return out, n_lines, len(out)


def main():
    # 基线：当前快照上的完整运行（不用历史 run_a，避免快照演进造成口径漂移）
    base_root = FE / 'run_resume3_base'
    if base_root.exists():
        shutil.rmtree(base_root)
    base_root.mkdir()
    env_b = {'RULES_LAB_RUN_ROOT': str(base_root)}
    run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261118'], env_b, 0)
    base_f = sorted(base_root.glob('*/inbox/receipts.jsonl'))[-1]
    base = {}
    for line in base_f.read_text(encoding='utf-8').splitlines():
        r = json.loads(line)
        base[(r['event_id'], r['kind'])] = r
    mon = json.loads(sorted(base_root.glob('*/out/monitor_20261118.json'))[-1].read_text(encoding='utf-8'))
    touched = sorted({s['code'] for s in mon.get('samples', []) if s.get('code')})
    assert len(touched) >= DROPPED, 'touched codes too few'
    drop_codes = touched[:DROPPED]
    print('base receipts:', len(base), '| dropped minute codes:', drop_codes)

    # 构造 snapshot_drill（独立指纹）
    drill = FE / 'snapshot_drill'
    if drill.exists():
        shutil.rmtree(drill)
    drill.mkdir(parents=True)
    for f in (FE / 'snapshot').iterdir():
        if f.name != 'minutes.json.gz':
            (drill / f.name).write_bytes(f.read_bytes())
    m = json.loads(gzip.decompress((FE / 'snapshot' / 'minutes.json.gz').read_bytes()))
    day = list(m['days'])[0]
    for c in drop_codes:
        m['days'][day].pop(c, None)
    (drill / 'minutes.json.gz').write_bytes(gzip.compress(
        json.dumps(m, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
    mp = drill / 'snapshot_manifest.json'
    man = json.loads(mp.read_text(encoding='utf-8'))
    man['data_sha256'] = {f.name: hashlib.sha256((drill / f.name).read_bytes()).hexdigest()
                          for f in drill.iterdir() if f.name != 'snapshot_manifest.json'}
    man['note'] = 'drill snapshot: minutes dropped for %s touched codes' % drop_codes
    body = json.dumps({k: v for k, v in man.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    man['snapshot_fingerprint'] = hashlib.sha256(body).hexdigest()
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding='utf-8')

    run_root = FE / 'run_resume3'
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir()
    env = {'RULES_LAB_RUN_ROOT': str(run_root),
           'RULES_LAB_SNAPSHOT_DIR': str(drill),
           'RULES_LAB_RUN_ID_OVERRIDE': RUN_ID}

    print('A) 缺口快照 + 中断注入（回执写入后立即崩）')
    run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261118',
             '--crash-at', 'AFTER_RECEIPT_WRITE'], env, 1)
    ra, ra_lines, ra_keys = receipts(run_root)
    print('  receipts after crash:', len(ra), '| lines:', ra_lines, '| keys:', ra_keys)

    print('B) 正常入口 --resume 恢复（仍是缺口数据）')
    run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261118', '--resume'], env, 0)
    rb, rb_lines, rb_keys = receipts(run_root)
    receipts_text_b = (Path(run_root) / RUN_ID / 'inbox' / 'receipts.jsonl').read_text(encoding='utf-8')
    print('  receipts after resume:', len(rb), '| lines:', rb_lines, '| keys:', rb_keys)
    assert len(rb) >= len(ra)
    assert rb_lines == rb_keys, 'duplicate receipt lines after resume'

    print('B2) 误操作探针：--redo-monitor 漏传 --resume 必须被拒且运行目录零变化')
    def digest(root):
        import hashlib as _h
        out = {}
        for p in sorted(Path(root).rglob('*')):
            if p.is_file():
                out[str(p.relative_to(root))] = _h.sha256(p.read_bytes()).hexdigest()
        return out
    before = digest(run_root)
    run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261118',
             '--redo-monitor', '--run-id', RUN_ID], env, 2)
    after = digest(run_root)
    assert before == after, 'run dir changed by rejected redo (no --resume)'
    print('  rejected redo left run dir untouched (%d files)' % len(after))

    print('C) 补齐行情（拷回完整分钟 + 重算指纹），--resume --redo-monitor 重判')
    freeze_before = (Path(run_root) / RUN_ID / 'freeze' / '20261118.json').read_bytes()
    rule_sha_before = hashlib.sha256(
        (FE / 'run' / 'state' / 'rules' / 'published' / 'strong_pullback@v1.json').read_bytes()).hexdigest()
    (drill / 'minutes.json.gz').write_bytes((FE / 'snapshot' / 'minutes.json.gz').read_bytes())
    man['data_sha256']['minutes.json.gz'] = hashlib.sha256(
        (drill / 'minutes.json.gz').read_bytes()).hexdigest()
    man.pop('note', None)
    body = json.dumps({k: v for k, v in man.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    man['snapshot_fingerprint'] = hashlib.sha256(body).hexdigest()
    mp.write_text(json.dumps(man, ensure_ascii=False, indent=1), encoding='utf-8')
    run_cli(['run', '--rule', 'strong_pullback@v1', '--days', '20261118',
             '--resume', '--redo-monitor', '--no-restart-check',
             '--run-id', RUN_ID], env, 0)
    mon = json.loads((Path(run_root) / RUN_ID / 'out' / 'monitor_20261118.json').read_text(encoding='utf-8'))
    print('  main monitor after re-judge: missing_minute =', (mon.get('counts') or {}).get('missing_minute'),
          '| touch =', (mon.get('counts') or {}).get('touch_events'))
    assert (mon.get('counts') or {}).get('missing_minute') == 0, 'main monitor still shows missing codes'
    rc, rc_lines, rc_keys = receipts(run_root)
    print('  receipts after restore+re-judge:', len(rc), '| lines:', rc_lines, '| keys:', rc_keys, '| base:', len(base))
    assert rc_lines == rc_keys, 'duplicate receipt lines after re-judge'

    # 验收：旧回执零重复（键唯一由构造保证，再核对与基线一致性）
    assert len(rc) == len(base), 'final receipts %s != base %s' % (len(rc), len(base))
    same = sum(1 for k in base if k in rc)
    print('  keys aligned with base run: %d/%d' % (same, len(base)))
    assert same == len(base), 'receipt keys diverge from base'
    final_keys = set(rc)
    assert len(final_keys) == len(rc), 'duplicate keys in ledger'
    review = json.loads((Path(run_root) / RUN_ID / 'out' / 'review_summary.json').read_text(encoding='utf-8'))
    base_review = json.loads(sorted(base_root.glob('*/out/review_summary.json'))[-1].read_text(encoding='utf-8'))
    assert review.get('sample_kind_counts') == base_review.get('sample_kind_counts'), \
        'review not synced: %s vs %s' % (review.get('sample_kind_counts'), base_review.get('sample_kind_counts'))
    print('  review sample_kind_counts synced with base:', review.get('sample_kind_counts'))
    # 绑定不变量：补数只动行情，规则/冻结/已送达回执继续绑定
    assert (Path(run_root) / RUN_ID / 'freeze' / '20261118.json').read_bytes() == freeze_before, \
        'freeze changed after data restore'
    rule_sha_after = hashlib.sha256(
        (FE / 'run' / 'state' / 'rules' / 'published' / 'strong_pullback@v1.json').read_bytes()).hexdigest()
    assert rule_sha_after == rule_sha_before, 'rule changed after data restore'
    receipts_path = Path(run_root) / RUN_ID / 'inbox' / 'receipts.jsonl'
    assert receipts_path.read_text(encoding='utf-8').startswith(receipts_text_b), \
        'pre-restore receipt ledger mutated (append-only violated)'
    print('  bindings intact: rule sha / freeze bytes / pre-restore receipts prefix unchanged')
    print('RESUME_DRILL_PASS: old receipts kept single copy; restored data '
          'produced %d expected new receipts; ledger == base (%d keys)' % (len(rc) - len(rb), len(rc)))


if __name__ == '__main__':
    main()
