"""确定性全链回放：选股 → 冻结 → 触位 → 回执 → 指标 → 重启复核 → 复盘 → 报告。

run_id 由 (规则引用+规则SHA+日期+快照指纹+截止+参数) 推导，
同一组参数重复执行得到同一批字节；中断后按 tasks/<D>.json 续跑。
"""
from __future__ import annotations

import hashlib
import json
import os

from . import rules
from .freeze import run_freeze
from .monitor import run_monitor
from .paths import REPORTS, canonical_bytes, read_json, report_root
from .research import build_report, load_config
from .review import build_review
from .selection import run_selection
from .snapshot import open_snapshot
from .state_store import EventStore, LocalInbox, RunContext, make_run_id

MIN_DAY_CODES = 20
ENGINE_VERSION = 'v1_1'        # 隔离 v1 冻结产物：不同引擎版本使用不同 run_id


def default_days(snap):
    return [int(d) for d in snap.minute_days()
            if len(snap.minute_day_codes(d)) >= MIN_DAY_CODES]


def replay_params(rule_ref, days, cutoff, restart_check, fail_on):
    return dict(rule_ref=rule_ref, days=[int(d) for d in days],
                cutoff=int(cutoff) if cutoff else None,
                restart_check=bool(restart_check),
                fail_on=sorted(fail_on or []),
                engine_version=ENGINE_VERSION)


def run_replay(rule_ref, *, days=None, cutoff=None, restart_check=True,
               fail_on=None, compute_report=True, fresh=True,
               crash_points=None):
    snap = open_snapshot(cutoff)
    rule = rules.get(rule_ref)
    ref = '%s@%s' % (rule['rule_id'], rule['version'])
    days = [int(d) for d in (days or default_days(snap))]
    fail_on = set(fail_on or [])
    params = replay_params(ref, days, cutoff, restart_check, fail_on)
    params['rule_sha256'] = rule['sha256']
    params['snapshot_fingerprint'] = snap.fingerprint
    run_id = make_run_id(**params)
    # 完整版验收专用逃生口：数据补齐后要在同一账本续跑时，允许显式指定
    # run_id（环境变量仅测试/演练注入，正常路径永远走确定性推导）。
    run_id = os.environ.get('RULES_LAB_RUN_ID_OVERRIDE') or run_id
    run = RunContext(run_id, meta=dict(params, rule_title=rule['title']))
    if fresh:
        run.reset()
    inbox = LocalInbox(run, fail_on=fail_on)

    day_notes = []
    for D in days:
        P = snap.prev_trading_day(D)
        stage = run.resume_point(D)
        if stage == 'PREP':
            run.journal(D, 'PREP', dict(P=P))
        sel_path = 'selection_%s.json' % P
        # 阶段复用以任务日志为准，不以产物文件存在为准：--redo-monitor 移除
        # 阶段后必须真正重算，否则主流程会读旧产物、把新结果留给重启复核。
        done_stages = set((run.read('tasks', '%s.json' % D, default={}) or {})
                          .get('completed', []))
        if 'SELECTION' in done_stages and run.exists('out', sel_path):
            sel = run.read('out', sel_path)
        else:
            sel = run_selection(snap, rule, P, run, day=D)
        if 'FREEZE' in done_stages and run.exists('freeze', '%s.json' % D):
            freeze = run.read('freeze', '%s.json' % D)
        else:
            freeze = run_freeze(snap, rule, P, D, sel, run)
        events = EventStore(run, D)
        if 'MONITOR' in done_stages and run.exists('out', 'monitor_%s.json' % D):
            mon = run.read('out', 'monitor_%s.json' % D)
        else:
            try:
                mon = run_monitor(snap, rule, freeze, run, inbox, events,
                                  fail_on=fail_on, crash_points=crash_points)
            except Exception as exc:                 # 中断注入时带出 run 定位
                if hasattr(exc, 'event_id'):
                    exc.run_id = run_id
                    exc.day = D
                raise
        day_notes.append(dict(day=D, P=P,
                              candidates=freeze['candidate_count'],
                              touches=mon['counts']['touch_events'],
                              no_fill=mon['counts']['no_fill'],
                              missing_minute=mon['counts']['missing_minute'],
                              indicator_done=mon['counts']['indicator_done']))

    # 重启复核：同一 run 目录再次进入，SENT/UNKNOWN 不得重发。
    restart_check_result = []
    if restart_check:
        for D in days:
            freeze = run.read('freeze', '%s.json' % D)
            res = run_monitor(snap, rule, freeze, run, inbox,
                              EventStore(run, D), restart=True,
                              fail_on=fail_on, tag='_restart')
            restart_check_result.append(dict(
                day=D,
                duplicate_trigger_bars=res['counts']['duplicate_trigger_bars'],
                resent=len(res['resent']),
                existing_events_kept=(res['recovery'] or {}).get('terminal'),
                inbox_counts=res['inbox_counts'],
                new_touch_events=res['counts']['touch_events']))

    summary, samples = build_review(snap, run, days, ref)
    cfg = load_config()
    integrity = dict(
        source_fingerprint=rules.source_fingerprint(),
        rule_index=rules.list_rules(),
        inbox_counts=inbox.counts(),
        restart_check=restart_check_result,
        days=day_notes,
        review_summary=summary,
        snapshot=snap.snapshot_ref(),
    )
    report = None
    if compute_report:
        # 完整版：研究报告与最终验证锁落各 run 自己的目录（v1.1 单一快照时
        # 共用 REPORTS 无冲突；完整版会换快照/换数据，锁必须随 run 隔离）
        report = build_report(run, cfg, snap, integrity=integrity,
                              lock_provenance=True,
                              output_dir=str(run.path('report')))
        run.journal(days[-1], 'REPORT', dict(study_id=cfg['study_id']))
    manifest = run.finish(dict(params=params, report=bool(compute_report)))
    return dict(contract='RULES_LAB_REPLAY_RESULT_V1', run_id=run_id,
                rule_ref=ref, days=days, manifest=manifest,
                report=report, integrity=integrity,
                day_notes=day_notes)


