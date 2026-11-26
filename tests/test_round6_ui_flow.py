"""第六轮验收：规则工作台 UI 全流程（API 级）。

编辑一个盘后条件 + 一个盘中条件 → 保存草稿 → 预览差异 → 发布新版本 →
用新版本跑全链 → 与原版本对照（候选/冻结/提醒回执变化符合预期）。
运行: python tests/test_round6_ui_flow.py
"""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
PY = sys.executable
BASE = 'strong_pullback@v1'
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

RUNNER = r'''
import sys, json
sys.path.insert(0, "."); sys.path.insert(0, "..")
import full.cli as cli
raise SystemExit(cli.main(json.loads(sys.argv[1])))
'''


def run_cli(args, expect_rc=0):
    p = subprocess.run([PY, '-X', 'utf8', '-c', RUNNER, json.dumps(args)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(FE))
    tail = (p.stdout or '')[-200:] + (p.stderr or '')[-200:]
    assert p.returncode == expect_rc, 'rc=%s %s' % (p.returncode, tail)
    return p


def http(path, body=None):
    url = 'http://127.0.0.1:17891' + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data,
                                 headers={'Content-Type': 'application/json'} if data else {})
    with urllib.request.urlopen(req, timeout=900) as r:
        return json.loads(r.read())


def main():
    os.environ['RULES_LAB_RUN_ROOT'] = str(FE / 'run_ui6')
    # 基线：原版本跑一遍（纯净目录）
    run_root = FE / 'run_ui6'
    if run_root.exists():
        import shutil
        shutil.rmtree(run_root)
    run_root.mkdir()
    env = dict(os.environ)
    p = subprocess.run([PY, '-X', 'utf8', '-c', RUNNER,
                        json.dumps(['run', '--rule', BASE, '--days', '20261118'])],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(FE), env=env)
    assert p.returncode == 0, p.stderr[-300:]
    base_sel = json.loads((sorted(run_root.glob('*/out/selection_20261117.json'))[-1]).read_text(encoding='utf-8'))
    base_n = base_sel['accepted_count']
    print('baseline candidates:', base_n)

    # 起 UI
    import full.webapp as webapp
    t = threading.Thread(target=lambda: webapp.create_app().run(port=17891, threaded=True),
                         daemon=True)
    t.start()
    time.sleep(3)

    # 载入规则
    rule = http('/api/rule?ref=' + BASE)['rule']
    assert rule['rule_id'] == 'strong_pullback'

    # 编辑：盘后 ret_60d 0.10→0.20；盘中 decline_from_prev_close 0.035→0.05
    for c in rule['selection']['children']:
        if c['field'] == 'ret_60d':
            c['value'] = 0.20
    for c in rule['trigger']['children']:
        if c['field'] == 'decline_from_prev_close':
            c['value'] = 0.05
    rule['rule_id'] = 'strong_pullback_draft'

    # 草稿
    r = http('/api/rule/draft', dict(rule=rule))
    assert r.get('ok'), r
    print('draft saved')

    # 预览
    pv = http('/api/rule/preview', dict(rule=rule, p_day=20261117, d_day=20261118))
    assert pv['ok'] and pv['edited_n'] < pv['base_n'], pv
    assert len(pv['lost']) > 0 and pv['gained'] == []
    tr = pv.get('trigger') or {}
    assert tr.get('edited', {}).get('receipts'), 'preview missing trigger diff: %s' % tr
    base_rc = tr['base']['receipts'].get('SENT')
    edit_rc = tr['edited']['receipts'].get('SENT')
    assert edit_rc < base_rc, (base_rc, edit_rc)
    print('preview: base %d -> edited %d, lost %d (带字段值); receipts %s -> %s' % (
        pv['base_n'], pv['edited_n'], len(pv['lost']), base_rc, edit_rc))

    # 发布
    pub = http('/api/rule/publish', dict(rule_id='strong_pullback_draft'))
    assert pub.get('ok'), pub
    new_ref = pub['ref']
    print('published:', new_ref)

    # 新版本全链——走 UI 按钮路径（/api/run 只传 rule+days），核对实际 rule_ref
    r = http('/api/run', dict(rule=new_ref, days=[20261118]))
    assert r.get('ok'), r
    new_runs = sorted(run_root.glob('*/run_manifest.json'), key=lambda x: x.stat().st_mtime)
    manifest = json.loads(new_runs[-1].read_text(encoding='utf-8'))
    assert manifest['meta']['rule_ref'] == new_ref, manifest['meta']['rule_ref']
    run_dir = new_runs[-1].parent
    new_sel = json.loads((run_dir / 'out' / 'selection_20261117.json').read_text(encoding='utf-8'))
    assert new_sel['accepted_count'] == pv['edited_n'], (new_sel['accepted_count'], pv['edited_n'])
    assert new_sel['rule_ref'] == new_ref
    print('UI-path run with rule_ref', new_ref, ': candidates == preview:', new_sel['accepted_count'])
    new_mon = json.loads(sorted(run_root.glob('*/out/monitor_20261118.json'), key=lambda x: x.stat().st_mtime)[-1].read_text(encoding='utf-8'))
    base_mon = json.loads(sorted(run_root.glob('*/out/monitor_20261118.json'), key=lambda x: x.stat().st_mtime)[0].read_text(encoding='utf-8'))
    print('receipts base %s -> edited %s' % (base_mon['inbox_counts'], new_mon['inbox_counts']))
    assert new_mon['inbox_counts']['SENT'] < base_mon['inbox_counts']['SENT']

    # 重启找回：发布版本与草稿都能从界面重新打开
    r2 = http('/api/rules')
    assert any('strong_pullback_draft@v' in x for x in r2['published']), r2['published']
    assert 'strong_pullback_draft' in (r2.get('drafts') or []), r2
    draft_back = http('/api/rule?ref=strong_pullback_draft')
    assert draft_back.get('rule') and draft_back.get('draft'), draft_back
    dv = {c['field']: c['value'] for c in draft_back['rule']['selection']['children']}
    assert dv.get('ret_60d') == 0.20, dv
    print('restart recovery: published + draft reloadable, draft ret_60d =', dv.get('ret_60d'))

    # 影子开关：同快照开/关两次，核对只追加评分
    run_shadow_root = FE / 'run_ui6_shadow'
    if run_shadow_root.exists():
        import shutil
        shutil.rmtree(run_shadow_root)
    run_shadow_root.mkdir()
    env3 = dict(env); env3['RULES_LAB_RUN_ROOT'] = str(run_shadow_root)
    p = subprocess.run([PY, '-X', 'utf8', '-c', RUNNER,
                        json.dumps(['run', '--rule', BASE, '--days', '20261118', '--with-tarot-shadow'])],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(FE), env=env3)
    assert p.returncode == 0, p.stdout[-400:] + p.stderr[-200:]
    out = None
    for line in p.stdout.splitlines():
        if line.startswith('{"ok":'):
            out = json.loads(line)
    assert out, p.stdout[-400:]
    ts = (out.get('tarot_shadow') or {})
    plain_sel = json.loads((sorted(run_shadow_root.glob('*/out/selection_20261117.json'))[-1]).read_text(encoding='utf-8'))
    freeze_n = json.loads((sorted(run_shadow_root.glob('*/freeze/20261118.json'))[-1]).read_text(encoding='utf-8'))['candidate_count']
    assert 'candidates' in ts and ts['candidates'] == freeze_n, (ts, freeze_n)
    shadow_run = sorted(run_shadow_root.glob('*/out/tarot_shadow_20261118.json'))
    assert shadow_run, 'shadow output missing (auto-day bug?)'
    assert plain_sel['accepted_count'] == base_n, 'selection changed with shadow on'
    print('shadow on: appended scoring only; selection still', plain_sel['accepted_count'],
          '| shadow:', ts)
    print('UI_FLOW_PASS')


if __name__ == '__main__':
    main()
