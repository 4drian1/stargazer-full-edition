"""完整版运行路径：任何写操作只落在 full_edition 内（D 盘）。

与 rules_lab v1.1 同一隔离纪律，但目录独立：运行态在 run/，只读样本在
snapshot/（由真实行情构建，构建脚本一次性写入后只读使用）。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]          # full_edition/
SNAPSHOT = Path(os.environ.get('RULES_LAB_SNAPSHOT_DIR') or ROOT / 'snapshot')
RUN = ROOT / 'run'
STATE = Path(os.environ.get('RULES_LAB_STATE_DIR') or RUN/'state').resolve()
if STATE != ROOT and ROOT not in STATE.parents:
    raise ValueError('STATE_OUTSIDE_FULL_EDITION')
CACHE = RUN / 'cache'
BUILD = RUN / 'build'
REPORTS = ROOT / 'reports'
RULES = ROOT / 'rules'
TESTS = ROOT / 'tests'
TMP = ROOT / '_tmp'
TOOLS = ROOT / 'tools'
SCRATCH = TMP / 'accept_scratch'

SUBDIRS = (
    SNAPSHOT, RUN, STATE, CACHE, BUILD, REPORTS, RULES, TMP, TOOLS,
    STATE / 'rules', STATE / 'rules' / 'drafts', STATE / 'rules' / 'published',
    STATE / 'tasks', STATE / 'freeze', STATE / 'events', STATE / 'inbox',
    STATE / 'locks', STATE / 'runs', STATE / 'research',
)

PORT = 17891          # 完整版候选端口（与正式版错开）


def ensure_dirs() -> None:
    for d in SUBDIRS:
        d.mkdir(parents=True, exist_ok=True)


def make_scratch(tag: str) -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=tag + '_', dir=str(SCRATCH)))


def run_root() -> Path:
    """run 目录根：默认 run/state/runs，测试可用环境变量重定向。"""
    override = os.environ.get('RULES_LAB_RUN_ROOT')
    root = Path(override) if override else STATE / 'runs'
    root.mkdir(parents=True, exist_ok=True)
    return root


def test_root() -> Path | None:
    override = os.environ.get('RULES_LAB_TEST_ROOT')
    return Path(override) if override else None


def report_root() -> Path:
    REPORTS.mkdir(parents=True, exist_ok=True)
    return REPORTS


def read_json(path):
    return json.loads(Path(path).read_bytes())


def assert_write_allowed(path) -> Path:
    """写路径守卫：必须是 D 盘且位于 full_edition 内，否则 fail-closed。"""
    p = Path(path).resolve()
    if p.drive.upper() != ROOT.drive.upper() or p.drive.upper() != 'D:':
        raise RuntimeError('WRITE_OUTSIDE_D_DRIVE:' + str(p))
    try:
        p.relative_to(ROOT)
    except ValueError:
        raise RuntimeError('WRITE_OUTSIDE_FULL_EDITION:' + str(p))
    return p


def atomic_write_bytes(path, data: bytes) -> Path:
    p = assert_write_allowed(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + '.tmp')
    with open(tmp, 'wb') as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    last = None
    for _ in range(6):
        try:
            os.replace(tmp, p)
            return p
        except OSError as exc:    # Windows：杀软/索引可能短暂占用目标文件
            last = exc
            time.sleep(0.4)
    raise last


def atomic_write_json(path, value) -> Path:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True,
                     separators=(',', ':'), allow_nan=False).encode('utf-8')
    return atomic_write_bytes(path, raw)


def canonical_bytes(value) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':')).encode('utf-8')


def sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def canonical_sha(value) -> str:
    return sha256_bytes(canonical_bytes(value))


def sha256_file(path) -> str:
    return sha256_bytes(Path(path).read_bytes())
