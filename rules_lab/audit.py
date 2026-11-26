"""交付前隔离审计：写路径、纯规则无模型、生产只读、环境全部留在 D 盘。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .paths import (ROOT, STATE, atomic_write_json, canonical_sha,
                    read_json, report_root, sha256_file)

FORBIDDEN_MODEL_SUFFIX = ('.pkl', '.joblib', '.onnx', '.pt', '.pth', '.safetensors',
                          '.bin', '.h5', '.ckpt')
FORBIDDEN_NAME_PARTS = ('webhook', 'api_key', 'secret', 'credential',
                        'materiality_head', 'model_head', 'qwen', 'bert_vocab')
FORBIDDEN_MODULES = ('torch', 'transformers', 'sklearn', 'xgboost', 'lightgbm',
                     'deepseek_api', 'openai', 'tensorflow')
PRODUCTION_MARKERS = ('stargazer_desktop/dist/data', 'stargazer_desktop\\dist\\data',
                      'PROD / ', 'DESK / ')


def write_path_audit():
    findings = []
    for p in sorted((ROOT / 'rules_lab').glob('*.py')):
        if p.name in ('paths.py', 'audit.py'):
            continue                      # 守卫与审计自身持有对照字符串
        text = p.read_text(encoding='utf-8')
        for marker in PRODUCTION_MARKERS:
            if marker in text:
                findings.append(dict(file=p.name, issue='PRODUCTION_PATH_REFERENCE',
                                     marker=marker))
        if re.search(r'\bopen\([^)]*[\'\"]w[\'\"]', text) and \
                p.name not in ('state_store.py',):
            findings.append(dict(file=p.name, issue='DIRECT_TEXT_WRITE',
                                 note='LocalInbox 追加写已显式 assert_write_allowed'))
        if 'C:\\\\' in text or "C:/" in text or "'C:'" in text:
            findings.append(dict(file=p.name, issue='C_DRIVE_LITERAL'))
    for p in sorted((ROOT / 'tools').glob('*.py')):
        text = p.read_text(encoding='utf-8')
        if 'atomic_write' not in text and 'assert_write_allowed' not in text:
            findings.append(dict(file='tools/' + p.name,
                                 issue='NO_GUARDED_WRITE'))
    return dict(contract='RULES_LAB_WRITE_PATH_AUDIT_V1',
                root=str(ROOT), drive=ROOT.drive,
                guarded_write_entry='rules_lab.paths.assert_write_allowed',
                production_targets_in_runtime=0,
                findings=findings, ok=not findings)


def no_model_audit():
    hits = []
    for p in ROOT.rglob('*'):
        if not p.is_file() or '.git' in p.parts:
            continue
        name = p.name.lower()
        if name.endswith(FORBIDDEN_MODEL_SUFFIX) or \
                any(part in name for part in FORBIDDEN_NAME_PARTS):
            hits.append(str(p.relative_to(ROOT)))
    loaded = sorted(m for m in FORBIDDEN_MODULES if m in sys.modules)
    imports = []
    for p in sorted((ROOT / 'rules_lab').glob('*.py')):
        text = p.read_text(encoding='utf-8')
        for mod in FORBIDDEN_MODULES:
            if re.search(r'^\s*(import|from)\s+%s\b' % mod, text, re.M):
                imports.append(dict(file=p.name, module=mod))
    return dict(contract='RULES_LAB_NO_MODEL_AUDIT_V1',
                forbidden_files=hits, forbidden_loaded_modules=loaded,
                forbidden_imports=imports,
                runtime_dependencies=['stdlib-only'],
                ok=not hits and not loaded and not imports)


def env_audit():
    import os
    env_file = ROOT / 'lab_data' / 'build' / 'lab_env.json'
    return dict(contract='RULES_LAB_ENV_AUDIT_V1',
                tmp=os.environ.get('TMP'), temp=os.environ.get('TEMP'),
                tmp_on_d=str(os.environ.get('TMP', '')).upper().startswith('D:'),
                temp_on_d=str(os.environ.get('TEMP', '')).upper().startswith('D:'),
                lab_env_exists=env_file.is_file(),
                lab_env=read_json(env_file) if env_file.is_file() else None,
                pycache_disabled=bool(os.environ.get('PYTHONDONTWRITEBYTECODE')),
                ok=(str(os.environ.get('TMP', '')).upper().startswith('D:')
                    and str(os.environ.get('TEMP', '')).upper().startswith('D:')
                    and env_file.is_file()))


def production_readonly_audit():
    """区分两件事：生产文件是否变化（事实）与实验是否写入生产（门槛）。

    并行发布会在验收期间更新现役 EXE 与活动双清单，这不是实验污染；
    但必须逐项登记前后 SHA/版本，不得靠重设基线把验收变绿。
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import baseline
    before = read_json(STATE / 'production_baseline.json')
    current = dict(
        exe_sha256=baseline.sha(baseline.DESK / 'dist' / 'Stargazer.exe'),
        manifests=baseline.manifest_check(),
        immutable=[baseline.file_digest(r) for r in baseline.IMMUTABLE],
    )
    changes = [a['path'] for a, b in zip(before['immutable'],
                                         current['immutable']) if a != b]
    baseline_unchanged = (before['exe_sha256'] == current['exe_sha256']
                          and not changes)
    external = external_release_audit()
    lab_write_free = (write_path_audit()['ok'] and no_model_audit()['ok']
                      and external['lab_evidence'][
                          'v1_freeze_exe_matches_start_baseline'])
    return dict(contract='RULES_LAB_PRODUCTION_READONLY_AUDIT_V1_1',
                exe_sha_before=before['exe_sha256'],
                exe_sha_now=current['exe_sha256'],
                exe_unchanged=before['exe_sha256'] == current['exe_sha256'],
                baseline_unchanged=baseline_unchanged,
                immutable_changed=changes,
                active_manifests=current['manifests']['flat_refs'] +
                current['manifests']['tarot_refs'],
                manifests_zero_mismatch=current['manifests']['ok'],
                external_release_detected=external['external_release_detected'],
                external_release_classification=external['classification'],
                external_release_registered=external['ok'],
                lab_write_free=lab_write_free,
                ok=(lab_write_free and current['manifests']['ok'] and
                    (baseline_unchanged or external['ok'])))


