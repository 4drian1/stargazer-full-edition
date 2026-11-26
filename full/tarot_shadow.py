"""Tarot 影子评分（模型开关第一版）：固定盘前行情/规则/候选/黄金档，
只对同一批冻结候选追加 Tarot 分数，不参与入池。

- 关闭（默认）：主链路不加载权重，本模块不运行；
- 打开：逐股记录 分数 / 模型版本 / 未评分原因（未准入/缺特征/推理报错）；
- 复用生产评分管线 tarot_live_v2.build（冻结权重），不自行实现打分。
输出 reports/tarot_shadow_<P>.json（逐股对照表 + 排序/覆盖摘要）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PROFILE = FE.parents[1] / 'stargazer_ml' / 'v5_hybrid' / 'green_common_profile'


def _prep_daily_parquet(snapshot_dir: Path, out_dir: Path, names: dict, p_day: int):
    import gzip
    import pandas as pd
    d = json.loads(gzip.decompress((snapshot_dir / 'daily.json.gz').read_bytes()))
    cols = d['columns']
    frames = []
    for code, rows in d['codes'].items():
        kept = [r for r in rows if r[0] <= p_day]
        if len(kept) < 70:
            continue
        df = pd.DataFrame(kept, columns=cols)
        df['code'] = code
        df['name'] = names.get(code, code)
        frames.append(df[['code', 'name', 'date', 'open', 'high', 'low', 'close',
                          'volume', 'amount', 'pre_close', 'is_st', 'float_mv']])
    out_dir.mkdir(parents=True, exist_ok=True)
    pd.concat(frames, ignore_index=True).to_parquet(out_dir / 'part_000.parquet', index=False)
    return len(frames)


def run_shadow(freeze_path: Path, snapshot_dir: Path, out_path: Path):
    sys.path.insert(0, str(PROFILE))
    sys.path.insert(0, str(FE.parents[1] / 'stargazer_desktop'))
    import tarot_live_v2 as tarot

    fr = json.loads(Path(freeze_path).read_bytes())
    P = int(fr['observation_day'])
    from full.vipdoc_reader import stock_names
    names = stock_names()
    daily_dir = FE / '_tmp' / 'tarot_daily' / str(P)
    n_codes = _prep_daily_parquet(Path(snapshot_dir), daily_dir, names, P)
    print('daily parquet codes:', n_codes)
    # 生产目录的完成标记：build 的输入指纹要读它（须在 build 之前写好）
    import hashlib as _hl
    _parts0 = sorted(daily_dir.glob('part_*.parquet'))
    assert _parts0, 'drill parquet missing'
    marker = dict(contract='TDX_NATIVE_POSTCLOSE_V1',
                  files={p.name: _hl.sha256(p.read_bytes()).hexdigest() for p in _parts0},
                  note='drill parquet built from frozen snapshot p=%s' % P)
    (daily_dir / 'COMPLETE.json').write_text(json.dumps(marker, ensure_ascii=False), encoding='utf-8')

    # 显式读冻结快照并校验：快照指纹必须自洽，且评分管线必须消费我们准备的 parquet。
    man = json.loads((Path(snapshot_dir) / 'snapshot_manifest.json').read_text(encoding='utf-8'))
    import hashlib as _hl
    body = json.dumps({k: v for k, v in man.items() if k != 'snapshot_fingerprint'},
                      sort_keys=True, ensure_ascii=False).encode()
    assert man['snapshot_fingerprint'] == _hl.sha256(body).hexdigest(), 'SNAPSHOT_FINGERPRINT_MISMATCH'
    assert man['p_day'] == P, 'snapshot p_day != freeze observation_day'

    # build() 在函数内 `from tdx_daily_source_v2 import daily_directory`，
    # 必须补丁源模块属性；同时记录调用，事后校验消费的是演练目录。
    import tdx_daily_source_v2 as _td
    calls = []
    def _drill_dir(root, p):
        calls.append(str(daily_dir))
        return daily_dir
    _td.daily_directory = _drill_dir
    prod = tarot.build(str(daily_dir), through=P)   # 生产入口：截断后的正式名单（对照用）
    assert calls and calls[0] == str(daily_dir), 'build did not consume drill parquet: %s' % calls[:2]
    parts = sorted(daily_dir.glob('part_*.parquet'))
    assert parts, 'drill parquet missing'

    # 全量评分：build() 返回的 rows 截断到前十（candidate_population_count 只给数量），
    # 逐股完整评分必须本地复算同一管线（冻结权重/同一函数），并与其前十逐股对齐。
    import numpy as np
    import pandas as pd
    import pickle
    from gcp_demo_pools import process_latest_code
    from descending_tops_v1 import for_frame as _desc

    core = tarot.frozen()
    manifest = tarot.verified_manifest(core)
    model_rel = tarot.MODEL.resolve().relative_to(tarot._project_root().resolve()).as_posix()
    model_sha = manifest['frozen_inputs'][model_rel]
    with tarot.MODEL.open('rb') as src:
        model = pickle.load(src)
    fields = ['code', 'name', *core.NUMERIC]

    rows_full, manual_full, failures_full = [], [], []
    structure_excluded = 0
    for code, g in tarot.daily_groups(parts, fields, P):
        g = g.reset_index(drop=True)
        if len(g) < 70 or int(g.date.iloc[-1]) != P or not str(code).startswith(('00', '30', '60', '68')):
            continue
        if (not np.isfinite(g[['open', 'high', 'low', 'close', 'volume']].to_numpy(dtype=float)).all()
                or g[['open', 'high', 'low', 'close']].le(0).any().any()
                or (g.low > g[['open', 'close']].min(axis=1)).any()
                or (g.high < g[['open', 'close']].max(axis=1)).any()):
            failures_full.append(dict(code=str(code), reason='INVALID_CAUSAL_DAILY_VALUES'))
            continue
        last = g.iloc[-1]
        if not np.isfinite(last[['pre_close', 'is_st', 'float_mv', 'amount']].to_numpy(dtype=float)).all():
            failures_full.append(dict(code=str(code), reason='TDX_CURRENT_METADATA_UNKNOWN'))
            continue
        descending = _desc(g)
        if descending['excluded']:
            structure_excluded += 1
            continue
        if not (last.close < last.pre_close and last.is_st == 0 and last.float_mv > 0
                and last.amount >= 5e7 and last.volume > 0):
            continue
        a, _ = process_latest_code(g)
        rec = tarot.latest_record(g, core, a.to_dict('records'))
        review_only = rec is None
        if review_only:
            from tarot_manual_review_v1 import latest_record as near_miss
            rec = near_miss(g, core, a.to_dict('records'), tarot.trading_calendar())
        if rec is None:
            continue
        features = pd.DataFrame([{k: rec[k] for k in core.FEATURES}])
        if np.isinf(features.to_numpy(dtype=float)).any():
            failures_full.append(dict(code=str(code), reason='INFINITE_FEATURE'))
            continue
        score = float(model.predict(features)[0])
        if not np.isfinite(score):
            raise ValueError('NONFINITE_MODEL_SCORE')
        try:
            row = tarot.row_for(g, rec, core, score, model_sha, policy_v3=False)
            (manual_full if review_only else rows_full).append(row)
        except ValueError as error:
            if str(error) != 'NO_CAUSAL_WAVE_MAP':
                raise
            failures_full.append(dict(code=str(code), reason=str(error)))
    rows_full.sort(key=lambda r: (-r['selection_score'], r['code']))
    manual_full.sort(key=lambda r: (-r['selection_score'], r['code']))

    # 与生产入口逐股对齐（前十）：证明本地复算没有漂移
    prod_top = [(r['code'], round(float(r['selection_score']), 12)) for r in (prod.get('rows') or [])]
    ours_top = [(r['code'], round(float(r['selection_score']), 12)) for r in rows_full[:len(prod_top)]]
    assert ours_top == prod_top, 'PARITY_MISMATCH with production top:\n%s\n%s' % (ours_top, prod_top)
    assert prod.get('candidate_population_count') == len(rows_full), \
        'population count mismatch: %s vs %s' % (prod.get('candidate_population_count'), len(rows_full))
    # 验收指标降级为记录（正常入口不应因零准入/零复核而失败）；
    # 界面按日显示：评分完成 / 有效零结果 / 失败原因。
    zero_admission = len(rows_full) == 0
    summary_acceptance = dict(
        universe_scored=len(rows_full), universe_manual=len(manual_full),
        universe_failures=len(failures_full), universe_structure_excluded=structure_excluded,
        zero_admission=zero_admission,
        status=('有效零结果：生产管线当日无准入' if zero_admission and not manual_full
                else '评分完成' if rows_full else '零准入（有人工复核）' if manual_full else '零准入零复核'),
    )

    rows = rows_full
    manual = manual_full
    failures = failures_full
    scored = {r['code']: r for r in rows}
    review = {r['code']: r for r in manual}
    fail_by = {f['code']: f for f in failures}
    mi = manifest.get('frozen_inputs', {})
    model_keys = [k for k in mi if k.endswith('model.pkl')]
    model_versions = {k: mi[k] for k in model_keys}

    table = []
    top10_codes = {r['code'] for r in rows[:10]}
    # 未被 build 收录的候选：按生产门槛顺序逐股诊断真实原因
    import numpy as np
    import pandas as pd
    from gcp_demo_pools import process_latest_code
    from descending_tops_v1 import for_frame as _desc
    daily = pd.read_parquet(daily_dir / 'part_000.parquet')
    core = tarot.frozen()

    def diagnose(code):
        g = daily[daily.code == code].sort_values('date').reset_index(drop=True)
        if len(g) < 70 or int(g.date.iloc[-1]) != P:
            return 'HISTORY_INSUFFICIENT_OR_STALE'
        if (not np.isfinite(g[['open', 'high', 'low', 'close', 'volume']].to_numpy(dtype=float)).all()
                or g[['open', 'high', 'low', 'close']].le(0).any().any()
                or (g.low > g[['open', 'close']].min(axis=1)).any()
                or (g.high < g[['open', 'close']].max(axis=1)).any()):
            return 'INVALID_CAUSAL_DAILY_VALUES'
        last = g.iloc[-1]
        if not np.isfinite(last[['pre_close', 'is_st', 'float_mv', 'amount']].to_numpy(dtype=float)).all():
            return 'TDX_CURRENT_METADATA_UNKNOWN'
        if _desc(g)['excluded']:
            return '未准入：下降三重顶结构否决（≥3 个下降主peak）'
        if not last.close < last.pre_close:
            return 'NOT_DOWN_DAY(门槛：P 日为下跌日)'
        if last.is_st != 0:
            return 'IS_ST'
        if last.float_mv <= 0:
            return 'FLOAT_MV_UNKNOWN'
        if last.amount < 5e7:
            return 'AMOUNT_BELOW_5E7'
        if last.volume <= 0:
            return 'NO_VOLUME'
        a, _ = process_latest_code(g)
        rec = tarot.latest_record(g, core, a.to_dict('records'))
        if rec is None:
            from tarot_manual_review_v1 import latest_record as near_miss
            rec = near_miss(g, core, a.to_dict('records'), tarot.trading_calendar())
            if rec is None:
                return 'NO_RECORD_FROM_LATEST(特征未形成)'
        features = pd.DataFrame([{k: rec[k] for k in core.FEATURES}])
        if np.isinf(features.to_numpy(dtype=float)).any():
            return 'INFINITE_FEATURE'
        return fail_by.get(code, {}).get('reason', 'NO_CAUSAL_WAVE_MAP(黄金因果图未确认)')

    for cand in fr['candidates']:
        code = cand['code']
        rec = dict(code=code, zone_lower=cand['zone_lower'], zone_upper=cand['zone_upper'],
                   golden_nodes=[g['node'] for g in (cand.get('golden_references') or [])])
        if code in scored:
            r = scored[code]
            if code in top10_codes:
                rec.update(arm='正式入选(TOP10)', tarot_rank=rows.index(r) + 1)
            else:
                rec.update(arm='已评分未进前十', tarot_rank=rows.index(r) + 1)
            rec.update(tarot_score=r['selection_score'], tarot_status=r.get('status'),
                       model_sha256=r['selection_model'].get('model_sha256'))
        elif code in review:
            r = review[code]
            rec.update(arm='人工复核', tarot_score=r.get('personal_score'),
                       tarot_status='OUTSIDE_FROZEN_ADMISSION', reason='未准入：模型分超出冻结适用范围，仅供人工复核',
                       model_sha256=(r.get('selection_model') or {}).get('model_sha256'))
        else:
            rec.update(arm='未准入', tarot_score=None, reason=diagnose(code))
        table.append(rec)

    scored_rows = [t for t in table if t['tarot_score'] is not None]
    scored_rows.sort(key=lambda t: -t['tarot_score'])
    summary = dict(
        candidates=len(table),
        acceptance=summary_acceptance,
        universe_scored=len(rows_full),
        universe_manual=len(manual_full),
        universe_failures=len(failures_full),
        universe_structure_excluded=structure_excluded,
        scored_any=len(scored_rows),
        arm_top10=sum(1 for t in table if t['arm'] == '正式入选(TOP10)'),
        arm_scored_beyond_top10=sum(1 for t in table if t['arm'] == '已评分未进前十'),
        arm_manual_review=sum(1 for t in table if t['arm'] == '人工复核'),
        arm_not_admitted=sum(1 for t in table if t['arm'] == '未准入'),
        coverage=round(len(scored_rows) / len(table), 4),
        model_sha256=model_versions,
        scored_ranking=[(t['code'], t['tarot_score']) for t in scored_rows],
        not_admitted_reasons={},
    )
    from collections import Counter
    summary['not_admitted_reasons'] = dict(Counter(t.get('reason') for t in table if t['arm'] == '未准入'))
    out = dict(summary=summary, table=table)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False)[:1200])
    print('written:', out_path)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freeze', required=True)
    ap.add_argument('--snapshot', default=str(FE / 'snapshot'))
    ap.add_argument('--out', default=None)
    a = ap.parse_args()
    P = json.loads(Path(a.freeze).read_bytes())['observation_day']
    out = Path(a.out) if a.out else FE / 'reports' / ('tarot_shadow_%s.json' % P)
    run_shadow(a.freeze, a.snapshot, out)


if __name__ == '__main__':
    main()
