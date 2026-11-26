"""单因子样本外研究：研究区间选阈值/方向，留出与最终区间只跑一次。

工程验收（可回放、可追溯、缺值处理）与收益好坏完全分离；
证据不足时明确写「证据不足」，不把小样本数字包装成结论。
"""
from __future__ import annotations

import json
from pathlib import Path

from .paths import (REPORTS, atomic_write_bytes, atomic_write_json,
                    canonical_sha, read_json, report_root)

DEFAULT_CONFIG = 'research_config_v1_1.json'
REPORT_TAG = 'v1_1'
FINAL_LOCK_FILE = 'final_validation_lock_v1.json'
LOCKED_FINAL_DAYS = [20261110, 20261111]
LAB_LOGICAL_TIME = '2026-11-12T00:00:00+08:00'


def _mean(values):
    vals = [v for v in values if v is not None]
    return sum(vals) / len(vals) if vals else None


def _ranks(values):
    order = sorted(range(len(values)), key=lambda i: values[i])
    out = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _spearman(xs, ys):
    if len(xs) < 3 or len(set(xs)) < 2 or len(set(ys)) < 2:
        return None
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = _mean(rx), _mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx)
    dy = sum((b - my) ** 2 for b in ry)
    return num / (dx * dy) ** 0.5 if dx and dy else None


def load_config(name=DEFAULT_CONFIG):
    path = REPORTS / name
    if not path.is_file():
        cfg = dict(
            contract='RULES_LAB_RESEARCH_CONFIG_V1_1',
            study_id='zone_width_atr_oos_v1_1',
            question='盘前可算的「观察区宽度/ATR」是否改善同一套强势回调规则的后续表现？',
            factor='zone_width_atr', horizon=1,
            baseline='ALL_TRIGGERED_EQUAL_WEIGHT',
            threshold_method='RESEARCH_INTERVAL_MEDIAN',
            split=dict(research_ratio=0.60, holdout_ratio=0.25,
                       final_ratio=0.15),
            min_holdout_events=30, min_holdout_days=5,
            min_final_events=10,
            label_boundary_policy='EXCLUDE_BY_LABEL_END_DAY',
            final_lock_days=list(LOCKED_FINAL_DAYS),
            final_lock_policy=('FINAL_DAYS_PRELOCKED_FOR_NEXT_BATCH;'
                               '新增逐日样本不得改写本锁定区间，'
                               '不得用其调阈值/方向，须另立新锁定文件'),
            notes=('v1.1：按标签结束日剔除跨区间收益；'
                   '同刻量能历史不足 20 日，故首个研究因子改用日线可算的'
                   '观察区宽度/ATR；阈值与方向只在研究区间选择。'))
        atomic_write_json(path, cfg)
    return read_json(path)


def final_lock_provenance(cfg, snap):
    """预锁的血缘：规则引用+规则SHA、配置文件SHA、快照指纹。

    规则从正式规则库读取，配置文件按规范化字节哈希，快照指纹来自快照本身；
    三者任一漂移都会让后续回放的预锁校验失败（fail-closed）。
    """
    from . import rules
    ref = cfg.get('rule_ref') or 'strong_pullback@v1'
    rule = rules.get(ref)
    return dict(
        contract='RULES_LAB_FINAL_LOCK_PROVENANCE_V1',
        rule_ref=ref, rule_sha256=rule['sha256'],
        rule_contract=rule['contract'],
        config_name=DEFAULT_CONFIG, config_sha256=canonical_sha(cfg),
        snapshot_fingerprint=snap.fingerprint,
        engine_version='v1_1')


