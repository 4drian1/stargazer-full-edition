"""第二轮回归：快照追加观察日日线后，盘前冻结结果必须逐股一致。

两个子进程分别用「含 D 日行」与「剔除 D 日行」的快照跑 freeze，
逐股比对 zone/黄金档/参考收盘价。任何差异即前视泄漏。
运行: python tests/test_round2_freeze_cutoff.py
"""
import gzip
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PY = sys.executable
D_DAY = 20261118

FREEZE_SCRIPT = r'''
import sys, json
sys.path.insert(0, "{fe_parent}")
sys.path.insert(0, "{fe}")
import rules_lab.replay as replay
print('KERNEL', replay.__file__, file=sys.stderr)''' + r'''
from rules_lab.snapshot import open_snapshot
from rules_lab import rules
from rules_lab.selection import run_selection
from rules_lab.state_store import RunContext
import full.cli as cli
cli._attach_golden()
snap = open_snapshot()
rule = rules.get("strong_pullback@v1")
P, D = 20261117, 20261118
run = RunContext("cutoff_test", meta=dict(test=True))
run.reset()
sel = run_selection(snap, rule, P, run, day=D)
freeze = replay.run_freeze(snap, rule, P, D, sel, run)
out = {{c["code"]: dict(zone_lower=c["zone_lower"], zone_upper=c["zone_upper"],
      reference_close=c["reference_close"], atr14=c["atr14"],
      golden=c.get("golden_references")) for c in freeze["candidates"]}}
print("RESULT_JSON" + json.dumps(out, ensure_ascii=False, sort_keys=True))
'''


def _freeze(snapshot_dir: Path) -> dict:
    env = dict(os.environ)
    env['RULES_LAB_SNAPSHOT_DIR'] = str(snapshot_dir)
    env['RULES_LAB_RUN_ROOT'] = str(snapshot_dir.parent / '_cutoff_runs')
    script = FREEZE_SCRIPT.format(fe=str(FE).replace('\\', '/'),
                                  fe_parent=str(FE.parent).replace('\\', '/'))
    p = subprocess.run([PY, '-X', 'utf8', '-c', script], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", env=env, cwd=str(FE))
    for line in p.stdout.splitlines():
        if line.startswith('RESULT_JSON'):
            return json.loads(line[len('RESULT_JSON'):])
    raise RuntimeError('freeze subprocess failed: %s %s' % (p.stdout[-300:], p.stderr[-300:]))


def main():
    src = FE / 'snapshot'
    stripped = FE / '_tmp' / 'snapshot_noD'
    if stripped.exists():
        shutil.rmtree(stripped)
    stripped.mkdir(parents=True)
    for f in src.iterdir():
        if f.name == 'daily.json.gz':
            d = json.loads(gzip.decompress(f.read_bytes()))
            cols = d['columns']
            ix = {c: i for i, c in enumerate(cols)}
            for code, rows in d['codes'].items():
                has_real_d = rows and rows[-1][ix['date']] == 20261118
                kept = [r for r in rows if r[ix['date']] <= 20261117]
                if has_real_d:
                    # 只保留盘前已知的 D 日 pre_close（= P 收盘），OHLC 置空——
                    # 冻结允许用 pre_close，绝不允许用 D 的 OHLC。
                    pre_close = kept[-1][ix['close']] if kept else None
                    row = [None] * len(cols)
                    row[ix['date']] = 20261118
                    row[ix['pre_close']] = pre_close
                    row[ix['is_st']] = kept[-1][ix['is_st']] if kept else 0
                    row[ix['qfq_factor']] = 1.0
                    kept.append(row)
                rows[:] = kept
            (stripped / f.name).write_bytes(gzip.compress(
                json.dumps(d, ensure_ascii=False, separators=(',', ':')).encode(), mtime=0))
        else:
            (stripped / f.name).write_bytes(f.read_bytes())
    with_d = _freeze(src)
    without_d = _freeze(stripped)
    assert set(with_d) == set(without_d), 'candidate set differs'
    diff = []
    for code in with_d:
        a, b = with_d[code], without_d[code]
        if json.dumps(a, sort_keys=True) != json.dumps(b, sort_keys=True):
            diff.append(code)
    print('candidates:', len(with_d), '| per-stock diffs after appending D daily:', len(diff))
    if diff:
        c = diff[0]
        print('example', c, json.dumps(with_d[c], ensure_ascii=False)[:300])
        print('vs stripped', json.dumps(without_d[c], ensure_ascii=False)[:300])
        raise SystemExit(1)
    print('CUTOFF_REGRESSION_PASS')


if __name__ == '__main__':
    main()
