"""独立表单服务：纯标准库 http.server，只绑定 127.0.0.1 的实验端口。

与正式 Stargazer 完全分离：独立启动命令、独立端口、独立实例锁、
独立缓存/任务/回执路径；不加载模型、不读 webhook、不调用任何 LLM。
"""
from __future__ import annotations

import html
import json
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import rules
from .contract import OPERATORS, LOGIC_OPS, field_catalog
from .factors import compute_selection_factors, apply_formula_factors
from .interpreter import preview
from .paths import ROOT, STATE, atomic_write_json
from .snapshot import open_snapshot

DEFAULT_PORT = 18765
ENV_FILE = ROOT / 'lab_data' / 'build' / 'lab_env.json'


def lab_env(port=DEFAULT_PORT):
    return dict(
        contract='RULES_LAB_ENV_V1',
        app='stargazer_rules_lab', root=str(ROOT),
        command='python -B -X utf8 -m rules_lab.cli serve --port %d' % port,
        bind='127.0.0.1', port=port,
        python=sys.version.split()[0],
        runtime_dependencies=['stdlib-only'],
        model_files=False, model_credentials=False, llm_imports=False,
        production_writes=False,
        instance_lock=str(STATE / 'locks' / 'rules_lab.lock'),
        cache=str(ROOT / 'lab_data' / 'cache'),
        tasks=str(STATE / 'runs'),
        notification_receipts=str(STATE / 'runs' / '<run_id>' / 'inbox' / 'receipts.jsonl'),
        tmp_override=str(ROOT / '_tmp'),
        production='READ_ONLY: D:\\stargazer',
    )


def _page():
    fields = ''.join('<option value="%s">%s (%s / %s / %s)</option>' % (
        html.escape(n), html.escape(n), html.escape(m['unit']),
        html.escape(m['window']), html.escape(m['available_at']))
        for n, m in sorted(field_catalog().items()))
    ops = ''.join('<option value="%s">%s</option>' % (html.escape(o), html.escape(o))
                  for o in sorted(OPERATORS))
    return """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>Stargazer 纯规则实验台</title>
<style>
body{font:14px/1.6 -apple-system,Segoe UI,sans-serif;margin:24px;max-width:1050px;color:#222}
h1{font-size:20px}h2{font-size:16px;margin-top:28px}
fieldset{border:1px solid #ccc;border-radius:6px;margin:12px 0;padding:12px}
input,select,button{padding:5px 8px;margin:3px}table{border-collapse:collapse;width:100%%}
td,th{border:1px solid #ddd;padding:5px 8px;text-align:left;font-size:12px}
code{background:#f5f5f5;padding:1px 4px}pre{background:#f7f7f7;padding:10px;overflow:auto}
.muted{color:#666}
</style></head><body>
<h1>Stargazer 纯规则实验台（独立工作区）</h1>
<p class="muted">运行时仅标准库 · 无模型 / 无 LLM / 无 webhook · 正式 Stargazer 只读 ·
所有写入限定在 D:\\stargazer\\stagazer_r</p>

<h2>1. 新建规则（条件树 / 阈值 / 因子组合）</h2>
<form method="post" action="/api/rules" id="f">
<label>规则 ID <input name="rule_id" value="my_rule" required></label>
<label>版本 <input name="version" value="v1"></label>
<label>标题 <input name="title" value="我的规则"></label>
<label>逻辑 <select name="logic"><option>AND</option><option>OR</option></select></label>
<label>引用因子（逗号分隔，name@f1）</label>
<input name="factor_refs" size="80" value="ret_60d@f1,dist_60h@f1,atr14@f1,ma20_slope5@f1,vol_ratio_5d@f1,zone_width_atr@f1">
<div id="rows"></div>
<button type="button" onclick="addRow()">+ 增加条件</button>
<button type="submit">生成草稿</button>
</form>
<script>
const FIELDS=%s, OPS=%s;
function addRow(){const d=document.createElement('div');
 d.innerHTML='<select name="field">'+FIELDS+'</select><select name="op">'+OPS+'</select>'+
 '<input name="value" value="0" size="10"><button type="button" onclick="this.parentNode.remove()">删除</button>';
 document.getElementById('rows').appendChild(d);}
addRow();addRow();
</script>

<h2>2. 规则版本（发布后不可变）</h2>
<form method="get" action="/api/rules"><button type="submit">刷新规则列表</button></form>
<form method="post" action="/api/publish">
<label>规则 ID <input name="rule_id" value="my_rule"></label>
<label>版本 <input name="version" value="" placeholder="留空自动递增"></label>
<button type="submit">发布</button></form>
<form method="post" action="/api/edit">
<label>规则 ID <input name="rule_id" value="my_rule"></label>
<label>路径 <input name="path" value="selection.children.0.value" size="34"></label>
<label>新值 <input name="value" value="0.15"></label><button type="submit">改阈值（草稿）</button></form>

<h2>3. 逐项预览（当前值 / 结果 / 未命中原因）</h2>
<form method="get" action="/api/preview">
<label>规则 <input name="rule" value="strong_pullback@v1"></label>
<label>代码 <input name="code" value="600000"></label>
<label>P 日 <input name="day" value="20261110"></label>
<button type="submit">预览</button></form>

<h2>4. 环境与源码指纹</h2>
<p><a href="/api/health">/api/health</a> · <a href="/api/fields">/api/fields</a> ·
<a href="/api/source">/api/source</a></p>
</body></html>""" % (json.dumps(fields), json.dumps(ops))