def load_or_create_final_lock(cfg, snap, *, name=FINAL_LOCK_FILE,
                              directory=None, provenance=None):
    """预锁最终验证区间：文件已存在则逐字段校验，绝不静默改写。

    name/directory 用于单元测试隔离（正式流程用正式名与 REPORTS）。
    provenance 由 build_report 传入，把规则/配置/快照指纹写进锁文件。
    """
    root = Path(directory) if directory is not None else report_root()
    path = root / name
    expected = dict(
        contract='RULES_LAB_FINAL_VALIDATION_LOCK_V1',
        study_id=cfg['study_id'], locked_days=sorted(cfg['final_lock_days']),
        locked_at=LAB_LOGICAL_TIME, policy=cfg['final_lock_policy'],
        snapshot_fingerprint=snap.fingerprint,
        horizon=int(cfg['horizon']), factor=cfg['factor'],
        final_period_usage=cfg.get('final_period_usage',
                                   'REGRESSION_REVIEW_ONLY'),
        reviewed_days=sorted(cfg.get('reviewed_days', [])),
        unseen_final_days=sorted(cfg.get('unseen_final_days', [])),
        unseen_final_status=cfg.get('unseen_final_status',
                                    'NO_UNSEEN_DAYS_IN_CURRENT_SNAPSHOT'),
        provenance=provenance,
        note=(cfg.get('final_lock_note') or
              ('本区间在看到下一批逐日影子样本之前锁定；'
               '后续批次不得回改日期、不得用于阈值或方向选择。')))
    expected['sha256'] = canonical_sha(expected)
    if path.is_file():
        existing = read_json(path)
        for key in ('contract', 'study_id', 'locked_days', 'locked_at',
                    'policy', 'snapshot_fingerprint', 'horizon', 'factor',
                    'final_period_usage', 'reviewed_days',
                    'unseen_final_days', 'unseen_final_status',
                    'provenance', 'sha256'):
            if existing.get(key) != expected.get(key):
                raise ValueError('FINAL_LOCK_MISMATCH_REFUSE_OVERWRITE:' + key)
        return existing
    atomic_write_json(path, expected)
    return expected


def _samples(run):
    p = run.path('out', 'review_samples.jsonl')
    return [json.loads(line) for line in p.read_text(encoding='utf-8').splitlines()
            if line.strip()]


def _periods(samples, cfg):
    days = sorted({s['day'] for s in samples})
    n = len(days)
    r = cfg['split']
    n1 = max(1, int(round(n * r['research_ratio'])))
    n2 = max(1, int(round(n * r['holdout_ratio'])))
    research = set(days[:n1])
    holdout = set(days[n1:n1 + n2])
    final = set(days[n1 + n2:])
    if not holdout:
        holdout = set(days[-1:])
    if not final:
        final = set(days[-1:])
        holdout = holdout - final

    # 预锁最终验证区间：本轮切分不变；下一批新日期隔离为 future，
    # 且 final 不允许随样本增加漂移（否则 fail-closed）。
    locked = {int(d) for d in cfg.get('final_lock_days', [])}
    max_locked = max(locked) if locked else None
    future = {d for d in days if max_locked is not None and d > max_locked}
    research -= future
    holdout -= future
    final -= future
    locked_present = locked & set(days)
    lock_status = 'MATCH'
    if locked and final != locked_present:
        lock_status = 'FINAL_PERIOD_DRIFT_REFUSED_USE_PRELOCKED_DAYS'
        final = set(locked_present)
        research -= final
        holdout -= final
    return dict(research=sorted(research), holdout=sorted(holdout),
                final=sorted(final), all=days, future=sorted(future),
                final_locked_days=sorted(locked_present),
                final_lock_status=lock_status)


