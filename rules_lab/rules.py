"""可编辑规则的版本库：草稿 → 发布（不可变）→ 按 id@version 回放。

规则定义与表单/导入/导出共用同一份 JSON 结构：
  - 表单新建条件、改阈值、组合已有因子，只改 state/rules/drafts/*.json；
  - 发布生成 state/rules/published/<id>@<version>.json，同名拒绝覆盖；
  - 冻结任务只引用 id@version + sha256，旧版本可随时重放。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from pathlib import Path

from . import contract
from .paths import ROOT, RULES, STATE, atomic_write_json, canonical_bytes, read_json

LAB_LOGICAL_TIME = '2026-11-12T00:00:00+08:00'


def rule_root():
    """规则库根：默认正式 lab_data/state/rules，测试可重定向到临时目录。"""
    override = os.environ.get('RULES_LAB_RULE_ROOT')
    return Path(override) if override else STATE / 'rules'


def _drafts():
    return rule_root() / 'drafts'


def _published():
    return rule_root() / 'published'


def _index_path():
    return rule_root() / 'index.json'


class RuleError(ValueError):
    pass


def _public(rule):
    value = {k: v for k, v in rule.items() if k != 'sha256'}
    return value


def content_sha(rule):
    return hashlib.sha256(canonical_bytes(_public(rule))).hexdigest()


def _write_draft(rule):
    path = _drafts() / ('%s_%s.json' % (rule['rule_id'], content_sha(rule)[:12]))
    atomic_write_json(path, rule)
    return path


def _read_index():
    if _index_path().is_file():
        return read_json(_index_path())
    return dict(contract='RULES_LAB_RULE_INDEX_V1', latest={}, drafts={})


def _write_index(index):
    atomic_write_json(_index_path(), index)


def source_fingerprint():
    """实验运行时源码指纹：用于证明「新增规则不改代码」。"""
    out = []
    for p in sorted((ROOT / 'rules_lab').glob('*.py')):
        out.append([p.name, hashlib.sha256(p.read_bytes()).hexdigest()])
    return dict(contract='RULES_LAB_SOURCE_FINGERPRINT_V1',
                files=out,
                sha256=hashlib.sha256(canonical_bytes(out)).hexdigest())


def validate_factor_refs(rule, registered):
    for ref in rule['factor_refs']:
        if not isinstance(ref, str) or '@' not in ref:
            raise RuleError('BAD_FACTOR_REF:' + str(ref))
        name, version = ref.rsplit('@', 1)
        if name not in registered:
            raise RuleError('UNKNOWN_FACTOR:' + name)
        if registered[name].version != version:
            raise RuleError('FACTOR_VERSION_MISMATCH:%s@%s' % (name, version))
    return True


def prepare(rule, *, status='DRAFT', version=None):
    rule = copy.deepcopy(rule)
    rule.pop('sha256', None)
    rule['contract'] = contract.RULE_CONTRACT
    rule['status'] = status
    if version:
        rule['version'] = version
    rule.setdefault('created_at', LAB_LOGICAL_TIME)
    rule.setdefault('notes', '')
    contract.validate_rule(rule)
    return rule


def import_rule(path, *, version=None):
    """导入外部规则 JSON（与表单导出格式相同），只产生草稿。"""
    p = Path(path)
    if not p.is_file():
        raise RuleError('RULE_FILE_MISSING:' + str(p))
    raw = json.loads(p.read_bytes().decode('utf-8'))
    if isinstance(raw, dict) and 'rule' in raw:
        raw = raw['rule']
    draft = prepare(raw, status='DRAFT', version=version)
    written = _write_draft(draft)
    index = _read_index()
    index.setdefault('drafts', {})[draft['rule_id']] = str(written.relative_to(ROOT))
    _write_index(index)
    return dict(draft=str(written), rule_id=draft['rule_id'], sha256=content_sha(draft))


def build_from_form(payload):
    """表单新建：rows 为 [{field, op, value}]，logic 为 AND/OR。"""
    rid = str(payload.get('rule_id') or '').strip()
    rows = payload.get('rows') or []
    if not rows:
        raise RuleError('FORM_NO_CONDITIONS')
    condition = dict(op=str(payload.get('logic') or 'AND').upper(),
                     children=[dict(op=r['op'], field=r['field'], value=r['value'])
                               for r in rows])
    rule = prepare(dict(
        rule_id=rid, version=str(payload.get('version') or 'v1'),
        title=str(payload.get('title') or rid), status='DRAFT',
        scope=dict(universe='SNAPSHOT_MINUTE_CODES', exclude_st=True,
                   board_source='boards@snapshot'),
        evaluation_time='POST_CLOSE_P',
        factor_refs=list(payload.get('factor_refs') or []),
        selection=condition,
        zone=dict(type='ATR_BAND', k_lower=1.0, k_upper=0.6, tolerance_atr=0.2),
        trigger=dict(op='AND', children=[
            dict(field='quote_fresh', op='==', value=1),
            dict(field='price_in_zone', op='==', value=1)]),
        events=dict(touch=dict(enabled=True, kind='TOUCH'),
                    indicator_supplement=dict(enabled=True, period_minutes=15,
                                              kind='INDICATOR_SUPPLEMENT')),
        notification=dict(channel='local_inbox',
                          address='lab_data/state/inbox/inbox.jsonl',
                          dedupe='event_id+kind'),
        execution=dict(fee_rate=0.0003, slippage_rate=0.001,
                       fill='NEXT_COMPLETED_BAR', t_plus_1=True),
        notes=str(payload.get('notes') or 'CREATED_BY_FORM'),
    ))
    written = _write_draft(rule)
    index = _read_index()
    index.setdefault('drafts', {})[rid] = str(written.relative_to(ROOT))
    _write_index(index)
    return dict(draft=str(written), rule_id=rid, sha256=content_sha(rule))


def _set_path(obj, dotted, value):
    parts = dotted.split('.')
    cur = obj
    for part in parts[:-1]:
        cur = cur[int(part)] if isinstance(cur, list) else cur[part]
    last = parts[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def edit(rule_id, changes, *, base_ref=None):
    """改阈值/改条件：从已发布版本或草稿派生新草稿，不改任何源码。"""
    if not changes:
        raise RuleError('NO_CHANGES')
    if base_ref:
        base = get(base_ref)
    else:
        index = _read_index()
        ref = index.get('drafts', {}).get(rule_id)
        base = read_json(ROOT / ref) if ref else get(rule_id)
    draft = prepare(base, status='DRAFT')
    for dotted, value in changes.items():
        _set_path(draft, dotted, value)
    contract.validate_rule(draft)
    written = _write_draft(draft)
    index = _read_index()
    index.setdefault('drafts', {})[rule_id] = str(written.relative_to(ROOT))
    _write_index(index)
    return dict(draft=str(written), rule_id=rule_id, sha256=content_sha(draft),
                changes=changes)


def _next_version(rule_id):
    found = []
    for p in _published().glob(rule_id + '@*.json'):
        m = re.match(r'^.*@v(\d+)\.json$', p.name)
        if m:
            found.append(int(m.group(1)))
    return 'v%d' % (max(found) + 1 if found else 1)


def publish(rule_id, *, version=None, registered=None):
    index = _read_index()
    ref = index.get('drafts', {}).get(rule_id)
    if not ref:
        raise RuleError('NO_DRAFT:' + rule_id)
    draft = read_json(ROOT / ref)
    version = version or _next_version(rule_id)
    target = _published() / ('%s@%s.json' % (rule_id, version))
    if target.is_file():
        raise RuleError('VERSION_ALREADY_EXISTS_REFUSE_OVERWRITE:' + str(target))
    rule = prepare(draft, status='PUBLISHED', version=version)
    if registered is not None:
        validate_factor_refs(rule, registered)
    if not rule.get('factor_refs'):
        raise RuleError('FACTOR_REFS_REQUIRED')
    rule['published_at'] = LAB_LOGICAL_TIME
    rule['sha256'] = content_sha(rule)
    atomic_write_json(target, rule)
    index.setdefault('latest', {})[rule_id] = version
    index.setdefault('versions', {}).setdefault(rule_id, []).append(version)
    _write_index(index)
    return dict(file=str(target), ref='%s@%s' % (rule_id, version),
                sha256=rule['sha256'])


def get(rule_ref):
    if '@' in rule_ref:
        rid, version = rule_ref.rsplit('@', 1)
    else:
        rid = rule_ref
        version = _read_index().get('latest', {}).get(rid)
        if not version:
            raise RuleError('RULE_NOT_PUBLISHED:' + rid)
    path = _published() / ('%s@%s.json' % (rid, version))
    if not path.is_file():
        raise RuleError('RULE_VERSION_MISSING:' + rule_ref)
    rule = read_json(path)
    contract.validate_rule(rule)
    if rule.get('sha256') != content_sha(rule):
        raise RuleError('RULE_SHA_MISMATCH_TAMPERED:' + rule_ref)
    if rule['status'] != 'PUBLISHED':
        raise RuleError('RULE_NOT_PUBLISHED_STATUS:' + rule_ref)
    return rule


def list_rules():
    index = _read_index()
    out = []
    for p in sorted(_published().glob('*@*.json')):
        rule = read_json(p)
        out.append(dict(ref='%s@%s' % (rule['rule_id'], rule['version']),
                        title=rule['title'], sha256=rule.get('sha256'),
                        status=rule['status'], file=str(p.relative_to(ROOT))))
    return dict(contract='RULES_LAB_RULE_LIST_V1', latest=index.get('latest', {}),
                drafts=index.get('drafts', {}), published=out)


def bootstrap(registered=None):
    """首次运行把 rules/*.json 导入并发布；已存在则完全不改动。"""
    index = _read_index()
    done = []
    for p in sorted(RULES.glob('*.json')):
        raw = json.loads(p.read_bytes().decode('utf-8'))
        rid, version = raw.get('rule_id'), raw.get('version')
        if not rid or not version:
            continue
        if (_published() / ('%s@%s.json' % (rid, version))).is_file():
            continue
        import_rule(p, version=version)
        done.append(publish(rid, version=version, registered=registered))
    return done


def export_rule(rule_ref, dest=None):
    rule = get(rule_ref)
    if dest is None:
        dest = RULES / ('%s_%s_export.json' % (rule['rule_id'], rule['version']))
    atomic_write_json(dest, rule)
    return str(dest)