def _report_hashes():
    """v1.1 报告的逐文件哈希（含预锁），缺文件记 None 而非静默跳过。"""
    out = {}
    for name in ('single_factor_oos_v1_1.md', 'single_factor_oos_v1_1.json',
                 'final_validation_lock_v1.json'):
        p = report_root() / name
        out[name] = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
    return out


def verify_replay(rule_ref, *, days=None, cutoff=None, restart_check=True):
    """跑两遍并逐字节比对全部产物（含收件箱、样本账本与报告/预锁）。"""
    a = run_replay(rule_ref, days=days, cutoff=cutoff,
                   restart_check=restart_check)
    first = dict(a['manifest']['files'])
    first_reports = _report_hashes()
    b = run_replay(rule_ref, days=days, cutoff=cutoff,
                   restart_check=restart_check)
    second = dict(b['manifest']['files'])
    second_reports = _report_hashes()
    changed = sorted(k for k in set(first) | set(second)
                     if first.get(k) != second.get(k))
    report_changed = sorted(k for k in set(first_reports) | set(second_reports)
                            if first_reports.get(k) != second_reports.get(k))
    same_run_id = a['run_id'] == b['run_id']
    deterministic = not changed and not report_changed and same_run_id
    return dict(contract='RULES_LAB_REPLAY_VERIFY_V1_1',
                run_id=a['run_id'], same_run_id=same_run_id,
                file_count=len(first), changed_files=changed,
                report_changed_files=report_changed,
                deterministic=deterministic,
                report_files_present=all(v is not None
                                         for v in second_reports.values()),
                ok=bool(deterministic and
                        all(v is not None for v in second_reports.values())),
                report_sha256=second_reports,
                first=first, second=second)