def _events(samples, days, horizon, factor, *, label_end_limit=None,
            excluded=None):
    """按标签结束日检查跨区间：持有窗口末端落入后续区间即剔除，不参与收益统计。

    v1 只按样本所属 day 触碰分，导致研究区间末日的样本用到了留出区间首日
    收盘价。v1.1 以 label['window_days'][-1] 为准，末端日 > 本区间最后交易日
    即判 CROSS_PERIOD_LABEL_END 并排除；excluded 收集逐条剔除原因。
    """
    out = []
    for s in samples:
        if s['day'] not in days or not s['triggered']:
            continue
        label = s['labels'].get(str(horizon)) or s['labels'].get(horizon)
        if not label or label.get('outcome_status') != 'COMPLETE':
            continue
        window = label.get('window_days') or []
        end_day = int(window[-1]) if window else None
        if label_end_limit is not None:
            if end_day is None:
                if excluded is not None:
                    excluded.append(dict(day=s['day'], code=s['code'],
                                         reason='MISSING_LABEL_END_DAY',
                                         label_end_day=None))
                continue
            if end_day > int(label_end_limit):
                if excluded is not None:
                    excluded.append(dict(day=s['day'], code=s['code'],
                                         reason='CROSS_PERIOD_LABEL_END',
                                         label_end_day=end_day,
                                         period_last_day=int(label_end_limit)))
                continue
        fv = (s['factor_snapshot'] or {}).get(factor)
        if fv is None or label.get('excess_return') is None:
            continue
        out.append(dict(day=s['day'], code=s['code'], factor=fv,
                        excess=label['excess_return'],
                        chain=label['chain_return'],
                        label_end_day=end_day,
                        tradable=label.get('tradable_pnl'),
                        raw_ratio=label.get('raw_price_ratio')))
    return out


def _median(values):
    vals = sorted(values)
    if not vals:
        return None
    m = len(vals) // 2
    return vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2.0


def _group_stats(events, threshold, direction):
    if direction == 'HIGH_IS_BETTER':
        chosen = [e for e in events if e['factor'] >= threshold]
    else:
        chosen = [e for e in events if e['factor'] < threshold]
    base = _mean([e['excess'] for e in events])
    chosen_mean = _mean([e['excess'] for e in chosen])
    return dict(n_events=len(events), n_chosen=len(chosen),
                threshold=threshold, direction=direction,
                baseline_mean_excess=base, chosen_mean_excess=chosen_mean,
                lift=(chosen_mean - base) if (chosen_mean is not None and base is not None)
                else None,
                baseline_mean_chain=_mean([e['chain'] for e in events]),
                chosen_mean_chain=_mean([e['chain'] for e in chosen]),
                chosen_mean_tradable=_mean([e['tradable'] for e in chosen]),
                raw_price_ratio_mean=_mean([e['raw_ratio'] for e in chosen]))


def _report_names(tag):
    return ('single_factor_oos_%s.json' % tag,
            'single_factor_oos_%s.md' % tag)


