#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""纯规则实验台唯一入口（独立启动命令 / 端口 / 环境 / 路径）。

    python -B -X utf8 -m rules_lab.cli env
    python -B -X utf8 -m rules_lab.cli rule bootstrap
    python -B -X utf8 -m rules_lab.cli serve --port 18765
    python -B -X utf8 -m rules_lab.cli replay --rule strong_pullback@v1 --verify
    python -B -X utf8 -m rules_lab.cli audit
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import audit, rules
from .factors import FACTOR_REGISTRY
from .form import lab_env, serve
from .interpreter import preview
from .paths import (ROOT, STATE, atomic_write_json, ensure_dirs, read_json)
from .replay import default_days, run_replay, verify_replay
from .research import build_report, load_config
from .snapshot import open_snapshot
from .state_store import RunContext


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=1, default=str))


def cmd_env(args):
    ensure_dirs()
    env = lab_env(args.port)
    atomic_write_json(ROOT / 'lab_data' / 'build' / 'lab_env.json', env)
    _print(env)


def cmd_snapshot(args):
    snap = open_snapshot()
    _print(dict(contract='RULES_LAB_SNAPSHOT_INFO_V1',
                snapshot=snap.snapshot_ref(), coverage=snap.coverage,
                limitations=snap.limitations,
                effective_days=default_days(snap)))


def cmd_rule(args):
    ensure_dirs()
    action = args.action
    if action == 'bootstrap':
        _print(dict(created=rules.bootstrap(FACTOR_REGISTRY),
                    registry=rules.list_rules()))
    elif action == 'list':
        _print(rules.list_rules())
    elif action == 'show':
        _print(rules.get(args.ref))
    elif action == 'import':
        _print(rules.import_rule(args.file, version=args.version))
    elif action == 'publish':
        _print(rules.publish(args.rule_id, version=args.version,
                             registered=FACTOR_REGISTRY))
    elif action == 'edit':
        try:
            value = json.loads(args.value)
        except (ValueError, TypeError):
            value = args.value
        _print(rules.edit(args.rule_id, {args.path: value},
                          base_ref=args.base_ref))
    elif action == 'preview':
        rule = rules.get(args.ref)
        snap = open_snapshot()
        _print(dict(rule_ref=args.ref, code=args.code, day=args.day,
                    **preview(rule, _selection_env(snap, rule, args.code, args.day))))
    elif action == 'export':
        _print(dict(file=rules.export_rule(args.ref, args.out)))
    else:
        raise SystemExit('UNKNOWN_RULE_ACTION:' + str(action))


def _selection_env(snap, rule, code, day):
    from .factors import apply_formula_factors, compute_selection_factors
    rows = snap.daily_asof(code, int(day))
    env = compute_selection_factors(rows, rule['zone'])
    return apply_formula_factors(env, rule)


def cmd_serve(args):
    ensure_dirs()
    serve(args.port)


def cmd_replay(args):
    ensure_dirs()
    days = [int(x) for x in args.days.split(',')] if args.days else None
    if args.verify:
        v = verify_replay(args.rule, days=days, cutoff=args.cutoff,
                          restart_check=not args.no_restart)
        _print({k: val for k, val in v.items() if k not in ('first', 'second')})
        # v1.1：逐文件比对或报告/预锁出现差异时返回非零退出码，
        # 让 CI/脚本能直接据退出码判定回放不确定，而不是只看打印文本。
        return 0 if v['ok'] else 1
    result = run_replay(args.rule, days=days, cutoff=args.cutoff,
                        restart_check=not args.no_restart,
                        compute_report=not args.no_report,
                        fresh=not args.resume)
    _print(dict(run_id=result['run_id'], rule_ref=result['rule_ref'],
                days=result['days'], day_notes=result['day_notes'],
                inbox_counts=result['integrity']['inbox_counts'],
                review_summary=result['integrity']['review_summary'],
                report=(result['report'] or {}).get('verdict'),
                files=len(result['manifest']['files'])))
    return 0


def cmd_report(args):
    run = RunContext(args.run_id)
    snap = open_snapshot()
    cfg = load_config()
    integrity = run.read('out', 'single_factor_oos_v1_1.json',
                         default={}).get('integrity', {})
    _print(dict(run_id=args.run_id,
                verdict=build_report(run, cfg, snap,
                                     integrity=integrity)['verdict']))


def cmd_audit(args):
    _print(audit.full_audit())


def cmd_baseline(args):
    sys.path.insert(0, str(ROOT / 'tools'))
    import baseline
    if args.action == 'record':
        baseline.record()
    else:
        raise SystemExit(baseline.verify())


def build_parser():
    p = argparse.ArgumentParser(prog='rules_lab')
    sub = p.add_subparsers(dest='command', required=True)
    q = sub.add_parser('env')
    q.add_argument('--port', type=int, default=18765)
    q.set_defaults(func=cmd_env)
    q = sub.add_parser('snapshot')
    q.set_defaults(func=cmd_snapshot)
    q = sub.add_parser('rule')
    q.add_argument('action', choices=('bootstrap', 'list', 'show', 'import',
                                      'publish', 'edit', 'preview', 'export'))
    q.add_argument('--ref'); q.add_argument('--rule-id'); q.add_argument('--file')
    q.add_argument('--version'); q.add_argument('--path'); q.add_argument('--value')
    q.add_argument('--base-ref'); q.add_argument('--out')
    q.add_argument('--code', default='600000'); q.add_argument('--day', default='20261110')
    q.set_defaults(func=cmd_rule)
    q = sub.add_parser('serve')
    q.add_argument('--port', type=int, default=18765)
    q.set_defaults(func=cmd_serve)
    q = sub.add_parser('replay')
    q.add_argument('--rule', default='strong_pullback@v1')
    q.add_argument('--days'); q.add_argument('--cutoff', type=int)
    q.add_argument('--verify', action='store_true')
    q.add_argument('--resume', action='store_true',
                   help='从 tasks/<D>.json 续跑，不重置已有产物')
    q.add_argument('--no-restart', action='store_true')
    q.add_argument('--no-report', action='store_true')
    q.set_defaults(func=cmd_replay)
    q = sub.add_parser('report')
    q.add_argument('--run-id', required=True)
    q.set_defaults(func=cmd_report)
    q = sub.add_parser('audit')
    q.set_defaults(func=cmd_audit)
    q = sub.add_parser('baseline')
    q.add_argument('action', choices=('record', 'verify'))
    q.set_defaults(func=cmd_baseline)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    ensure_dirs()
    rc = args.func(args)
    return int(rc or 0)


if __name__ == '__main__':
    raise SystemExit(main())
