"""The trial serves the unchanged production shell and adds two rules modules."""
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parent
FULL = ROOT / 'stagazer_r/full_edition'
DESKTOP = ROOT / 'stargazer_desktop'
PROD_RUNTIME = DESKTOP / 'dist'
MODE = sys.argv[1] if __name__ == '__main__' else None


COMPACT_DATA_DIALOG = """async function openDataModal(){
  modal('数据来源与更新', `<form id="trial-data-form" class="trial-data-form">
    <label for="srcSel">来源</label>
    <select id="srcSel"><option value="tdx">本地通达信</option><option value="stockdb">StockDB</option></select>
    <label for="importSrc">导入路径</label>
    <input id="importSrc" type="text" spellcheck="false">
    <div class="button-row"><button class="primary-btn small" type="submit" disabled>保存</button>
    <button class="outline-btn" type="button" data-import disabled>导入本地数据</button></div>
    <div class="muted" role="status"></div>
  </form>`);
  const form=$('#trial-data-form'), status=form.querySelector('[role=status]');
  const select=form.querySelector('select'), path=form.querySelector('input');
  const buttons=[...form.querySelectorAll('button')];
  try{
    const config=await api('/api/source/config');
    if(!form.isConnected)return;
    if(config.ok===false)throw Error(config.error);
    select.value=config.source||'tdx';
    path.value=config.tdx_path||'';
    buttons.forEach(button=>button.disabled=false);
  }catch(error){status.textContent=error.message;}
  async function save(event){
    event.preventDefault();
    buttons.forEach(button=>button.disabled=true);
    try{
      const result=await api('/api/source/config',{source:select.value,tdx_path:path.value.trim()});
      if(result.ok===false)throw Error(result.error);
      status.textContent='已保存';
    }catch(error){status.textContent=error.message;}
    finally{buttons.forEach(button=>button.disabled=false);}
  }
  form.onsubmit=save;
  form.querySelector('[data-import]').onclick=async()=>{
    if(!path.value.trim()){status.textContent='请填写导入路径';return;}
    buttons.forEach(button=>button.disabled=true);
    status.textContent='正在导入…';
    try{
      const result=await api('/api/data/import',{src:path.value.trim(),include_minutes:true});
      if(!result.ok)throw Error(result.error||result.note||result.status||'导入失败');
      status.textContent=result.status==='NOTHING_NEW'?'没有新增数据':'导入完成';
      STATE=await api('/api/state').catch(()=>STATE);
    }catch(error){status.textContent=error.message;}
    finally{buttons.forEach(button=>button.disabled=false);}
  };
}"""


def publish_ready(path, port):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(dict(port=port, root=str(ROOT), mode=MODE,
                                   pid=os.getpid())), encoding='utf-8')
    temp.replace(path)


def flask_server(app, port, ready):
    import socket
    from werkzeug.serving import make_server
    original = socket.getfqdn

    def compatible_name(name=''):
        try:
            return original(name)
        except UnicodeError:
            return 'localhost'

    socket.getfqdn = compatible_name
    server = make_server('127.0.0.1', port, app, threaded=True)
    publish_ready(ready, server.server_port)
    server.serve_forever()


