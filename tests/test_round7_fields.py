"""第七轮验收：新指标目录 + 「周期→指标→条件」工作台。

用例：日K 均线筛选（ret_60d>=0.10 且 close_above_ma20==1 且 dist_60h>=-0.45）
+ 5 分钟监控（decline_from_prev_close>=0.035 且 m5_rsi6<30），
m5_macd_dif 设为「随提醒显示」。
走 编辑→预览→发布→运行，逐笔核对：触发当时值、阈值、K线时间、缺数原因；
并守住版本绑定与草稿续编。
运行: python tests/test_round7_fields.py
"""
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PY = sys.executable
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

RUNNER = r'''
import sys, json
sys.path.insert(0, "."); sys.path.insert(0, "..")
import full.cli as cli
raise SystemExit(cli.main(json.loads(sys.argv[1])))
'''


def run_cli(args, env, expect_rc=0):
    p = subprocess.run([PY, '-X', 'utf8', '-c', RUNNER, json.dumps(args)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(FE), env=env)
    tail = (p.stdout or '')[-200:] + (p.stderr or '')[-200:]
    assert p.returncode == expect_rc, 'rc=%s %s' % (p.returncode, tail)
    return p


def http(path, body=None, port=17891):
    url = 'http://127.0.0.1:%d' % port + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=1200) as r:
        return json.loads(r.read())