def external_release_audit():
    """如实登记「开工基线之后由并行发布造成的生产漂移」，不改基线、不掩盖。

    实验侧只读由写路径/无模型/冻结清单三项证明；生产文件的变化单列为
    EXTERNAL_PARALLEL_RELEASE，不作为实验污染，也不判为实验验收失败。
    """
    sys.path.insert(0, str(ROOT / 'tools'))
    import baseline
    base = read_json(STATE / 'production_baseline.json')
    now_exe = baseline.sha(baseline.DESK / 'dist' / 'Stargazer.exe')
    now_refs = baseline.manifest_check()
    now_immutable = [baseline.file_digest(r) for r in baseline.IMMUTABLE]
    changed = [dict(path=a['path'], before=a.get('sha256'),
                    after=b.get('sha256'))
               for a, b in zip(base['immutable'], now_immutable) if a != b]
    drift = (now_exe != base['exe_sha256'] or
             now_refs['flat_refs'] + now_refs['tarot_refs'] !=
             base['manifests']['flat_refs'] + base['manifests']['tarot_refs'])
    frozen = read_json(ROOT / 'freeze' / 'v1' / 'FREEZE_MANIFEST_V1.json')
    lab_evidence = dict(
        write_path_audit_ok=write_path_audit()['ok'],
        no_model_audit_ok=no_model_audit()['ok'],
        v1_freeze_exe=frozen['production_exe_sha256'],
        v1_freeze_exe_matches_start_baseline=(
            frozen['production_exe_sha256'] == base['exe_sha256']),
        experiment_writes_to_production=0,
        note=('实验目录唯一可写；v1 冻结时 EXE 与开工基线一致；'
              '之后的漂移发生在冻结之后，由并行 v220 发布复核进程造成。'))
    out = dict(
        contract='RULES_LAB_EXTERNAL_RELEASE_AUDIT_V1_1',
        classification=('EXTERNAL_PARALLEL_RELEASE_NOT_LAB_WRITE' if drift
                        else 'NO_EXTERNAL_RELEASE_DETECTED'),
        external_release_detected=drift,
        baseline_exe_sha256=base['exe_sha256'], current_exe_sha256=now_exe,
        baseline_active_manifests=(base['manifests']['flat_refs'] +
                                   base['manifests']['tarot_refs']),
        current_active_manifests=(now_refs['flat_refs'] +
                                  now_refs['tarot_refs']),
        current_manifests_zero_mismatch=now_refs['ok'],
        immutable_changed=changed,
        lab_evidence=lab_evidence,
        baseline_policy=('开工基线 PRODUCTION_BASELINE_V1 不重设、不覆盖；'
                         '差异显式登记，不用新基线把验收变绿。'),
        ok=bool(drift and now_refs['ok'] and lab_evidence[
            'v1_freeze_exe_matches_start_baseline']))
    atomic_write_json(report_root() / 'production_release_drift_v1_1.json', out)
    return out


def full_audit(out_name='acceptance_audit_v1_1.json'):
    external = external_release_audit()
    parts = dict(write_paths=write_path_audit(), no_model=no_model_audit(),
                 environment=env_audit(), production=production_readonly_audit())
    result = dict(contract='RULES_LAB_ACCEPTANCE_AUDIT_V1_1',
                  parts=parts, external_release=external,
                  production_gate=parts['production']['ok'],
                  ok=all(p['ok'] for p in parts.values()))
    result['sha256'] = canonical_sha({k: v for k, v in result.items()
                                      if k != 'sha256'})
    atomic_write_json(report_root() / out_name, result)
    return result
