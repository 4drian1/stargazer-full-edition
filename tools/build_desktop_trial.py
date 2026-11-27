"""Build the single-file Windows trial with a separate, local runtime."""
import gzip
from importlib import metadata
import json
from pathlib import Path
import re
import subprocess
import sys
import zipfile
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name


FULL = Path(__file__).resolve().parents[1]
PROJECT = FULL.parents[1]
DESKTOP = PROJECT / 'stargazer_desktop'
SEED = FULL.with_name('full_release')
WORK = FULL / '_tmp' / 'desktop_trial_build'
OUT = PROJECT / 'output' / 'stargazer_full_trial'


def dependency_files(sites, seeds):
    installed = {}
    for site in sites:
        for dist in metadata.distributions(path=[str(site)]):
            installed.setdefault(canonicalize_name(dist.name), (site, dist))
    pending = list(seeds)
    seen = set()
    while pending:
        name = canonicalize_name(pending.pop())
        if name in seen:
            continue
        seen.add(name)
        site, dist = installed[name]
        for relative in dist.files or ():
            path = site / relative
            if str(relative).endswith('.pyc') or '..' in relative.parts or not path.is_file():
                continue
            yield path, relative
        for raw in dist.requires or ():
            dependency = Requirement(raw)
            if dependency.marker is None or dependency.marker.evaluate():
                pending.append(dependency.name)