def rules_service(ready, self_test):
    sys.path[:0] = [str(FULL), str(DESKTOP), str(ROOT / 'stargazer_ml/v5_hybrid/risk_r0_v1')]
    reference = os.environ.get('STARGAZER_TRIAL_REFERENCE_DATA')
    if reference:
        os.environ['STARGAZER_PROD_DATA'] = reference

    from flask import Response, abort, jsonify, request
    from full import plan_reference
    from full.data_center import import_daily
    from full.strong_pool import ZONE, _day_pair
    from full.vipdoc_reader import calendar_days, vipdoc_root
    from full.webapp import BASE_RULE, _DATA_LOCK, create_app, snapshot_manifest, source_config
    from rules_lab.paths import SNAPSHOT
    from tdx_supply_v1 import daily_index_sessions

    plan_reference.PROD_ASSISTANT = PROD_RUNTIME / 'data/assistant'
    state = {}

    def sync_daily(day):
        if int(snapshot_manifest().get('daily_through') or 0) >= day:
            return
        vipdoc = vipdoc_root()
        missing = [stem for stem, item in daily_index_sessions(vipdoc, day).items()
                   if not item['has_target']]
        if missing:
            raise ValueError(f'本地通达信缺 {day} 指数日线，请先在通达信更新。缺口：{", ".join(missing)}')
        result = import_daily(vipdoc, SNAPSHOT, BASE_RULE, include_minutes=False)
        if not result.get('ok'):
            codes = ', '.join(row['code'] for row in result.get('conflicts', [])[:5])
            reason = result.get('error') or ('历史价格冲突 ' + codes if codes else result['status'])
            raise ValueError('规则日线同步失败：' + reason)
        if int(snapshot_manifest().get('daily_through') or 0) < day:
            raise ValueError(f'通达信已有 {day} 指数日线，但个股日线未同步到该日。')

    app = create_app(sync_daily=sync_daily)

    @app.get('/api/trial/daily-preview')
    def daily_preview():
        import re
        from urllib.parse import urlsplit
        code = request.args.get('code', '')
        if not re.fullmatch(r'\d{6}', code):
            return jsonify(ok=False, error='请输入六位股票代码'), 400
        config = source_config()[1]
        source = config.get('source', 'tdx')
        try:
            if source == 'stockdb':
                from _stockdb import http_get_raw, make_opener, recs_of
                base = config.get('stockdb_base', '')
                endpoint = urlsplit(base)
                if (endpoint.scheme != 'http' or endpoint.hostname not in ('localhost', '127.0.0.1', '::1')
                        or endpoint.username or endpoint.password or endpoint.path not in ('', '/')
                        or endpoint.query or endpoint.fragment):
                    raise ValueError('StockDB 地址应为本机 HTTP 服务，例如 http://127.0.0.1:7899')
                rows = recs_of(http_get_raw(make_opener(), base.rstrip('/'), '日k', code, '*', timeout=5))
            else:
                from full.vipdoc_reader import daily_rows
                vip = vipdoc_root()
                stems = [market+code for market in ('sh','sz','bj')
                         if (vip / market / 'lday' / (market+code+'.day')).is_file()
                         and not (market == 'sh' and code.startswith('000'))]
                if len(stems) != 1:
                    raise ValueError('本地通达信未找到唯一的个股日线')
                rows = daily_rows(stems[0], 20, int(datetime.now(ZONE).strftime('%Y%m%d')))
            import math
            sample = []
            for row in rows:
                date = str(row.get('date', '')).replace('-', '')
                close = float(row.get('close') or 0)
                if re.fullmatch(r'\d{8}', date) and math.isfinite(close) and close > 0:
                    sample.append(dict(date=date, close=close))
            sample.sort(key=lambda row: row['date'])
            if not sample:
                raise ValueError('所选来源暂无可用日线')
            return jsonify(ok=True, source=source, code=code, count=len(sample), rows=sample[-20:])
        except (OSError, ValueError, TypeError) as exc:
            return jsonify(ok=False, source=source, error=str(exc)), 409

    @app.get('/trial-module/<page>')
    def module(page):
        if page not in ('strategy', 'review', 'data', 'push'):
            abort(404)
        body = (FULL / 'full/static/index.html').read_text(encoding='utf-8')
        head = '<style>.topbar,.statusbar2{display:none!important}.app-shell{padding-bottom:0}</style>'
        body = body.replace('</head>', head + '</head>', 1)
        if page in ('data', 'review'):
            start = body.index('async function openDataModal(){')
            end = body.index('\nasync function loadDiag(){', start)
            body = body[:start] + COMPACT_DATA_DIALOG + body[end:]
            style = """<style>
              .trial-data-form{display:grid;grid-template-columns:max-content minmax(0,1fr);
                gap:10px 12px;align-items:center;padding:14px 16px}
              .trial-data-form input,.trial-data-form select{min-width:0;width:100%;padding:7px 8px;
                border:1px solid #343a3e;border-radius:3px;background:#0c0e0f;color:#e4e8eb}
              .trial-data-form .button-row,.trial-data-form [role=status]{grid-column:1/-1}
              .trial-data-form [role=status]:empty{display:none}
              .trial-data-form [role=status]{overflow-wrap:anywhere}
            </style>"""
            body = body.replace('</head>', style + '</head>', 1)
        if page in ('data', 'push'):
            head = '<style>.app-shell{display:none}.modal-backdrop{position:static;display:block;padding:0;background:none}.modal{width:100%!important;max-width:none;box-shadow:none;border:0;padding-bottom:0}.modal .modal-head,.modal .modal-actions{display:none}</style>'
            body = body.replace('</head>', head + '</head>', 1)
            script = ''
        elif page == 'strategy':
            script = """const action=document.createElement('div');
    action.className='logic-line';
    action.innerHTML='<button class="primary-btn small" type="button">按规则盘后选股</button><span role="status"></span>';
    V.before(action);
    action.querySelector('button').onclick=async function(){
      const output=action.querySelector('[role=status]');
      const selected=$('#ruleRef')?.value;
      if(!selected?.includes('@')){output.textContent='请选择一个已发布版本';return;}
      this.disabled=true;output.textContent='正在核验日线并选股…';
      try{
        const response=await api('/api/plan/select',{rule:selected});
        if(!response.ok)throw Error(response.error);
        const list=await api('/api/plan/latest?run_id='+encodeURIComponent(response.run_id));
        output.textContent='数据日 '+response.p_day+' · 观察日 '+response.day+' · 候选 '+response.count+' 只';
        const prior=action.nextElementSibling;
        if(prior?.id==='trial-rule-results')prior.remove();
        const table=document.createElement('div');table.id='trial-rule-results';table.className='panel';
        for(const item of list.candidates||[]){
          const row=document.createElement('div');row.className='row2';
          const zone=item.zone_lower!=null&&item.zone_upper!=null ? '  黄金观察区 '+item.zone_lower+'–'+item.zone_upper : '';
          row.textContent=[item.code,item.name].filter(Boolean).join(' ')+zone;table.append(row);
        }
        if(table.children.length)action.after(table);
      }catch(error){output.textContent=error.message;}
      finally{this.disabled=false;}
    };"""
        else:
            script = """const replay=document.createElement('div');
    V.before(replay);
    async function setupReplay(){
      const rs=await api('/api/rules');
      replay.innerHTML=panel('历史回放',`<div class="logic-line">
        <label>规则 <select id="runRef"></select></label>
        <label>监控日 <input id="runDay" value="${STATE.monitor_day||''}" size="9"></label>
        <label><input type="checkbox" id="useModel" ${STATE.model_switch?.selection?'checked':''}> 附加 Tarot 影子评分</label>
        <button class="primary-btn small" onclick="runNow()">运行回放</button>
        <button class="outline-btn" onclick="openDataModal()">导入回放数据</button>
        <span id="runState" role="status"></span></div><div id="runOut"></div>`)
        +spacing+panel('回放回执','<div class="table-wrap"><table id="inboxTbl"></table></div>');
      for(const rule of rs.published)$('#runRef').add(new Option(rule.title||rule.ref,rule.ref));
      $('#runRef').value=defaultRef(rs.published.map(rule=>rule.ref));
      await showInbox();
    }"""
        before, marker, after = body.rpartition("go('overview');")
        if not marker:
            abort(500)
        launch = {'data': 'await openDataModal();', 'push': 'await openPushModal();'}.get(page)
        if launch:
            # The outer native dialog owns navigation and dismissal.
            body_script = before + script + '\n' + launch.replace('await ', '') + '\n'
            return Response(body_script + after, mimetype='text/html')
        startup = f"""(async()=>{{
      try{{STATE=await api('/api/state');}}catch(error){{toast('数据状态读取失败：'+error.message,true);}}
      go('{page}');
      {'setupReplay().catch(error=>toast(error.message,true));' if page == 'review' else ''}
    }})();"""
        return Response(before + script + '\n' + startup + after, mimetype='text/html')

    @app.route('/api/trial/sync-daily', methods=['GET', 'POST'])
    def synchronization():
        from flask import request
        if request.method == 'GET':
            return jsonify(dict(state, daily_through=snapshot_manifest().get('daily_through')))
        try:
            day, _ = _day_pair(datetime.now(ZONE), calendar_days())
        except (OSError, ValueError) as exc:
            return jsonify(ok=False, status=str(exc)), 409
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(ok=False, status='ALREADY_RUNNING')
        state.update(running=True, status='STARTED', error=None)

        def work():
            try:
                sync_daily(day)
            except (OSError, ValueError, RuntimeError) as exc:
                state.update(status='FAILED', error=str(exc))
            else:
                state.update(status='FINISHED')
            finally:
                state.update(running=False)
                _DATA_LOCK.release()

        Thread(target=work, name='trial-daily-sync', daemon=True).start()
        return jsonify(ok=True, status='STARTED')

    flask_server(app, 0, ready)


