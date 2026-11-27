"""完整版第一轮入口：本地行情 → 规则选股 → 冻结 → 次日逐根 → 模拟提醒 → 复盘。

复用 rules_lab v1.1 内核（解释器/选股/监控/回放/复盘同一套），
差异：真实行情快照、冻结件附加黄金档参考、自有运行目录与端口。
断网/无密钥/无模型权重可运行（全部本地确定性计算）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parents[1]))          # stagazer_r（兜底）
sys.path.insert(0, str(FE))                     # full_edition 优先于 v1.1

import rules_lab.replay as replay
import rules_lab.rules as rules
from rules_lab.paths import REPORTS, SNAPSHOT, ensure_dirs, read_json
from rules_lab.selection import run_selection
from rules_lab.snapshot import open_snapshot


def _attach_golden():
    """冻结后给每个候选附加黄金档参考（temporary/reference-only）。"""
    from rules_lab.freeze import run_freeze as orig

    def wrapped(snap, rule, P, D, selection, run):
        from full.golden import golden_references
        freeze = orig(snap, rule, P, D, selection, run)
        for cand in freeze['candidates']:
            # 盘前冻结只能用 P 日及以前的数据；快照日线可能含 D 日行（前视）
            drows = [r for r in (snap.daily.get(cand['code']) or []) if r['date'] <= P]
            if not drows:
                continue
            cand['golden_references'] = golden_references(
                drows, cand['reference_close'], cand['atr14'] or 0.0)
        run.write('freeze', '%s.json' % D, value=freeze)   # orig 先持久化，附加后重写
        return freeze

    replay.run_freeze = wrapped


def _verify_snapshot():
    """正常入口的快照完整性校验：manifest 指纹自洽 + 数据文件哈希匹配。"""
    import gzip
    import hashlib
    mp = SNAPSHOT / 'snapshot_manifest.json'
    if not mp.is_file():
        raise SystemExit('SNAPSHOT_MISSING:' + str(mp))
    man = json.loads(mp.read_text(encoding='utf-8'))
    body = json.dumps({k: v for k, v in man.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    if man.get('snapshot_fingerprint') != hashlib.sha256(body).hexdigest():
        raise SystemExit('SNAPSHOT_FINGERPRINT_MISMATCH')
    for f, want in (man.get('data_sha256') or {}).items():
        p = SNAPSHOT / f
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != want:
            raise SystemExit('SNAPSHOT_DATA_MISMATCH:' + f)
    return man


def cmd_run(a):
    if getattr(a, 'redo_monitor', None):
        if not a.resume:
            print(json.dumps(dict(ok=False, error='REDO_MONITOR_REQUIRES_RESUME:'
                                                ' 缺 --resume 会清空该运行重新开始；'
                                                ' 补数重判必须 --resume --redo-monitor --run-id 三者齐备'),
                             ensure_ascii=False))
            return 2
        if not a.run_id and not os.environ.get('RULES_LAB_RUN_ID_OVERRIDE'):
            print(json.dumps(dict(ok=False, error='REDO_MONITOR_REQUIRES_RUN_ID:'
                                                ' 用 --run-id 或 RULES_LAB_RUN_ID_OVERRIDE 指定目标运行'),
                             ensure_ascii=False))
            return 2
        if a.run_id:
            os.environ['RULES_LAB_RUN_ID_OVERRIDE'] = a.run_id
        _drop_stages(a.days or [], ('MONITOR', 'INDICATOR'))
    _verify_snapshot()
    _attach_golden()
    try:
        rep = replay.run_replay(a.rule, days=a.days or None, cutoff=a.cutoff,
                                restart_check=not a.no_restart_check,
                                fail_on=a.fail_on or None, fresh=not a.resume,
                                crash_points=a.crash_at or None,require_minutes=a.require_minutes)
    except Exception as exc:
        failed = dict(ok=False,error=('%s:%s' % (type(exc).__name__,exc))[:300])
        if a.result is not None:
            a.result.update(failed)
        print(json.dumps(failed,ensure_ascii=False))
        return 1
    integrity = rep.get('integrity') or {}
    if getattr(a, 'with_tarot_shadow', None):
        # 影子开关（默认关）：只追加评分与来源，不影响规则链路产物。
        # 日期以主流程实际运行结果为准（用户未显式给 --days 时 a.days 为空）。
        try:
            from full.tarot_shadow import run_shadow
            runs_root = os.environ.get('RULES_LAB_RUN_ROOT') or str(FE / 'run' / 'state' / 'runs')
            run_dir = Path(runs_root) / rep['run_id']
            for d in (rep.get('days') or []):
                freeze_p = run_dir / 'freeze' / ('%s.json' % d)
                if freeze_p.is_file():
                    shadow = run_shadow(str(freeze_p), str(SNAPSHOT),
                                        run_dir / 'out' / ('tarot_shadow_%s.json' % d))
                    integrity['tarot_shadow'] = dict(
                        day=d, candidates=shadow['summary']['candidates'],
                        scored_any=shadow['summary']['scored_any'],
                        coverage=shadow['summary']['coverage'],
                        status=shadow['summary'].get('status'),
                        source='tarot_shadow/local-frozen-weights')
        except Exception as exc:
            integrity['tarot_shadow'] = dict(error='%s:%s' % (type(exc).__name__, str(exc))[:200])
    summary = dict(ok=True, run_id=rep.get('run_id'), rule_ref=rep.get('rule_ref'),
                          days=rep.get('days'), day_notes=rep.get('day_notes'),
                          inbox_counts=integrity.get('inbox_counts'),
                          tarot_shadow=integrity.get('tarot_shadow'),
                          review_summary=integrity.get('review_summary'),
                          files=len((rep.get('manifest') or {}).get('files', {})))
    if a.result is not None:
        a.result.update(summary)
    print(json.dumps(summary, ensure_ascii=False)[:1800])
    return 0


def _drop_stages(days, stages):
    """把指定阶段从任务日志移除，--resume 时会重跑该阶段（同一账本）。
    重跑监控意味着研究语境变了：一并移除该 run 的最终验证锁，报告阶段会重建。"""
    from rules_lab.paths import run_root, read_json
    from rules_lab import paths as _paths
    runs = _paths.run_root()
    override = os.environ.get('RULES_LAB_RUN_ID_OVERRIDE')
    run_dirs = [runs / override] if override else sorted(runs.glob('*'))
    for d in days:
        for run_dir in run_dirs:
            tasks_dir = run_dir / 'tasks'
            p = tasks_dir / ('%s.json' % d)
            if p.is_file():
                doc = read_json(p)
                doc['completed'] = [s for s in doc.get('completed', []) if s not in stages]
                _paths.atomic_write_json(p, doc)
            if 'MONITOR' in stages:
                for lock in (run_dir / 'report').glob('final_validation_lock*.json'):
                    lock.unlink()


def cmd_select(a):
    snap = open_snapshot(a.cutoff)
    rule = rules.get(a.rule)
    run = _scratch_run('select_%s_%s' % (a.rule.replace('@', '_'), a.day))
    sel = run_selection(snap, rule, int(a.day), run)
    out = dict(day=int(a.day), rule=a.rule,
               accepted_n=len(sel['accepted']), accepted=sorted(sel['accepted']))
    print(json.dumps(out, ensure_ascii=False)[:1500])
    return 0


def _edited_rule(rule_ref, field, op, value):
    import hashlib
    rule = json.loads(json.dumps(rules.get(rule_ref)))   # deep copy from published
    hit = []
    def walk(node):
        if isinstance(node, dict):
            if node.get('field') == field:
                node['op'] = op
                node['value'] = value
                hit.append(1)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(rule.get('selection', {}))
    walk(rule.get('trigger', {}))
    if not hit:
        raise SystemExit('FIELD_NOT_IN_RULE:' + field)
    rule['rule_id'] = rule['rule_id'] + '_edit'
    rule['version'] = 'v1'
    rule['status'] = 'DRAFT'
    rule.pop('sha256', None)
    rule.pop('published_at', None)
    draft_path = FE / 'run' / 'build' / ('%s_%s_draft.json' % (rule['rule_id'], hashlib.sha256(
        json.dumps(rule, sort_keys=True).encode()).hexdigest()[:8]))
    draft_path.parent.mkdir(parents=True, exist_ok=True)
    draft_path.write_text(json.dumps(rule, ensure_ascii=False), encoding='utf-8')
    rules.import_rule(str(draft_path))
    ref = '%s@v1' % rule['rule_id']
    from rules_lab.paths import STATE, read_json as _rj
    target = STATE / 'rules' / 'published' / ('%s.json' % ref)
    if target.is_file():
        existing = _rj(target)
        if existing.get('sha256') != rules.content_sha(rules.prepare(rule, status='PUBLISHED', version='v1')):
            for n in range(2, 10):
                try:
                    p = rules.publish(rule['rule_id'], version='v%d' % n, registered=None)
                    return p['ref']
                except Exception:
                    continue
            raise SystemExit('EDIT_VERSION_EXHAUSTED')
        return ref
    rules.publish(rule['rule_id'], version='v1', registered=None)
    return ref


def cmd_diff_selection(a):
    """验收演示：改一个盘后筛选阈值，候选名单按预期变化（含逐股原因）。"""
    base, base_vals = cmd_select_like(a.rule, a.day, a.cutoff, with_values=a.field)
    ref = _edited_rule(a.rule, a.field, a.op, a.value)
    edit, edit_vals = cmd_select_like(ref, a.day, a.cutoff, with_values=a.field)
    gained = sorted(set(edit) - set(base))
    lost = sorted(set(base) - set(edit))
    print(json.dumps(dict(field=a.field, op=a.op, value=a.value,
                          base_n=len(base), edited_n=len(edit),
                          gained=gained[:20],
                          lost=[dict(code=c, actual=base_vals.get(c)) for c in lost[:20]]),
                     ensure_ascii=False))
    return 0


def cmd_select_like(rule_ref, day, cutoff, with_values=None):
    snap = open_snapshot(cutoff)
    rule = rules.get(rule_ref)
    run = _scratch_run('diff_%s_%s' % (rule_ref.replace('@', '_'), day))
    sel = run_selection(snap, rule, int(day), run)
    vals = {}
    if with_values:
        for code, row in (sel.get('rows') or {}).items():
            for cond in row.get('conditions', []):
                if cond.get('field') == with_values:
                    vals[code] = cond.get('actual')
                    break
    return set(sel['accepted']), vals


def _scratch_run(tag):
    from rules_lab.state_store import RunContext
    import hashlib
    rid = 'fe_' + tag + '_' + hashlib.sha256(tag.encode()).hexdigest()[:8]
    return RunContext(rid, meta=dict(scratch=True))


def cmd_diff_trigger(a):
    """验收演示：改一个盘中触发条件，提醒按预期变化（重跑监控日对比回执）。"""
    _attach_golden()
    rep_base = replay.run_replay(a.rule, days=[a.day], cutoff=None, fresh=True,
                                 compute_report=False)
    ref = _edited_rule(a.rule, a.field, a.op, a.value)
    rep_edit = replay.run_replay(ref, days=[a.day], cutoff=None, fresh=True,
                                 compute_report=False)
    def brief(rep):
        days = (rep.get('days') or {})
        d = days.get(str(a.day)) or days.get(a.day) or {}
        return dict(run_id=rep.get('run_id'), candidates=d.get('candidates'),
                    receipts=d.get('receipts'), resent=d.get('resent'),
                    touch_events=d.get('touch_events'))
    print(json.dumps(dict(base=brief(rep_base), edited=brief(rep_edit)),
                     ensure_ascii=False)[:1500])
    return 0


def cmd_serve(a):
    import full.webapp as webapp
    webapp.main([])
    return 0


def main(argv=None, *, result=None):
    ap = argparse.ArgumentParser(prog='full')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p = sub.add_parser('run', help='全链回放：选股→冻结→监控→复盘')
    p.add_argument('--rule', default='strong_pullback@v1')
    p.add_argument('--days', type=int, nargs='*')
    p.add_argument('--cutoff', type=int)
    p.add_argument('--fail-on', nargs='*')
    p.add_argument('--crash-at', nargs='*', help='中断注入点（验收用，如 AFTER_EVENT_PERSIST）')
    p.add_argument('--no-restart-check', action='store_true')
    p.add_argument('--resume', action='store_true')
    p.add_argument('--redo-monitor', action='store_true',
                   help='把 MONITOR/INDICATOR 阶段从指定 run 的任务日志移除并重判'
                        '（须配合 --run-id；数据补齐后用）')
    p.add_argument('--run-id', default=None, help='指定运行（redo-monitor 必填）')
    p.add_argument('--with-tarot-shadow', action='store_true',
                   help='影子开关（默认关）：对冻结候选追加 Tarot 评分与来源，不参与入池')
    p.add_argument('--require-minutes',action='store_true',help='缺整日分钟归档时返回失败')
    p.set_defaults(fn=cmd_run)
    p = sub.add_parser('select')
    p.add_argument('--rule', default='strong_pullback@v1')
    p.add_argument('--day', required=True, type=int)
    p.add_argument('--cutoff', type=int)
    p.set_defaults(fn=cmd_select)
    p = sub.add_parser('diff-selection')
    p.add_argument('--rule', default='strong_pullback@v1')
    p.add_argument('--day', required=True, type=int)
    p.add_argument('--field', required=True)
    p.add_argument('--op', default='>=')
    p.add_argument('--value', required=True, type=float)
    p.add_argument('--cutoff', type=int)
    p.set_defaults(fn=cmd_diff_selection)
    p = sub.add_parser('diff-trigger')
    p.add_argument('--rule', default='strong_pullback@v1')
    p.add_argument('--day', required=True, type=int)
    p.add_argument('--field', required=True)
    p.add_argument('--op', default='>=')
    p.add_argument('--value', required=True, type=float)
    p.set_defaults(fn=cmd_diff_trigger)
    p = sub.add_parser('serve')
    p.set_defaults(fn=cmd_serve)
    a = ap.parse_args(argv)
    a.result = result
    ensure_dirs()
    return a.fn(a)


if __name__ == '__main__':
    raise SystemExit(main())
