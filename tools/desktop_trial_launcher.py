"""Single executable boundary for the two isolated Stargazer trial views."""
import ctypes
import hashlib
from html import escape
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from threading import Event, Lock
import time
import traceback
import urllib.request
import uuid
import zipfile


PROJECT = Path('D:/stargazer')
PRODUCTION = PROJECT / 'stargazer_desktop/dist'
CANDIDATE = PROJECT / 'stagazer_r/full_release'
WORK = Path(sys.executable).resolve().parent / 'StargazerFullTest_data'
BUNDLE = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
TEMP = WORK / 'tmp'
STARTUP_TIMEOUT = 150


def startup_page(message, detail=''):
    return f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
    <title>Stargazer 完整版</title>
    <style>body{{background:#111315;color:#eee;font:16px system-ui,sans-serif;
    margin:0;min-height:100vh;display:grid;place-items:center}}main{{max-width:75vw}}
    h1{{color:#f4a338;margin:0}}p{{white-space:pre-wrap;overflow-wrap:anywhere}}</style>
    <main><h1>Stargazer 完整版</h1><p role="status">{escape(message)}</p>
    <p>{escape(detail)}</p></main></html>"""


def first_copy(source, target):
    if source.is_file() and not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def seed_plan(classic):
    source = PRODUCTION / 'data/assistant'
    target = classic / 'data/assistant'
    if not source.is_dir() or any(target.glob('confirmed_*.json')):
        return
    recent = max(source.glob('confirmed_*.json'), key=lambda item: item.stat().st_mtime,
                 default=None)
    if recent:
        first_copy(recent, target / recent.name)
        identity = recent.stem.removeprefix('confirmed_')
        first_copy(source / f'plan_{identity}.json', target / f'plan_{identity}.json')


def configured_tdx():
    pointer = PRODUCTION / 'data/tdx_root.txt'
    if pointer.is_file():
        text = pointer.read_text(encoding='utf-8-sig').strip()
        if text and Path(text).is_dir():
            return Path(text)
    source = CANDIDATE / 'data/source_config.json'
    if source.is_file():
        config = json.loads(source.read_bytes())
        text = config.get('tdx_path')
        if text and Path(text).is_dir():
            return Path(text)
    return PROJECT / 'market_data/vipdoc'


def prepare():
    payload = BUNDLE / 'desktop_trial_payload.zip'
    with payload.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256')
    build = digest.hexdigest()[:16]
    pointer = WORK / f'runtime-{build}.txt'
    runtime = WORK / build
    if pointer.is_file():
        import re
        name = pointer.read_text(encoding='utf-8').strip()
        if re.fullmatch(r'[a-f0-9]{16}', name):
            runtime = WORK / name
    elif not (runtime / '.complete').is_file():
        for receipt in WORK.glob('*/.complete'):
            old = receipt.parent / 'desktop_trial_services.py'
            if old.is_file():
                legacy = digest.copy()
                legacy.update(old.read_bytes())
                if receipt.parent.name == legacy.hexdigest()[:16]:
                    runtime = receipt.parent
                    break
    ready = runtime / '.complete'
    if not ready.is_file():
        with zipfile.ZipFile(payload) as archive:
            archive.extractall(runtime)
        ready.touch()
    pointer.write_text(runtime.name, encoding='utf-8')
    for name, relative in (
        ('desktop_trial_services.py', 'desktop_trial_services.py'),
        ('desktop_trial_nav.js', 'desktop_trial_nav.js'),
        ('webapp.py', 'stagazer_r/full_edition/full/webapp.py'),
        ('_stockdb.py', '_stockdb.py'),
    ):
        shutil.copyfile(BUNDLE / name, runtime / relative)
    package = runtime / '_python/Lib/site-packages/msgpack'
    if not (package / '__init__.py').is_file():
        shutil.copytree(BUNDLE / 'trial_msgpack', package, dirs_exist_ok=True)
    (runtime / '_dev_venv/pyvenv.cfg').write_text(
        f'home = {runtime / "_python"}\ninclude-system-site-packages = true\n', encoding='utf-8')
    full = runtime / 'stagazer_r/full_edition'
    classic = runtime / 'stargazer_desktop/dist'
    tdx = configured_tdx()
    (full / 'data').mkdir(exist_ok=True)
    (classic / 'data').mkdir(parents=True, exist_ok=True)
    source_config = full / 'data/source_config.json'
    config = json.loads(source_config.read_bytes()) if source_config.exists() else {}
    config.setdefault('source', 'tdx')
    config.setdefault('tdx_path', str(tdx))
    config.setdefault('stockdb_base', 'http://127.0.0.1:7899')
    source_config.write_text(json.dumps(config), encoding='utf-8')
    pointer = classic / 'data/tdx_root.txt'
    pointer.write_text(str(tdx), encoding='utf-8')
    cfg = classic / 'config.yaml'
    if not cfg.exists():
        shutil.copyfile(runtime / 'stargazer_desktop/config.example.yaml', cfg)
        with cfg.open('a', encoding='utf-8') as stream:
            stream.write('\npentacles_demo:\n  daily_dir: ' +
                         json.dumps(str(classic / 'data/postclose_risk/tdx_daily_v2')) + '\n')
    # Seed the trial's own plan, holdings and board selection. Never import push settings,
    # API credentials, delivery state, or a writable link to the production runtime.
    first_copy(CANDIDATE / 'data/strong_watch.json', full / 'data/strong_watch.json')
    board_source = CANDIDATE / 'data/board_watch.json'
    board_target = full / 'data/board_watch.json'
    if board_source.is_file() and not board_target.exists():
        board = json.loads(board_source.read_bytes())
        board_target.write_text(json.dumps(dict(enabled=False, boards=board.get('boards') or []),
                                            ensure_ascii=False), encoding='utf-8')
    first_copy(PRODUCTION / 'data/local_records/trade_plan.json', classic / 'data/trade_plan.json')
    for name in ('watchlist.json', 'trade_plan.json', 'trades.db', 'board_list.json',
                 'user_watch_sectors.json', 'pentacles_demo/snapshot.json',
                 'pentacles_demo/assistant_plan.json'):
        first_copy(PRODUCTION / 'data' / name, classic / 'data' / name)
    seed_plan(classic)
    source = PRODUCTION / 'data/postclose_risk/board_identities'
    for file in source.rglob('*.json'):
        first_copy(file, classic / 'data/postclose_risk/board_identities' / file.relative_to(source))
    return runtime


class Desktop:
    def __init__(self):
        self.children = {}
        self.window = None
        self.runtime = None
        self.closing = Event()
        self.process_lock = Lock()

    def start_service(self, mode, self_test=False):
        child = self.children.get(mode)
        if child and child[0].poll() is None and child[1]:
            return child[1]
        ready = TEMP / f'{mode}-{uuid.uuid4().hex}.json'
        log = WORK / f'{mode}.log'
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('STARGAZER_', 'RULES_LAB_', 'PYTHON', '_PYI_'))
               and k not in ('DEEPSEEK_API_KEY', 'I34_INFER_DIR')}
        env.update(PYTHONUTF8='1', PYTHONDONTWRITEBYTECODE='1',
                   TEMP=str(TEMP), TMP=str(TEMP),
                   STARGAZER_TRIAL_REFERENCE_DATA=str(PRODUCTION / 'data'))
        if mode == 'classic':
            env['STARGAZER_TRIAL_RULES_URL'] = self.start_service('rules', self_test=self_test)
        flags = ['--self-test'] if self_test else []
        with self.process_lock:
            if self.closing.is_set():
                return None
            with log.open('ab') as output:
                output.write(f'\n[{time.strftime("%Y-%m-%d %H:%M:%S")}] Starting {mode}\n'.encode())
                output.flush()
                process = subprocess.Popen([
                    str(self.runtime / '_python/python.exe'), '-B',
                    str(self.runtime / 'desktop_trial_services.py'), mode, str(ready), *flags,
                ], cwd=self.runtime, env=env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT,
                    creationflags=subprocess.CREATE_NO_WINDOW)
            self.children[mode] = process, None
        deadline = time.monotonic() + STARTUP_TIMEOUT
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        while not self.closing.is_set():
            exit_code = process.poll()
            if exit_code is not None:
                raise RuntimeError(f'{mode} 后端提前退出（退出码 {exit_code}），详情见 {log}')
            if ready.is_file():
                data = json.loads(ready.read_bytes())
                if data.get('pid') == process.pid and data.get('mode') == mode:
                    url = f'http://127.0.0.1:{int(data["port"])}'
                    try:
                        with opener.open(url, timeout=2) as response:
                            if response.status == 200:
                                self.children[mode] = process, url
                                return url
                    except OSError:
                        pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError(f'{mode} 后端在 {STARTUP_TIMEOUT} 秒内未就绪，详情见 {log}')
            self.closing.wait(min(.25, remaining))
        return None

    def close(self):
        with self.process_lock:
            self.closing.set()
            children = tuple(self.children.values())
        for process, _ in children:
            if process.poll() is None:
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
                if process.poll() is None:
                    process.terminate()

    def open_window(self, receipt=None):
        try:
            url = self.start_service('classic')
            if url is None or self.closing.is_set():
                return
            self.window.load_url(url)
        except Exception as exc:
            error = traceback.format_exc()
            (WORK / 'startup_error.log').write_text(error, encoding='utf-8')
            if self.closing.is_set():
                return
            if receipt:
                Path(receipt).write_text(json.dumps(dict(ok=False, error=error)), encoding='utf-8')
                self.window.destroy()
            else:
                self.window.load_html(startup_page('启动失败', str(exc)))
                self.close()
            return
        if receipt:
            verify_window(self, receipt)

    def self_test(self, receipt):
        result = {}
        checks = {
            'rules': ('/trial-module/strategy', '/trial-module/review', '/trial-module/data',
                      '/trial-module/push', '/api/state', '/api/rules'),
            'classic': ('/', '/api/build_info', '/api/pentacles/state', '/api/assistant/state',
                        '/api/risk/outlook/latest', '/api/info_feed', '/i34', '/api/state',
                        '/api/market_data/state', '/api/strong-pool/state', '/api/trial/overview'),
        }
        for mode, routes in checks.items():
            url = self.start_service(mode, self_test=True)
            result[mode] = {}
            for path in routes:
                with urllib.request.urlopen(url + path, timeout=15) as response:
                    if path == '/':
                        assert b'/trial-modules.js' in response.read()
                    result[mode][path] = response.status
        Path(receipt).write_text(json.dumps(result, indent=2), encoding='utf-8')


def verify_window(desktop, receipt):
    window = desktop.window
    result = []
    try:
        window.resize(960, 690)
        for label, match in (
            ('交易计划', 'strong'), ('持仓', '持仓'), ('观察名单', '观察名单'),
            ('Stargazer助手', '助手'), ('策略规则', '/trial-module/strategy'),
            ('复盘研究', '复盘研究'), ('Corvus信息', 'Corvus'), ('总览', '总览'),
        ):
            click = f"[...document.querySelectorAll('.main-nav button')].find(b=>b.textContent.replace(/\\s/g,'')==={json.dumps(label)})?.click()"
            for _ in range(80):
                window.evaluate_js(click)
                read = """(() => {
                    const main=document.querySelector('main.main-content');
                    if(main?.hidden)return document.querySelector('#trial-module-frame')?.src;
                    return main?.innerText;
                })()"""
                content = window.evaluate_js(read) or ''
                if (match == 'strong' and '强势' in content) or (match != 'strong' and match in content):
                    break
                time.sleep(.25)
            else:
                raise RuntimeError(f'页面未就绪：{label}')
            dimensions = window.evaluate_js("""(() => ({
              width: document.documentElement.clientWidth,
              scroll: document.documentElement.scrollWidth,
              nowrap: [...document.querySelectorAll('.main-nav button')]
                       .every(button => getComputedStyle(button).whiteSpace === 'nowrap')
            }))()""")
            if dimensions['scroll'] > dimensions['width'] + 1 or not dimensions['nowrap']:
                raise RuntimeError(f'页面横向溢出：{label} {dimensions}')
            if label == '交易计划':
                assert window.evaluate_js("""(() => {
                  const grid=document.querySelector('.plan-layout'), main=grid.querySelector('.plan-main');
                  return main.getBoundingClientRect().width >= grid.getBoundingClientRect().width - 2;
                })()"""), '草案和生效计划未占满可用宽度'
            if label == 'Stargazer助手':
                assert window.evaluate_js("""(() => {
                  const grid=document.querySelector('.sg-assistant'), chat=grid.querySelector('.sg-chat');
                  return chat.getBoundingClientRect().width >= grid.getBoundingClientRect().width * .7;
                })()"""), '对话区域仍然过窄'
            if label == 'Corvus信息':
                assert window.evaluate_js("""(() => {
                  const grid=document.querySelector('.sg-corvus-grid');
                  const china=grid.querySelector('.sg-corvus-main').getBoundingClientRect();
                  const global=grid.querySelector('.sg-side').getBoundingClientRect();
                  return global.top >= china.bottom && Math.abs(global.width-china.width)<2;
                })()"""), '全球行情没有放在 A 股信息下方'
            if label == '复盘研究':
                assert window.evaluate_js("""[...document.querySelectorAll('.main-nav button')]
                    .filter(b=>b.textContent.trim()==='复盘研究').length===1 &&
                    ![...document.querySelectorAll('.main-nav button')].some(b=>b.textContent.trim()==='复盘')""")
                window.run_js("document.querySelector('[data-review-mode=review]').click()")
                assert window.evaluate_js("document.querySelector('#trial-module-frame').src.endsWith('/trial-module/review')")
                window.run_js("document.querySelector('[data-review-mode=\"\"]').click()")
                assert window.evaluate_js("!document.querySelector('main.main-content').hidden")
            result.append(label)
        for _ in range(40):
            if window.evaluate_js("document.querySelector('#trial-save-model')?.disabled === false"):
                break
            time.sleep(.25)
        else:
            raise RuntimeError('总览功能卡片未就绪')
        window.run_js("document.querySelector('[data-settings=data]').click()")
        if not window.evaluate_js("document.querySelector('#trial-settings').open"):
            raise RuntimeError('数据源配置弹窗未打开')
        window.run_js("document.querySelector('#trial-settings').close()")
        window.load_url(desktop.children['rules'][1] + '/trial-module/data')
        for _ in range(40):
            if window.evaluate_js("document.querySelector('#trial-data-form button')?.disabled === false"):
                break
            time.sleep(.25)
        else:
            raise RuntimeError('数据来源表单未就绪')
        assert window.evaluate_js("""(() => {
          const form=document.querySelector('#trial-data-form');
          return form.getBoundingClientRect().bottom <= 208 &&
                 !document.querySelector('#diagOut') &&
                 [...form.querySelectorAll('option')].some(option=>option.textContent==='StockDB');
        })()"""), '数据来源表单需要滚动或仍显示诊断项'
        Path(receipt).write_text(json.dumps(dict(ok=True, views=result), ensure_ascii=False),
                                 encoding='utf-8')
    except Exception:
        Path(receipt).write_text(json.dumps(dict(ok=False, error=traceback.format_exc())),
                                 encoding='utf-8')
    finally:
        window.destroy()


def main():
    import msvcrt
    WORK.mkdir(parents=True, exist_ok=True)
    TEMP.mkdir(exist_ok=True)
    with (WORK / 'desktop.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            ctypes.windll.user32.MessageBoxW(None, '测试版已在运行。', 'Stargazer 完整版', 0x40)
            return
        desktop = Desktop()
        try:
            desktop.runtime = prepare()
            if len(sys.argv) == 3 and sys.argv[1] == '--self-test':
                desktop.self_test(sys.argv[2])
                return
            testing = len(sys.argv) == 3 and sys.argv[1] == '--ui-smoke'
            import webview
            desktop.window = webview.create_window('Stargazer 完整版 · 测试',
                                                   html=startup_page('正在启动规则服务与 classic 后端…'),
                                                   width=1240, height=820,
                                                   min_size=(900, 600), text_select=True)
            webview.start(desktop.open_window, (sys.argv[2] if testing else None,),
                          gui='edgechromium', storage_path=str(WORK / 'webview'))
        except Exception as exc:
            (WORK / 'startup_error.log').write_text(traceback.format_exc(), encoding='utf-8')
            if '--self-test' in sys.argv:
                raise
            ctypes.windll.user32.MessageBoxW(None, str(exc), 'Stargazer 完整版启动失败', 0x10)
        finally:
            desktop.close()


if __name__ == '__main__':
    main()