def install_extensions(server):
    from flask import Response, jsonify, request
    rules_url = os.environ['STARGAZER_TRIAL_RULES_URL']
    original_market_state = server.app.view_functions['api_market_data_state']

    def rules_sync(method, path='/api/trial/sync-daily', payload=None, timeout=4):
        try:
            req = Request(rules_url + path, method=method, data=payload,
                          headers={'Content-Type': 'application/json'} if payload is not None else {})
            with urlopen(req, timeout=timeout) as response:
                return json.load(response), response.status
        except HTTPError as exc:
            return json.load(exc), exc.code
        except OSError:
            return dict(ok=False, status='FAILED', error='规则服务暂不可用'), 503

    def market_state():
        from tdx_supply_v1 import INDICES, daily_index_sessions, lc5_last_day
        result = original_market_state().get_json()
        synced, _ = rules_sync('GET')
        vipdoc = Path(result['vipdoc'])
        result['configured'] = vipdoc.is_dir()
        result['update_vipdoc'] = result['vipdoc']
        result['rule_daily_through'] = synced.get('daily_through')
        result['state'] = dict(synced, fatal=synced.get('error'))
        if result['configured'] and result.get('target_day'):
            try:
                raw = daily_index_sessions(vipdoc, result['target_day'])
                minutes = {stem: lc5_last_day(vipdoc / stem[:2] / 'fzline' / (stem+'.lc5'))
                           for stem in INDICES}
                result['gaps'] = dict(sources=dict(tdx_daily=dict(indices=raw),
                                                   tdx_minutes=dict(indices=minutes)))
            except (OSError, ValueError):
                result['gaps'] = {}
        return jsonify(result)

    def market_update():
        result, code = rules_sync('POST')
        return jsonify(result), code

    server.app.view_functions['api_market_data_state'] = market_state
    server.app.view_functions['api_market_data_update'] = market_update

    @server.app.get('/api/trial/overview')
    def trial_overview():
        state, code = rules_sync('GET', '/api/state', timeout=12)
        if code != 200:
            return jsonify(state), code
        last, _ = rules_sync('GET', '/api/last_run')
        push, _ = rules_sync('GET', '/api/push/config')
        return jsonify(state=state, last=last, push=push)

    @server.app.post('/api/trial/model-switch')
    def trial_model_switch():
        body = request.get_json(silent=True) or {}
        if not isinstance(body.get('selection'), bool):
            return jsonify(ok=False, error='选股模型开关需要布尔值'), 400
        result, code = rules_sync('POST', '/api/model_switch',
                                  json.dumps(dict(selection=body['selection'])).encode())
        return jsonify(result), code

    @server.app.after_request
    def production_shell(response):
        if response.status_code != 200:
            return response
        if request.path == '/':
            response.direct_passthrough = False
            html = response.get_data(as_text=True)
            response.set_data(html.replace('</body>', '<script src="/trial-modules.js"></script></body>', 1))
        elif request.path == '/assets/market-data-v231.js':
            response.direct_passthrough = False
            script = response.get_data(as_text=True)
            script = script.replace(
                "const details = [",
                """const details = [
      '点击更新数据，将手动更新后的通达信日线同步到规则快照。',
      `规则快照已同步至 ${data.rule_daily_through ?? '待核验'}`,""", 1)
            script = script.replace("else if (state.status) {",
                """else if (state.error) setStatus(current, state.error);
    else if (state.status) {""", 1)
            response.set_data(script)
        return response

    @server.app.get('/trial-modules.js')
    def modules_script():
        script = (ROOT / 'desktop_trial_nav.js').read_text(encoding='utf-8')
        return Response('const trialRulesUrl = ' + json.dumps(rules_url) + ';\n' + script,
                        mimetype='text/javascript')