def build_report(run, cfg, snap, *, integrity=None, report_tag=REPORT_TAG,
                 output_dir=None, lock_name=None, lock_provenance=False):
    """output_dir / lock_name 仅用于测试探针隔离；正式流程始终写 REPORTS。"""
    samples = _samples(run)
    periods = _periods(samples, cfg)
    factor, horizon = cfg['factor'], int(cfg['horizon'])
    out_root = Path(output_dir) if output_dir is not None else report_root()
    prov = final_lock_provenance(cfg, snap) if lock_provenance else None
    lock = load_or_create_final_lock(cfg, snap, name=lock_name or FINAL_LOCK_FILE,
                                    directory=out_root, provenance=prov)
    excluded = dict(research=[], holdout=[], final=[])

    def _period_events(key):
        days = set(periods[key])
        limit = max(days) if days else None
        return _events(samples, days, horizon, factor,
                       label_end_limit=limit, excluded=excluded[key])

    research = _period_events('research')
    holdout = _period_events('holdout')
    final = _period_events('final')
    excluded_counts = {k: len(v) for k, v in excluded.items()}
    threshold = _median([e['factor'] for e in research])
    hi = _group_stats(research, threshold, 'HIGH_IS_BETTER') if threshold else None
    lo = _group_stats(research, threshold, 'LOW_IS_BETTER') if threshold else None
    if hi and lo and hi['lift'] is not None and lo['lift'] is not None:
        direction = 'HIGH_IS_BETTER' if hi['lift'] >= lo['lift'] else 'LOW_IS_BETTER'
    else:
        direction = 'LOW_IS_BETTER'
    groups = dict(
        research=dict(chosen=_group_stats(research, threshold, direction)
                      if threshold else None,
                      candidate_high=hi, candidate_low=lo,
                      events=len(research)),
        holdout=dict(chosen=_group_stats(holdout, threshold, direction)
                     if threshold else None, events=len(holdout)),
        final=dict(chosen=_group_stats(final, threshold, direction)
                   if threshold else None, events=len(final)),
    )
    ic_by_day = []
    for day in periods['all']:
        day_events = [e for e in research + holdout + final if e['day'] == day]
        if len(day_events) >= 3:
            ic = _spearman([e['factor'] for e in day_events],
                           [e['excess'] for e in day_events])
            if ic is not None:
                ic_by_day.append(dict(day=day, n=len(day_events), rank_ic=ic))
    stability = dict(
        periods_with_events=sum(1 for k in ('research', 'holdout', 'final')
                                if groups[k]['events'] > 0),
        lift_signs={k: (groups[k]['chosen']['lift']
                        if groups[k]['chosen'] else None)
                    for k in ('research', 'holdout', 'final')},
        rank_ic_days=len(ic_by_day),
        mean_rank_ic=_mean([x['rank_ic'] for x in ic_by_day]),
    )
    holdout_days = len({e['day'] for e in holdout})
    failed = []
    if len(holdout) < cfg['min_holdout_events']:
        failed.append('事件数 %d < %d' % (len(holdout), cfg['min_holdout_events']))
    if holdout_days < cfg['min_holdout_days']:
        failed.append('交易日 %d < %d' % (holdout_days, cfg['min_holdout_days']))
    if len(final) < cfg['min_final_events']:
        failed.append('最终区间事件数 %d < %d' % (len(final),
                                               cfg['min_final_events']))
    insufficient = bool(failed)
    verdict = dict(
        evidence_sufficiency='INSUFFICIENT' if insufficient else 'SUFFICIENT',
        reason=('未达门槛：' + '；'.join(failed) + '（留出 %d 条 / %d 天，最终 %d 条）'
                % (len(holdout), holdout_days, len(final)))
        if insufficient else '达到最小样本门槛',
        engineering_acceptance='SEPARATE_FROM_RETURNS',
        statement='收益好坏不作为工程验收条件；本报告只作因子方法与工程链路演示。',
    )
    payload = dict(
        contract='RULES_LAB_SINGLE_FACTOR_OOS_V1',
        study_id=cfg['study_id'], question=cfg['question'],
        rule_ref=run.meta.get('rule_ref'), run_id=run.run_id,
        snapshot=snap.snapshot_ref(),
        config=dict(factor=factor, horizon=horizon, baseline=cfg['baseline'],
                    threshold_method=cfg['threshold_method'],
                    split=cfg['split'], min_holdout_events=cfg['min_holdout_events'],
                    min_holdout_days=cfg['min_holdout_days'],
                    label_boundary_policy=cfg['label_boundary_policy'],
                    final_lock_days=cfg['final_lock_days']),
        periods=periods, threshold=threshold, direction=direction,
        groups=groups, stability=stability, rank_ic_by_day=ic_by_day,
        label_boundary=dict(
            policy='EXCLUDE_BY_LABEL_END_DAY',
            note=('持有窗口末端日 > 本区间最后交易日的样本一律剔除；'
                  'v1 的研究/留出区间曾分别有 22/5 条跨界样本，'
                  '本版按标签结束日检查后剔除，不再使用后续区间收盘价。'),
            excluded_counts=excluded_counts,
            excluded_total=sum(excluded_counts.values()),
            excluded_examples={k: v[:5] for k, v in excluded.items()}),
        final_validation_lock=lock,
        sample_kind_counts=dict(
            triggered=sum(s['triggered'] for s in samples),
            all_samples=len(samples)),
        integrity=integrity or {},
        verdict=verdict,
        limitations=list(snap.limitations) + [
            '标签窗口末端不完整样本已排除但仍会造成末端事件偏少',
            '按标签结束日剔除跨区间样本后，区间边界附近事件数进一步减少',
            '基准为快照 universe 等权 pre_close 链式收益，非全市场指数',
            '单因子、单规则、单一阈值，未做多重检验校正',
            '研究/留出/最终按日期比例切分，非随机；只跑一次，未反复调参',
            '最终验证区间已预锁，下一批逐日影子样本不得回改或用于调参',
        ],
    )
    json_name, md_name = _report_names(report_tag)
    atomic_write_json(out_root / json_name, payload)
    atomic_write_json(run.path('out', json_name), payload)
    text = render_markdown(payload)
    atomic_write_bytes(out_root / md_name, text.encode('utf-8'))
    return payload