class Handler(BaseHTTPRequestHandler):
    server_version = 'RulesLab/1.0'

    def log_message(self, fmt, *args):
        sys.stderr.write('[rules_lab] ' + (fmt % args) + '\n')

    def _json(self, value, status=200):
        raw = json.dumps(value, ensure_ascii=False, indent=1).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _html(self, text, status=200):
        raw = text.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _form(self):
        size = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(size).decode('utf-8') if size else ''
        if 'application/json' in (self.headers.get('Content-Type') or ''):
            return json.loads(body or '{}')
        return {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == '/':
                return self._html(_page())
            if u.path == '/api/health':
                return self._json(lab_env(getattr(self.server, 'lab_port', DEFAULT_PORT)))
            if u.path == '/api/fields':
                return self._json(dict(fields=field_catalog(),
                                       operators=OPERATORS,
                                       logic_ops=list(LOGIC_OPS)))
            if u.path == '/api/rules':
                return self._json(rules.list_rules())
            if u.path == '/api/source':
                return self._json(rules.source_fingerprint())
            if u.path == '/api/preview':
                rule = rules.get(q.get('rule', 'strong_pullback@v1'))
                snap = open_snapshot()
                rows = snap.daily_asof(q.get('code', '600000'),
                                       int(q.get('day', 20261110)))
                env = compute_selection_factors(rows, rule['zone'])
                apply_formula_factors(env, rule)
                return self._json(preview(rule, env, 'selection'))
            return self._json(dict(error='NOT_FOUND', path=u.path), 404)
        except Exception as exc:                       # noqa: BLE001
            return self._json(dict(error=type(exc).__name__,
                                   detail=str(exc)[:400]), 400)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            form = self._form()
            if u.path == '/api/rules':
                rows = []
                fields = form.get('field') or []
                if isinstance(fields, str):
                    fields = [fields]
                ops = form.get('op') or []
                if isinstance(ops, str):
                    ops = [ops]
                values = form.get('value') or []
                if isinstance(values, str):
                    values = [values]
                for f, o, v in zip(fields, ops, values):
                    try:
                        value = json.loads(v)
                    except (ValueError, TypeError):
                        value = v
                    rows.append(dict(field=f, op=o, value=value))
                payload = dict(rule_id=form.get('rule_id'),
                               version=form.get('version'),
                               title=form.get('title'),
                               logic=form.get('logic'),
                               rows=rows,
                               factor_refs=[x.strip() for x in
                                            str(form.get('factor_refs', '')).split(',')
                                            if x.strip()])
                return self._json(rules.build_from_form(payload), 201)
            if u.path == '/api/publish':
                return self._json(rules.publish(
                    form['rule_id'], version=form.get('version') or None), 201)
            if u.path == '/api/edit':
                value = json.loads(form['value']) if \
                    str(form['value']).strip().startswith(('[', '{')) else \
                    (float(form['value']) if '.' in form['value'] else
                     (int(form['value']) if form['value'].lstrip('-').isdigit()
                      else form['value']))
                return self._json(rules.edit(form['rule_id'],
                                             {form['path']: value}), 201)
            return self._json(dict(error='NOT_FOUND', path=u.path), 404)
        except Exception as exc:                       # noqa: BLE001
            return self._json(dict(error=type(exc).__name__,
                                   detail=str(exc)[:400]), 400)


def serve(port=DEFAULT_PORT):
    atomic_write_json(ENV_FILE, lab_env(port))
    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    server.lab_port = port
    print('rules_lab form: http://127.0.0.1:%d/ (Ctrl+C to stop)' % port)
    print('env:', json.dumps(lab_env(port), ensure_ascii=False))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
