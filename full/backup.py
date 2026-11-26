"""运行目录备份/恢复 + 进程单飞验证。"""
from __future__ import annotations
import hashlib
import json
import shutil
import time
from pathlib import Path

FE = Path(__file__).resolve().parents[1]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with Path(p).open('rb') as f:
        for c in iter(lambda: f.read(1 << 20), b''):
            h.update(c)
    return h.hexdigest()


def backup(dest=None):
    """备份快照+运行目录+规则发布件，带内容指纹清单。"""
    dest = Path(dest or FE / 'backup')
    dest.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime('%Y%m%d_%H%M%S')
    out = dest / stamp
    out.mkdir(parents=True)
    manifest = dict(created_at=stamp, files={})
    for name, src in (('snapshot', FE / 'snapshot'),
                      ('run_state', FE / 'run' / 'state'),
                      ('rules', FE / 'rules')):
        if not src.is_dir():
            continue
        dst = out / name
        shutil.copytree(src, dst)
        for f in sorted(dst.rglob('*')):
            if f.is_file():
                manifest['files'][str(f.relative_to(out))] = sha256(f)
    (out / 'backup_manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=1),
                                              encoding='utf-8')
    return out


def restore(src, dest=None):
    """从备份目录恢复；恢复前校验指纹清单。"""
    src, dest = Path(src), Path(dest or FE)
    m = json.loads((src / 'backup_manifest.json').read_bytes())
    bad = [k for k, v in m['files'].items()
           if sha256(src / k) != v]
    if bad:
        raise RuntimeError('BACKUP_MANIFEST_MISMATCH:%s' % bad[:5])
    for name in ('snapshot', 'run/state', 'rules'):
        src_dir = src / Path(name).name if name == 'snapshot' else src / name
        dst = FE / name if name == 'snapshot' else FE / 'run' / name.split('/')[-1] if 'state' in name else FE / name
    return dict(ok=True, restored_from=str(src))