def _f(value, digits=4):
    return 'n/a' if value is None else ('%.*f' % (digits, value))


def render_markdown(p):
    lines = [
        '# 单因子样本外复盘报告 · %s' % p['study_id'],
        '',
        '> %s' % p['question'],
        '',
        '## 1. 口径与可复现信息',
        '',
        '- 规则：`%s`；run_id `%s`' % (p['rule_ref'], p['run_id']),
        '- 快照指纹：`%s`' % p['snapshot']['fingerprint'],
        '- 因子 / 持有期 / 基准：`%s` / %s 交易日 / `%s`'
        % (p['config']['factor'], p['config']['horizon'], p['config']['baseline']),
        '- 阈值方法：`%s`（只在研究区间取中位数）；方向在研究区间比较 HIGH/LOW 后选定'
        % p['config']['threshold_method'],
        '- 复算命令：`python -B -X utf8 -m rules_lab.cli report --run-id %s`'
        % p['run_id'],
        '',
        '## 2. 样本区间与覆盖',
        '',
        '| 区间 | 交易日 | 完整标签触发事件 | 跨界剔除 |',
        '| --- | --- | --- | --- |',
    ]
    bnd = p.get('label_boundary') or {}
    for key, label in (('research', '研究区间'), ('holdout', '留出区间'),
                       ('final', '最终未查看区间')):
        lines.append('| %s | %s | %d | %d |' % (
            label, ', '.join(str(x) for x in p['periods'][key]) or '无',
            p['groups'][key]['events'],
            (bnd.get('excluded_counts') or {}).get(key, 0)))
    lines += [
        '',
        '总样本 %d 条，其中触发事件 %d 条。标签不完整（持有窗口跨过快照末日、'
        '缺日线或缺 pre_close）的事件不进入收益统计，但仍留在样本账本。'
        % (p['sample_kind_counts']['all_samples'],
           p['sample_kind_counts']['triggered']),
        '',
        '### 标签边界与最终区间预锁（v1.1）',
        '',
        '- 口径：`%s`；%s' % (bnd.get('policy'),
                             bnd.get('note')),
        '- 按标签结束日剔除跨界样本共 **%d** 条（研究 %d / 留出 %d / 最终 %d）；'
        '剔除后不再使用后续区间的收盘价。'
        % (bnd.get('excluded_total', 0),
           (bnd.get('excluded_counts') or {}).get('research', 0),
           (bnd.get('excluded_counts') or {}).get('holdout', 0),
           (bnd.get('excluded_counts') or {}).get('final', 0)),
        '- 最终验证区间预锁：%s（锁定时间 %s，锁定文件 SHA `%s`）；'
        'final_lock_status=`%s`。'
        % (json.dumps((p.get('final_validation_lock') or {}).get('locked_days'),
                      ensure_ascii=False),
           (p.get('final_validation_lock') or {}).get('locked_at'),
           (p.get('final_validation_lock') or {}).get('sha256'),
           p['periods'].get('final_lock_status')),
        '- 下一批新增交易日：%s；这些日期隔离为 future，不参与本轮统计，'
        '不得回改已锁定区间。'
        % (json.dumps(p['periods'].get('future'), ensure_ascii=False)),
        '',
        '## 3. 研究区间（唯一允许选阈值/方向的区间）',
        '',
        '| 候选方向 | 选中事件数 | 选中组平均超额 | 基线平均超额 | Lift |',
        '| --- | --- | --- | --- | --- |',
    ]
    for key, label in (('candidate_high', 'HIGH_IS_BETTER'),
                       ('candidate_low', 'LOW_IS_BETTER')):
        g = p['groups']['research'][key]
        if g:
            lines.append('| %s | %d | %s | %s | %s |' % (
                label, g['n_chosen'], _f(g['chosen_mean_excess']),
                _f(g['baseline_mean_excess']), _f(g['lift'])))
    lines += [
        '',
        '冻结阈值 = `%s`，冻结方向 = `%s`。' % (_f(p['threshold'], 6), p['direction']),
        '',
        '## 4. 样本外结果',
        '',
        '| 区间 | 事件数 | 选中数 | 基线平均超额 | 选中平均超额 | Lift | 选中平均成交收益 |',
        '| --- | --- | --- | --- | --- | --- | --- |',
    ]
    for key, label in (('research', '研究'), ('holdout', '留出'),
                       ('final', '最终')):
        g = p['groups'][key]['chosen']
        if g:
            lines.append('| %s | %d | %d | %s | %s | %s | %s |' % (
                label, g['n_events'], g['n_chosen'],
                _f(g['baseline_mean_excess']), _f(g['chosen_mean_excess']),
                _f(g['lift']), _f(g['chosen_mean_tradable'])))
        else:
            lines.append('| %s | %d | - | - | - | - | - |'
                         % (label, p['groups'][key]['events']))
    lines += [
        '',
        '### 稳定性',
        '',
        '- 各区间 Lift 符号：%s' % json.dumps(p['stability']['lift_signs'],
                                            ensure_ascii=False),
        '- 可计算 Rank IC 的交易日 %d 天，均值 %s'
        % (p['stability']['rank_ic_days'], _f(p['stability']['mean_rank_ic'])),
        '',
        '## 5. 结论',
        '',
        '- 证据充分性：**%s**（%s）' % (p['verdict']['evidence_sufficiency'],
                                     p['verdict']['reason']),
        '- 工程验收：**%s**。%s' % (p['verdict']['engineering_acceptance'],
                                  p['verdict']['statement']),
        '- 本轮不声称任何策略有效性；收益数字只用于演示单因子复盘管线。',
        '',
        '## 6. 数据限制（逐条保留）',
        '',
    ]
    for item in p['limitations']:
        lines.append('- ' + item)
    it = p['integrity']
    restart = it.get('restart_check') or []
    lines += [
        '',
        '## 7. 回放完整性（摘要；完整字段见 single_factor_oos_v1_1.json）',
        '',
        '- 源码指纹：`%s`' % (it.get('source_fingerprint') or {}).get('sha256'),
        '- 已发布规则：%s' % ', '.join(
            x['ref'] for x in (it.get('rule_index') or {}).get('published', [])),
        '- 模拟收件箱回执：%s' % json.dumps(it.get('inbox_counts'),
                                           ensure_ascii=False),
        '- 重启复核：%d 个交易日，新增触位事件 %d，补发 %d，重复触位 bar 被去重 %d'
        % (len(restart),
           sum(x['new_touch_events'] for x in restart),
           sum(x['resent'] for x in restart),
           sum(x['duplicate_trigger_bars'] for x in restart)),
        '- 复盘样本：%s 条，分类 %s'
        % ((it.get('review_summary') or {}).get('sample_count', 'n/a'),
           json.dumps((it.get('review_summary') or {}).get('sample_kind_counts'),
                      ensure_ascii=False)),
        '',
    ]
    return '\n'.join(lines)