def build_payload(target):
    written = set()
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as bundle:
        def add(source, name):
            name = Path(name).as_posix()
            if name not in written:
                bundle.write(source, name)
                written.add(name)

        def tree(source, prefix):
            for item in source.rglob('*'):
                relative = item.relative_to(source)
                if item.is_file() and '__pycache__' not in relative.parts and item.suffix != '.pyc':
                    add(item, prefix / relative)

        installed = Path(sys.base_prefix)
        site = Path(sys.prefix) / 'Lib' / 'site-packages'
        python = Path('_python')
        for name in ('python.exe', 'python3.dll', 'python313.dll', 'vcruntime140.dll',
                     'vcruntime140_1.dll', 'LICENSE.txt'):
            add(installed / name, python / name)
        tree(installed / 'DLLs', python / 'DLLs')
        tree(installed / 'Lib', python / 'Lib')
        add(Path(sys.executable), Path('_dev_venv/Scripts/python.exe'))
        calendar = Path('Lib/site-packages/akshare/file_fold/calendar.json')
        add(Path(sys.prefix) / calendar, Path('_dev_venv') / calendar)
        packages = (
            'akshare', 'pandas', 'numpy', 'scipy', 'sklearn', 'joblib', 'threadpoolctl', 'pyarrow', 'narwhals',
            'flask', 'werkzeug', 'jinja2', 'markupsafe', 'itsdangerous', 'click', 'blinker',
            'requests', 'urllib3', 'certifi', 'chardet', 'charset_normalizer', 'idna',
            'dateutil', 'dateparser', 'pytz', 'tzdata', 'tzlocal', 'six', 'yaml', 'pytdx',
            'openai', 'pydantic', 'pydantic_core', 'annotated_types', 'typing_extensions',
            'typing_inspection', 'httpx', 'httpcore', 'anyio', 'sniffio', 'distro', 'jiter',
            'h11', 'tqdm', 'colorama', 'regex', 'lxml', 'jsonpath', 'bs4', 'soupsieve',
            'openpyxl', 'et_xmlfile', 'html5lib', 'webencodings', 'decorator', 'tabulate', 'xlrd',
            'psycopg', 'psycopg_binary', 'py_mini_racer', 'cryptography', 'cffi', '_cffi_backend',
            'langgraph', 'langchain_core', 'langchain_protocol', 'langsmith', 'ormsgpack', 'xxhash', 'orjson',
            'aiosqlite', 'tenacity', 'jsonpatch', 'jsonpointer', 'packaging', 'requests_toolbelt',
            'zstandard', 'mcp', 'sse_starlette', 'starlette', 'uvicorn', 'httpx_sse', 'dotenv',
            'pydantic_settings', 'jsonschema', 'jsonschema_specifications', 'referencing',
            'attrs', 'attr', 'rpds', 'rfc3339_validator', 'dingtalk_stream', 'websockets',
        )
        sites = (site, SEED / 'pkg_venv' / 'Lib' / 'site-packages')
        for source in sites:
            for item in source.iterdir():
                name = item.name.lower().replace('-', '_')
                if not any(name == p or name.startswith(p + '.') or name.startswith(p + '_')
                           for p in packages):
                    continue
                dest = python / 'Lib' / 'site-packages' / item.name
                tree(item, dest) if item.is_dir() else add(item, dest)
        for path, relative in dependency_files(sites, (
                'scikit-learn', 'akshare', 'openai', 'langgraph-checkpoint-sqlite',
                'langgraph', 'mcp', 'dingtalk-stream', 'pyarrow', 'Flask')):
            add(path, python / 'Lib' / 'site-packages' / relative)

        target_full = Path('stagazer_r/full_edition')
        for name in ('full', 'rules_lab', 'rules'):
            tree(FULL / name, target_full / name)
        tree(SEED / 'snapshot', target_full / 'snapshot')

        profile = PROJECT / 'stargazer_ml/v5_hybrid/green_common_profile'
        risk = profile.with_name('risk_r0_v1')
        tarot_manifest = re.search(r'tarot_live_manifest_v\d+_\d+_rel\.json\.gz',
                                   (profile / 'tarot_live_v2.py').read_text(encoding='utf-8')).group()
        risk_manifest = re.search(r'live_monitor_fingerprints_\d+_v\d+_flow\.json\.gz',
                                  (DESKTOP / 'golden_stock_analysis.py').read_text(encoding='utf-8')).group()
        for manifest in (profile / tarot_manifest, risk / risk_manifest):
            add(manifest, manifest.relative_to(PROJECT))
            document = json.loads(gzip.decompress(manifest.read_bytes()))
            for name in document.get('frozen_inputs', document):
                path = (PROJECT / name).resolve()
                if not path.is_relative_to(PROJECT) or 'dist' in Path(name).parts:
                    raise ValueError('Unexpected frozen input: ' + name)
                add(path, name)

        for folder in (DESKTOP, profile, risk, risk / 'index_minutes_v1'):
            for item in folder.glob('*.py'):
                add(item, item.relative_to(PROJECT))
        for name in ('tools', 'agent', 'strategy', 'web'):
            folder = DESKTOP / name
            tree(folder, folder.relative_to(PROJECT))
        for name in ('STRATEGY.md', 'config.example.yaml', 'PENTACLES_LEARNING.md'):
            add(DESKTOP / name, Path('stargazer_desktop') / name)

        impact = PROJECT / 'c3/corvus/impact_v1_1'
        for pattern in ('i24_1_zero_fit_*', 'i25_quantity_*', 'i28_time_*',
                        'i29_bound_*', 'i33_clause_role_fit_*'):
            folder = next(impact.glob(pattern))
            tree(folder, folder.relative_to(PROJECT))
        for name in ('i34_infer.py', 'i34_desktop_integration.py', 'i34_vocab.json',
                     'i34_receipt_index.json', 'i34_deepseek_input.json',
                     'i34_cninfo_download_links.txt', 'i34_stock_linkage_v1.py'):
            item = impact / 'i34_demo' / name
            add(item, item.relative_to(PROJECT))


def main():
    WORK.mkdir(parents=True, exist_ok=True)
    OUT.mkdir(parents=True, exist_ok=True)
    boot = PROJECT / 'tmp' / 'full_trial_boot'
    boot.mkdir(parents=True, exist_ok=True)
    payload = WORK / 'desktop_trial_payload.zip'
    if '--reuse-payload' not in sys.argv:
        build_payload(payload)
    print(f'Payload ready: {payload.stat().st_size:,} bytes', flush=True)
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
        '--noupx', '--name', 'StargazerFullTest', '--distpath', str(OUT),
        '--workpath', str(WORK / 'work'), '--specpath', str(WORK),
        '--runtime-tmpdir', str(boot),
        '--icon', str(DESKTOP / 'app.ico'), '--add-data', f'{payload};.',
        '--add-data', f'{FULL / "tools/desktop_trial_services.py"};.',
        '--add-data', f'{FULL / "tools/desktop_trial_nav.js"};.',
        '--add-data', f'{FULL / "full/webapp.py"};.',
        '--add-data', f'{PROJECT / "_stockdb.py"};.',
        '--add-data', f'{Path(sys.prefix) / "Lib/site-packages/msgpack"};trial_msgpack',
        str(FULL / 'tools' / 'desktop_trial_launcher.py'),
    ], check=True)


if __name__ == '__main__':
    main()