def classic_service(ready, self_test):
    def stage(message):
        print(f'[{datetime.now():%Y-%m-%d %H:%M:%S}] classic: {message}', flush=True)

    stage('initializing runtime')
    risk = ROOT / 'stargazer_ml/v5_hybrid/risk_r0_v1'
    sys.path[:0] = [str(DESKTOP), str(risk)]
    os.environ['STARGAZER_PROJECT_ROOT'] = str(ROOT)
    os.environ['STARGAZER_DATA_ROOT'] = str(PROD_RUNTIME / 'data')
    os.environ['STARGAZER_MARKET_DATA'] = str(ROOT / 'market_data')
    os.environ['STARGAZER_PYTHON'] = str(ROOT / '_dev_venv/Scripts/python.exe')
    os.environ['STARGAZER_DESKTOP_DIR'] = str(PROD_RUNTIME)
    os.environ['STARGAZER_TDX_ROOT'] = (PROD_RUNTIME / 'data/tdx_root.txt').read_text(encoding='utf-8').strip()

    import paths
    paths.APP_DIR = str(PROD_RUNTIME)
    paths.FROZEN = True
    import yaml
    cfg = PROD_RUNTIME / 'config.yaml'
    config = yaml.safe_load(cfg.read_text(encoding='utf-8'))
    config['push']['enabled'] = False
    config['push']['daily_tarot'] = dict(enabled=False)
    cfg.write_text(yaml.safe_dump(config, allow_unicode=True), encoding='utf-8')
    stage('loading backend and model dependencies')
    import server
    server.log = server.notifier.log
    import i34_bridge
    reference = os.environ.get('STARGAZER_TRIAL_REFERENCE_DATA')
    if reference:
        corvus = Path(reference).parents[2] / 'corvus/data'
        if (corvus / 'corvus.sqlite').is_file():
            i34_bridge._news_root = lambda: corvus
    install_extensions(server)
    if self_test:
        server.register_extension_routes()
        flask_server(server.app, 0, ready)
        return
    stage('starting HTTP and monitor services')
    port = server.start(port=17942)
    publish_ready(ready, port)
    stage(f'published readiness on port {port}')
    from threading import Event
    Event().wait()


if __name__ == '__main__':
    mode, receipt, *flags = sys.argv[1:]
    handlers = {'rules': rules_service, 'classic': classic_service}
    handlers[mode](Path(receipt), '--self-test' in flags)