def main():
    # 0) 指标计算单测（本地，直接）
    from rules_lab.factors import _rsi, _macd, _aggregate, compute_trigger_factors
    assert _rsi(list(range(1, 12)), 6) == 100.0 and _rsi(list(range(11, 0, -1)), 6) == 0.0
    assert _aggregate([1, 2, 3, 4, 5], 3) == [3]
    dif, dea, hist = _macd([10 + 0.1 * ((i % 7) - 3) for i in range(40)])
    assert dif is not None and isinstance(hist, float)
    print('factor unit checks OK (rsi/macd/aggregate)')

    run_root = FE / 'run_r7'
    if run_root.exists():
        shutil.rmtree(run_root)
    run_root.mkdir()
    os.environ['RULES_LAB_RUN_ROOT'] = str(run_root)
    env = dict(os.environ)

    # 1) UI：载入 → 改规则（新字段 + 随提醒显示）→ 预览 → 发布
    import full.webapp as webapp
    t = threading.Thread(target=lambda: webapp.create_app().run(port=17891, threaded=True),
                         daemon=True)
    t.start()
    time.sleep(3)
    factors = http('/api/factors')
    ui = factors['ui']
    for k in ('m5_rsi6', 'm5_macd_dif', 'close_above_ma20', 'dist_60h',
              'same_time_vol_ratio', 'dist_to_zone_upper_pct'):
        assert k in ui, k
    assert '距近60日最高价' in ui['dist_60h']['label']
    assert '近似换手' in ui['daily_turnover_proxy']['label']
    assert '口径B' in ui['same_time_vol_ratio']['label']
    print('UI catalog OK:', len(ui), 'fields')

    rule = http('/api/rule?ref=strong_pullback@v1')['rule']
    rule['selection']['children'] = [
        dict(field='ret_60d', op='>=', value=0.10),
        dict(field='close_above_ma20', op='==', value=1),
        dict(field='dist_60h', op='>=', value=-0.45),
        dict(field='atr14', op='>', value=0.0),
        dict(field='vol_ratio_5d', op='>=', value=0.8),
        dict(field='vol_ratio_5d', op='<=', value=3.0),
    ]
    rule['trigger']['children'] = [
        dict(field='quote_fresh', op='==', value=1),
        dict(field='decline_from_prev_close', op='>=', value=0.035),
        dict(field='m5_rsi6', op='<', value=30),
    ]
    rule['notification']['attach_fields'] = ['m5_macd_dif']
    rule['rule_id'] = 'r7_ma_rsi'
    r = http('/api/rule/draft', dict(rule=rule))
    assert r.get('ok'), r
    pv = http('/api/rule/preview', dict(rule=rule, p_day=20261117, d_day=20261118))
    assert pv['ok'], pv
    tr = pv.get('trigger') or {}
    assert tr.get('edited', {}).get('receipts'), 'preview trigger diff missing'
    print('preview: candidates %d->%d, receipts %s->%s' % (
        pv['base_n'], pv['edited_n'], tr['base']['receipts'], tr['edited']['receipts']))
    pub = http('/api/rule/publish', dict(rule_id='r7_ma_rsi'))
    assert pub.get('ok'), pub
    ref = pub['ref']

    # 2) 发布版全链（按钮路径）
    r = http('/api/run', dict(rule=ref, days=[20261118]))
    assert r.get('ok'), r
    runs = sorted(run_root.glob('*/run_manifest.json'), key=lambda x: x.stat().st_mtime)
    run_dir = runs[-1].parent
    manifest = json.loads(runs[-1].read_text(encoding='utf-8'))
    assert manifest['meta']['rule_ref'] == ref
    mon = json.loads((run_dir / 'out' / 'monitor_20261118.json').read_text(encoding='utf-8'))
    events = json.loads((run_dir / 'events' / '20261118.json').read_text(encoding='utf-8'))
    evs = events['events']
    assert mon['counts']['touch_events'] == len(evs) > 0
    print('run:', ref, '| touches:', mon['counts']['touch_events'],
          '| receipts:', mon.get('inbox_counts'))

    # 3) 逐笔核对：当时值、阈值、K线时间、随提醒显示
    receipts = [json.loads(l) for l in
                (run_dir / 'inbox' / 'receipts.jsonl').read_text(encoding='utf-8').splitlines()]
    supplements = [r for r in receipts if r['kind'] == 'INDICATOR_SUPPLEMENT']
    touches = [e for e in evs.values()]
    for e in touches:
        fv = e.get('factor_values') or {}
        assert 'm5_rsi6' in fv, 'event missing m5_rsi6'
        assert e.get('bar', {}).get('ts'), 'event missing bar ts'
        assert e.get('conditions') or e.get('reasons') is not None
    hit = [e for e in touches if (e.get('factor_values') or {}).get('m5_rsi6') is not None]
    assert hit, 'no event carries rsi value'
    sample = hit[0]
    print('sample event %s bar=%s close=%s rsi6=%s decline=%.4f (阈值 rsi<30, decl>=0.035)' % (
        sample['code'], sample['bar']['ts'], sample.get('bar_close'),
        (sample.get('factor_values') or {}).get('m5_rsi6'),
        (sample.get('factor_values') or {}).get('decline_from_prev_close') or 0))
    assert (sample.get('factor_values') or {}).get('m5_rsi6') < 30
    assert supplements, 'indicator supplement receipts missing'
    attach_found = any('attach_fields' in (s.get('body') or {}).get('extra', {}) for s in supplements)
    assert attach_found, 'attach_fields (随提醒显示) missing in supplements'
    af = [s for s in supplements if 'attach_fields' in (s.get('body') or {}).get('extra', {})][0]
    print('attach_fields in supplement:', af['body']['extra']['attach_fields'])
    # 同刻量比 B：10 日分钟历史已在快照内，应可计算（>0）
    stv = [(e['code'], (e.get('factor_values') or {}).get('same_time_vol_ratio')) for e in hit[:5]]
    assert all(v is not None and v > 0 for _, v in stv), stv
    print('same_time_vol_ratio 可计算 OK:', [v for _, v in stv[:3]])
    # 历史不足时的具体原因（去掉历史 → LOOKBACK_INSUFFICIENT）
    from rules_lab.factors import compute_trigger_factors as ctf
    ctx0 = dict(day=20261118, asof_ts=20261118093500, max_age_seconds=300,
                zone_lower=7.0, zone_upper=7.4, atr14=0.5, pre_close=7.7, tolerance_atr=0.2,
                prev_bar=None, completed_volumes=[100], bar_count=1,
                today_bars=[(20261118093500, 7.5, 120)], hist_minutes=[],
                cum_amount_so_far=1e8, float_mv=4e9)
    f0 = ctf(dict(ts=20261118093500, close=7.5, low=7.4, high=7.6, volume=120, amount=5e7), ctx0)
    assert f0['same_time_vol_ratio'] is None
    assert 'LOOKBACK_INSUFFICIENT' in str(f0['__agg__'].get('same_time_vol_ratio_reason'))
    # 缺桶诊断：抽掉 10:00 的一根 → 该桶及之后不参与 15 分钟指标
    bars10 = [(20261118000000 + 935 * 100 + i * 500, 10 + 0.01 * i, 100) for i in range(6)]
    bars10 = [(20261118093500 + (i // 3) * 1000 + (i % 3) * 500 if i < 3 else
               20261118100500 + ((i - 3) // 3) * 1000 + ((i - 3) % 3) * 500, 10 + 0.01 * i, 100)
              for i in range(6)]
    # 直接造网格 0..5 缺位置 4（10:00 缺一根）
    bars_missing = [(20261118093500, 10.0, 100), (20261118094000, 10.01, 100),
                    (20261118094500, 10.02, 100), (20261118095000, 10.03, 100),
                    (20261118095500, 10.04, 100), (20261118100500, 10.05, 100)]
    from rules_lab.factors import _bucketize
    closes15, miss15 = _bucketize([(t, c) for t, c, _v in bars_missing], 3)
    assert miss15 == [1] and len(closes15) == 1, (closes15, miss15)
    print('缺桶诊断 OK: missing bucket', miss15, '| closes truncated to', len(closes15))

    # 4) 守住：草稿续编 + 版本绑定
    draft_back = http('/api/rule?ref=r7_ma_rsi')
    assert draft_back.get('draft') and draft_back['rule']['trigger']['children'], draft_back
    base_fr = http('/api/rule?ref=strong_pullback@v1')['rule']
    assert base_fr['sha256'] == 'e7550f4427acf807694d3bea483abbc2d7813d0249dc535d545f7457010d4e84'
    print('draft reloadable; base rule sha unchanged (frozen plans bound)')
    print('ROUND7_PASS')


if __name__ == '__main__':
    main()
