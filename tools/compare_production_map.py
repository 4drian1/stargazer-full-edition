"""同盘前截点对照：候选版冻结 vs 正式版黄金地图（生产计算，只读）。

口径对齐：as_of = D 日 09:00（盘前），data_date = P。逐股比对：
- 昨收/参考收盘价（候选 reference_close vs 生产 reference_close）
- 黄金档节点集合（候选 golden_lferences vs 生产 node_zones）
- 空档原因（生产 status/reason vs 候选空列表）
结果落盘 reports/production_map_compare_<P>.json。
运行:
  python -m tools.compare_production_map --freeze run/state/runs/<run>/freeze/20261118.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parents[1] / 'stargazer_desktop'))
sys.path.insert(0, str(FE))


def _nodes(zones):
    return sorted(round(float(z.get('node')), 4) for z in (zones or []) if z.get('node'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--freeze', required=True)
    ap.add_argument('--out', default=None)
    ap.add_argument('--limit', type=int, default=None)
    a = ap.parse_args()
    fr = json.loads(Path(a.freeze).read_bytes())
    P = int(fr['observation_day'])
    D = int(fr['day'])
    cache = FE / '_tmp' / 'prod_cache'
    cache.mkdir(parents=True, exist_ok=True)
    import golden_stock_analysis as prod

    rows = []
    cands = fr['candidates']
    if a.limit:
        cands = cands[:a.limit]
    for i, cand in enumerate(cands):
        code = cand['code']
        rec = dict(code=code, candidate_close=cand['reference_close'],
                   candidate_nodes=_nodes(cand.get('golden_references')))
        try:
            r = prod.compute(dict(code=code, as_of='%sT09:00:00+08:00' % D,
                                  data_date=P, cache_dir=str(cache)))
            rec.update(production_status=r.get('status'),
                       production_close=r.get('reference_close'),
                       production_nodes=_nodes(r.get('node_zones')),
                       production_reason=r.get('reason'),
                       production_atr=r.get('atr_ref'))
        except Exception as exc:
            rec.update(production_status='ERROR', production_error='%s:%s' % (type(exc).__name__, str(exc)[:120]))
        rows.append(rec)
        if i % 20 == 0:
            print('compared %d/%d' % (i + 1, len(cands)), flush=True)

    close_match = sum(1 for r in rows if r.get('production_close')
                      and abs(r['production_close'] - r['candidate_close']) < 1e-6)
    node_exact = sum(1 for r in rows if r.get('production_nodes') is not None
                     and r['production_nodes'] == r['candidate_nodes'])
    both_empty = sum(1 for r in rows if not r['candidate_nodes'] and not r.get('production_nodes'))
    from collections import Counter
    status_dist = Counter(r.get('production_status') for r in rows)
    summary = dict(candidates=len(rows), P=P, D=D,
                   close_match=close_match, node_set_exact_match=node_exact,
                   both_empty_nodes=both_empty, production_status=dict(status_dist))
    out = dict(summary=summary, rows=rows)
    out_p = Path(a.out) if a.out else FE / 'reports' / ('production_map_compare_%s.json' % P)
    out_p.parent.mkdir(parents=True, exist_ok=True)
    out_p.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(summary, ensure_ascii=False))
    print('written:', out_p)


if __name__ == '__main__':
    main()
