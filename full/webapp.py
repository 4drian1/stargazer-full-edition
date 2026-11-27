"""完整版规则工作台（第六轮）：编辑 → 预览 → 发布 → 全流程运行。

复用 rules_lab 指标目录与解释器；盘后筛选与盘中触发分别编辑；
支持加删条件、AND/OR 一级分组；因子带单位/周期/可用时间；
缺数据显示待核验原因。草稿与发布版本持久化在 run/state/rules，
重启后可找回；已冻结计划绑定原 rule_ref+sha 不受影响。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Lock

FE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FE.parent))
sys.path.insert(0, str(FE))

from rules_lab import rules  # noqa: E402
from rules_lab.factors import factor_catalog  # noqa: E402
from rules_lab.paths import SNAPSHOT, ensure_dirs  # noqa: E402
from rules_lab.selection import run_selection  # noqa: E402
from rules_lab.snapshot import open_snapshot  # noqa: E402
from rules_lab.state_store import RunContext  # noqa: E402
from full.agent_lock import SnapshotLock  # noqa: E402

BASE_RULE = 'lvp_golden'  # 不带版本号：按规则索引解析最新已发布版本（v1/v2…），包内外都有效
_PLAN_LOCK = Lock()
_DATA_LOCK = SnapshotLock()


def check_agent_binding(body):
    binding = body.get('_agent_binding')
    if not binding:
        return
    if not isinstance(binding,dict) or binding.get('snapshot') != snapshot_manifest()['snapshot_fingerprint']:
        raise ValueError('STALE_SNAPSHOT')
    if binding.get('rule_sha') and binding['rule_sha'] != rules.get(body['rule'])['sha256']:
        raise ValueError('STALE_RULE')


def _run_directory(root, ref=None):
    if ref is not None:
        import re
        if not isinstance(ref,str) or not re.fullmatch('[a-f0-9]{16}',ref):
            raise ValueError('INVALID_RUN_ID')
        path = root/ref
        return path if (path/'run_manifest.json').is_file() else None
    path = max(root.glob('*/run_manifest.json'),key=lambda p:p.stat().st_mtime,default=None)
    return path.parent if path else None

# 「选周期 → 选指标 → 写条件」的界面规格：分类、中文名、句式与口径说明。
# 字段 ID/阈值/版本仍按原口径读写；这里只影响展示与选择。
FIELD_UI = {
    # 日 K 选股
    'close': dict(label='收盘价', period='日K', category='日K选股', unit='元'),
    'open': dict(label='开盘价', period='日K', category='日K选股', unit='元'),
    'high': dict(label='最高价', period='日K', category='日K选股', unit='元'),
    'low': dict(label='最低价', period='日K', category='日K选股', unit='元'),
    'pre_close': dict(label='昨收', period='日K', category='日K选股', unit='元'),
    'volume': dict(label='成交量', period='日K', category='日K选股', unit='股'),
    'amount': dict(label='成交额', period='日K', category='日K选股', unit='元'),
    'pct_chg': dict(label='涨跌幅', period='日K', category='日K选股', unit='百分比'),
    'float_mv': dict(label='流通市值', period='日K', category='日K选股', unit='元',
                     note='当前快照字段，历史时点非 PIT 权威'),
    'is_st': dict(label='是否 ST', period='日K', category='日K选股', unit='0/1'),
    'history_days': dict(label='历史交易日数', period='日K', category='日K选股', unit='天'),
    'ret_5d': dict(label='近5日涨幅', period='日K', category='日K选股', unit='比率'),
    'ret_20d': dict(label='近20日涨幅', period='日K', category='日K选股', unit='比率'),
    'ret_60d': dict(label='近60日涨幅', period='日K', category='日K选股', unit='比率'),
    'dist_20h': dict(label='距近20日最高价', period='日K', category='日K选股', unit='比率',
                     note='-8% 表示低于高点 8%'),
    'dist_60h': dict(label='距近60日最高价', period='日K', category='日K选股', unit='比率',
                     note='-8% 表示低于高点 8%'),
    'atr14': dict(label='近14日平均真实波幅', period='日K', category='日K选股', unit='元'),
    'ma20': dict(label='20日均线', period='日K', category='日K选股', unit='元'),
    'ma60': dict(label='60日均线', period='日K', category='日K选股', unit='元'),
    'close_above_ma20': dict(label='收盘价>20日均线', period='日K', category='日K选股', unit='0/1',
                             note='句式示例：日 K · 收盘价 > 20 日均线'),
    'rsi6': dict(label='RSI(6)', period='日K', category='日K选股', unit='RSI点'),
    'macd_dif': dict(label='MACD DIF', period='日K', category='日K选股', unit='元'),
    'macd_dea': dict(label='MACD DEA', period='日K', category='日K选股', unit='元'),
    'macd_hist': dict(label='MACD 柱', period='日K', category='日K选股', unit='元'),
    'ma20_slope5': dict(label='20日均线5日斜率', period='日K', category='日K选股', unit='比率'),
    'vol_ratio_5d': dict(label='5日量/20日量', period='日K', category='日K选股', unit='比率'),
    'daily_turnover_proxy': dict(label='成交额÷流通市值（近似换手）', period='日K',
                                 category='日K选股', unit='比率',
                                 note='现有字段的真实口径；真正累计换手率待当时流通股本'),
    # 分钟监控
    'quote_fresh': dict(label='行情新鲜', period='5分钟K', category='分钟监控', unit='0/1'),
    'bar_close': dict(label='当根收盘价', period='5分钟K', category='分钟监控', unit='元'),
    'bar_low': dict(label='当根最低价', period='5分钟K', category='分钟监控', unit='元'),
    'bar_high': dict(label='当根最高价', period='5分钟K', category='分钟监控', unit='元'),
    'bar_volume': dict(label='当根成交量', period='5分钟K', category='分钟监控', unit='股'),
    'decline_from_prev_close': dict(label='距昨收跌幅', period='5分钟K', category='分钟监控',
                                    unit='比率', note='1 - 收盘/昨收'),
    'bar_count': dict(label='当日已走完K线数', period='5分钟K', category='分钟监控', unit='根'),
    'minute_volume_ratio': dict(label='量比（口径A）', period='5分钟K', category='量比与累计',
                                unit='比率', note='这一根量 ÷ 今日已走完K线均量'),
    'same_time_vol_ratio': dict(label='量比（口径B）', period='5分钟K', category='量比与累计',
                                unit='比率', note='今日同刻累计量 ÷ 历史同刻均量；'
                                                 '需多日分钟线，当前快照待核验'),
    'cum_turnover_proxy': dict(label='当日累计换手（近似）', period='5分钟K', category='量比与累计',
                               unit='比率', note='当日累计成交额 ÷ 流通市值'),
    # 分钟技术指标（5/15/30；15/30 由本地 5 分钟线聚合，只用完整聚合K线）
    'm5_rsi6': dict(label='RSI(6)', period='5分钟K', category='分钟监控', unit='RSI点'),
    'm5_ma5': dict(label='MA(5)', period='5分钟K', category='分钟监控', unit='元'),
    'm5_ma20': dict(label='MA(20)', period='5分钟K', category='分钟监控', unit='元'),
    'm5_macd_dif': dict(label='MACD DIF', period='5分钟K', category='分钟监控', unit='元'),
    'm5_macd_dea': dict(label='MACD DEA', period='5分钟K', category='分钟监控', unit='元'),
    'm5_macd_hist': dict(label='MACD 柱', period='5分钟K', category='分钟监控', unit='元'),
    'm15_rsi6': dict(label='RSI(6)', period='15分钟K', category='分钟监控', unit='RSI点'),
    'm15_ma5': dict(label='MA(5)', period='15分钟K', category='分钟监控', unit='元'),
    'm15_macd_dif': dict(label='MACD DIF', period='15分钟K', category='分钟监控', unit='元'),
    'm15_macd_dea': dict(label='MACD DEA', period='15分钟K', category='分钟监控', unit='元'),
    'm15_macd_hist': dict(label='MACD 柱', period='15分钟K', category='分钟监控', unit='元'),
    'm30_rsi6': dict(label='RSI(6)', period='30分钟K', category='分钟监控', unit='RSI点'),
    'm30_ma5': dict(label='MA(5)', period='30分钟K', category='分钟监控', unit='元'),
    'm30_macd_dif': dict(label='MACD DIF', period='30分钟K', category='分钟监控', unit='元'),
    'm30_macd_dea': dict(label='MACD DEA', period='30分钟K', category='分钟监控', unit='元'),
    'm30_macd_hist': dict(label='MACD 柱', period='30分钟K', category='分钟监控', unit='元'),
    # 黄金观察区
    'zone_distance_atr': dict(label='距观察区（ATR倍数）', period='5分钟K', category='黄金观察区',
                              unit='ATR倍数', note='收盘在带内为 0；带外为距最近沿的 ATR 倍数'),
    'price_in_zone': dict(label='是否到位（带内容差内）', period='5分钟K', category='黄金观察区', unit='0/1'),
    'dist_to_zone_upper_pct': dict(label='距上沿', period='5分钟K', category='黄金观察区',
                                   unit='百分比', note='(收盘-上沿)/上沿；带内为负'),
    'dist_to_zone_lower_pct': dict(label='距下沿', period='5分钟K', category='黄金观察区',
                                   unit='百分比', note='(收盘-下沿)/下沿；带内为正'),
    'zone_lower': dict(label='观察区下沿', period='日K', category='黄金观察区', unit='元'),
    'zone_upper': dict(label='观察区上沿', period='日K', category='黄金观察区', unit='元'),
    'zone_width_atr': dict(label='观察区宽度（ATR）', period='日K', category='黄金观察区', unit='ATR倍数'),
}


def _scratch(tag):
    import hashlib
    rid = 'web_' + tag + '_' + hashlib.sha256(tag.encode()).hexdigest()[:8]
    return RunContext(rid, meta=dict(scratch=True))


def _strip_attach(rule):
    """「随提醒显示」的条件：不参与触发判断，字段名进 notification.attach_fields，
    提醒时按触发时点因子值附带展示。"""
    attach = []
    trig = rule.get('trigger') or {}
    children = []
    for c in trig.get('children', []):
        if isinstance(c, dict) and c.pop('_attach', False):
            attach.append(c.get('field'))
        else:
            children.append(c)
    trig['children'] = children
    if attach:
        rule.setdefault('notification', {})['attach_fields'] = attach
    return rule


def _selection_with(rule, p_day):
    snap = open_snapshot()
    run = _scratch('preview_sel_%s' % rule['rule_id'])
    sel = run_selection(snap, rule, int(p_day), run)
    vals = {}
    for code, row in (sel.get('rows') or {}).items():
        v = {}
        for cond in row.get('conditions', []):
            v[cond.get('field')] = cond.get('actual')
        vals[code] = v
    return set(sel['accepted']), vals, sel.get('universe_count')


def _monitor_receipt_count(rule, day):
    """用监控日重放统计草稿规则的触位/回执数（同解释器，不发布草稿）。

    直接驱动内核：选股→冻结→监控→收件箱，全在 scratch 运行目录。"""
    import hashlib
    from rules_lab.freeze import run_freeze
    from rules_lab.monitor import run_monitor
    from rules_lab.state_store import EventStore, LocalInbox, RunContext
    snap = open_snapshot()
    P = snap.prev_trading_day(int(day))
    rid = 'web_mon_%s_%s' % (rule['rule_id'], hashlib.sha256(
        json.dumps(rule.get('trigger', {}), sort_keys=True).encode()).hexdigest()[:8])
    run = RunContext(rid, meta=dict(scratch=True))
    run.reset()
    sel = run_selection(snap, rule, P, run, day=int(day))
    freeze = run_freeze(snap, rule, P, int(day), sel, run)
    inbox = LocalInbox(run)
    events = EventStore(run, int(day))
    mon = run_monitor(snap, rule, freeze, run, inbox, events, restart=False)
    return dict(candidates=freeze['candidate_count'],
                touches=mon['counts']['touch_events'],
                receipts=inbox.counts())


def _bootstrap_rules():
    """启动时把 rules/ 下尚未发布的种子规则全部导入并发布（已发布的完全不动）。"""
    from rules_lab.paths import STATE
    from rules_lab.factors import FACTOR_REGISTRY
    published = STATE / 'rules' / 'published'
    done = []
    for seed in sorted((FE / 'rules').glob('*.json')):
        try:
            raw = json.loads(seed.read_text(encoding='utf-8'))
            rid, ver = raw.get('rule_id'), raw.get('version')
            if not rid or not ver:
                continue
            if (published / ('%s@%s.json' % (rid, ver))).is_file():
                continue
            rules.import_rule(str(seed))
            done.append(rules.publish(rid, version=ver, registered=FACTOR_REGISTRY).get('ref'))
        except Exception:
            continue
    return done



def _data_file(name, default):
    p = FE / 'data' / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.is_file():
        p.write_text(json.dumps(default, ensure_ascii=False), encoding='utf-8')
    try:
        return p, json.loads(p.read_text(encoding='utf-8'))
    except Exception:
        return p, default


def model_switch():
    return _data_file('model_switch.json', dict(selection=False, risk=False))


def _default_tdx_path():
    try:
        from full.vipdoc_reader import vipdoc_root
        return str(vipdoc_root())
    except Exception:
        return 'D:/stargazer/market_data/vipdoc'


def source_config():
    return _data_file('source_config.json', dict(
        source='tdx', tdx_path=_default_tdx_path(),
        stockdb_base='http://127.0.0.1:7899'))


def snapshot_manifest():
    return json.loads((SNAPSHOT / 'snapshot_manifest.json').read_text(encoding='utf-8'))


def default_p_day():
    return int(snapshot_manifest().get('p_day') or 0)


def default_monitor_day():
    return int(snapshot_manifest().get('monitor_day') or 0)


def recent_trading_days(n=5, end=None):
    """最近 n 个不晚于 end（默认 P 日）的交易日，旧→新。"""
    days = json.loads((SNAPSHOT / 'calendar.json').read_text(encoding='utf-8'))['days']
    end = int(end or default_p_day() or 0)
    elig = [int(d) for d in days if int(d) <= end]
    return elig[-n:]


def create_app(sync_daily=None):
    from flask import Flask, jsonify, request, Response
    app = Flask('full_edition_web', static_folder=str(FE / 'full' / 'static'),
                static_url_path='/static')
    ensure_dirs()
    _bootstrap_rules()

    @app.after_request
    def no_cache(resp):
        resp.headers['Cache-Control'] = 'no-store'
        return resp

    @app.route('/')
    def index():
        return Response((FE / 'full' / 'static' / 'index.html').read_text(encoding='utf-8'),
                        mimetype='text/html')

    @app.route('/api/factors')
    def api_factors():
        cat = factor_catalog()
        ui = {k: dict(v, spec=cat.get(k)) for k, v in FIELD_UI.items() if k in cat}
        for k, meta in cat.items():
            ui.setdefault(k, dict(label=k, period=meta.get('granularity', ''),
                                  category='其他', unit=meta.get('unit', ''),
                                  note=meta.get('window', ''), spec=meta))
        periods = ['日K', '5分钟K', '15分钟K', '30分钟K']
        categories = ['日K选股', '分钟监控', '量比与累计', '黄金观察区', '其他']
        return jsonify(dict(catalog=cat, ui=ui, periods=periods, categories=categories,
                            ops=['>=', '<=', '>', '<', '==', '!=']))

    @app.route('/api/rule')
    def api_rule():
        ref = request.args.get('ref') or BASE_RULE
        from rules_lab.paths import STATE, read_json
        idx_f = STATE / 'rules' / 'index.json'
        idx = read_json(idx_f) if idx_f.is_file() else {}
        rid = ref.split('@')[0]
        draft_rel = (idx.get('drafts') or {}).get(rid)
        if draft_rel and '@' not in ref:
            rule = read_json(FE / draft_rel)
            return jsonify(dict(rule=rule, draft=True))
        try:
            rule = rules.get(ref)
        except Exception as exc:
            return jsonify(dict(error=str(exc)[:120])), 404
        return jsonify(dict(rule=rule))

    @app.route('/api/tarot_status')
    def api_tarot_status():
        import importlib.util
        profile = FE.parents[1] / 'stargazer_ml' / 'v5_hybrid' / 'green_common_profile'
        repo_ok = (profile / 'tarot_live_v2.py').is_file()
        sklearn_ok = importlib.util.find_spec('sklearn') is not None
        return jsonify(dict(available=bool(repo_ok and sklearn_ok),
                            repo_ok=repo_ok, sklearn_ok=sklearn_ok,
                            note='Tarot 影子需要主仓库权重与 sklearn；不可用时不影响纯规则链路'))

    @app.route('/api/rules')
    def api_rules():
        from rules_lab.paths import STATE, read_json
        index_f = STATE / 'rules' / 'index.json'
        idx = read_json(index_f) if index_f.is_file() else {}
        published = []
        for p in sorted((STATE / 'rules' / 'published').glob('*.json')):
            try:
                r = read_json(p)
                published.append(dict(ref=p.name.replace('.json', ''), title=r.get('title') or ''))
            except Exception:
                published.append(dict(ref=p.name.replace('.json', ''), title=''))
        drafts = sorted((idx.get('drafts') or {}).keys())
        return jsonify(dict(index=idx, published=published, drafts=drafts))

    @app.route('/api/rule/draft', methods=['POST'])
    def api_draft():
        body = request.get_json(force=True)
        rule = _strip_attach(body['rule'])
        # 服务端校验：契约 + 因子已登记 + 阈值类型
        from rules_lab import contract
        try:
            contract.validate_rule(rule)
            registered = {n for n in factor_catalog()}
            for section in ('selection', 'trigger'):
                for child in rule.get(section, {}).get('children', []):
                    if child.get('field') not in registered:
                        raise ValueError('UNKNOWN_FIELD:' + str(child.get('field')))
        except Exception as exc:
            return jsonify(dict(ok=False, error='%s:%s' % (type(exc).__name__, str(exc))[:160])), 400
        draft_path = FE / 'run' / 'build' / ('%s_draft.json' % rule['rule_id'])
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        draft_path.write_text(json.dumps(rule, ensure_ascii=False), encoding='utf-8')
        rules.import_rule(str(draft_path))
        return jsonify(dict(ok=True, rule_id=rule['rule_id'],
                            note='草稿已保存（重启后仍在）；点发布生成新版本'))

    @app.route('/api/rule/preview', methods=['POST'])
    def api_preview():
        body = request.get_json(force=True)
        rule = body['rule']
        p_day = int(body.get('p_day') or default_p_day())
        d_day = int(body.get('d_day') or default_monitor_day())
        base_ref = body.get('base') or BASE_RULE
        base_rule = rules.get(base_ref)
        rule = _strip_attach(dict(rule, rule_id=base_rule['rule_id'] + '_draft',
                                  version='v1', status='DRAFT'))
        try:
            base_set, base_vals, uni = _selection_with(base_rule, p_day)
            edit_set, edit_vals, _ = _selection_with(rule, p_day)
        except Exception as exc:
            return jsonify(dict(ok=False, error='%s:%s' % (type(exc).__name__, str(exc))[:160])), 400
        gained = sorted(edit_set - base_set)
        lost = sorted(base_set - edit_set)
        unknown_n = sum(1 for v in edit_vals.values()
                        if any(x is None for x in v.values()))
        # 盘中差异：同解释器对监控日重放，给出触位/回执增减与原因
        trigger_diff = None
        try:
            base_mon = _monitor_receipt_count(dict(base_rule), d_day)
            edit_mon = _monitor_receipt_count(rule, d_day)
            trigger_diff = dict(base=base_mon, edited=edit_mon,
                                note='盘中条件改动在发布前即可看到提醒数量变化；'
                                     '逐笔明细在运行后的收件箱')
        except Exception as exc:
            trigger_diff = dict(error='%s:%s' % (type(exc).__name__, str(exc))[:160])
        return jsonify(dict(ok=True, p_day=p_day, d_day=d_day, universe=uni,
                            base_n=len(base_set), edited_n=len(edit_set),
                            gained=[dict(code=c, values=edit_vals.get(c, {})) for c in gained[:30]],
                            lost=[dict(code=c, values=base_vals.get(c, {})) for c in lost[:30]],
                            unknown_values=unknown_n,
                            trigger=trigger_diff))

    @app.route('/api/rule/publish', methods=['POST'])
    def api_publish():
        body = request.get_json(force=True)
        rule_id = body['rule_id']
        idx = rules._read_index()
        if rule_id not in idx.get('drafts', {}):
            return jsonify(dict(ok=False, error='NO_DRAFT:' + rule_id)), 400
        n = 1
        while (FE / 'run' / 'state' / 'rules' / 'published' / ('%s@v%d.json' % (rule_id, n))).is_file():
            n += 1
        p = rules.publish(rule_id, version='v%d' % n, registered=None)
        return jsonify(dict(ok=True, ref=p['ref'], sha256=p['sha256'][:16],
                            note='已冻结计划仍绑定原版本；新计划用新版本'))

    @app.route('/api/run', methods=['POST'])
    def api_run():
        body = request.get_json(force=True) or {}
        ref = body.get('rule') or BASE_RULE
        days = body.get('days') or [default_monitor_day()]
        with_shadow = bool(body.get('with_tarot_shadow', model_switch()[1].get('selection', False)))
        import full.cli as cli
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')), 409
        try:
            check_agent_binding(body)
            argv = ['run', '--rule', ref, '--days'] + [str(int(d)) for d in days]
            if body.get('_agent_binding'):
                argv.append('--require-minutes')
            if with_shadow:
                argv.append('--with-tarot-shadow')
            result = {}
            rc = cli.main(argv,result=result)
        except (TypeError, ValueError, OSError, SystemExit) as exc:
            return jsonify(dict(ok=False,error=str(exc)[:220],mode='REPLAY')), 409
        finally:
            _DATA_LOCK.release()
        return jsonify(dict(ok=rc==0,mode='REPLAY',run_id=result.get('run_id'),
                            error=result.get('error'),
                            push='历史回放使用本地收件箱' if rc==0 else None))


    @app.route('/api/model_switch', methods=['GET', 'POST'])
    def api_model_switch():
        p, doc = model_switch()
        if request.method == 'POST':
            body = request.get_json(force=True) or {}
            if body.get('risk'):
                return jsonify(dict(ok=False,error='完整版风险模型入口尚未接入')), 409
            doc.update(selection=bool(body.get('selection', doc.get('selection'))),risk=False)
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        return jsonify(dict(doc,risk=False,risk_available=False))

    @app.route('/api/source/config', methods=['GET', 'POST'])
    def api_source_config():
        p, doc = source_config()
        if request.method == 'POST':
            body = request.get_json(force=True) or {}
            if body.get('source') in ('tdx', 'stockdb'):
                doc['source'] = body['source']
            if body.get('tdx_path'):
                root = Path(str(body['tdx_path']).strip()).expanduser()
                if not any((root/venue/'lday').is_dir() for venue in ('sh','sz','bj')):
                    return jsonify(dict(ok=False,error='请选择含 sh/lday 或 sz/lday 的通达信 vipdoc 目录')), 400
            for k in ('tdx_path', 'stockdb_base'):
                if body.get(k):
                    doc[k] = str(body[k]).strip()
            p.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
        return jsonify(dict(doc,active_source='tdx',stockdb_research_only=True))

    @app.route('/api/source/diagnostics')
    def api_source_diagnostics():
        import time
        from full.vipdoc_reader import vipdoc_root
        doc = source_config()[1]
        out = dict(config=doc, tdx=dict(path=str(vipdoc_root())), stockdb=dict())
        vip = vipdoc_root()
        n = 0
        for ex in ('sh', 'sz', 'bj'):
            d = vip / ex / 'lday'
            if d.is_dir():
                n += sum(1 for _ in d.glob('*.day'))
        out['tdx']['daily_files'] = n
        try:
            import sys as _sys
            root = str(FE.parents[1])
            if root not in _sys.path:
                _sys.path.insert(0, root)
            from _stockdb import make_opener, http_get_raw, recs_of
            t0 = time.time()
            recs = recs_of(http_get_raw(make_opener(), doc.get('stockdb_base') or 'http://127.0.0.1:7899', '日k', '300118', '*', timeout=3))
            dates = sorted({int(r['date']) for r in recs if r.get('date')})
            out['stockdb'].update(ok=True, ms=int((time.time() - t0) * 1000),
                                  rows=len(recs), first=dates[0] if dates else None,
                                  last=dates[-1] if dates else None,
                                  note='历史个股可读' if dates else '空表')
        except Exception as exc:
            out['stockdb'].update(ok=False, reason='%s:%s' % (type(exc).__name__, str(exc)[:80]),
                                  note='连接失败或服务未启动')
        out['stockdb']['index'] = dict(status='未验证', note='指数表未做读取验证')
        out['verdict'] = ('当前数据源为通达信：可选股、可回放'
                          if doc.get('source') == 'tdx'
                          else 'StockDB：较新日期可能缺失，仅建议离线研究；选股回放仍绑定最近通达信快照')
        return jsonify(out)

    def _task_log(name, detail):
        import time as _t
        td = FE / 'data' / 'tasks'
        td.mkdir(parents=True, exist_ok=True)
        tid = 'task_%s_%d' % (_t.strftime('%Y%m%d_%H%M%S'),_t.time_ns())
        rec = dict(task_id=tid, name=name, started_at=_t.strftime('%H:%M:%S'),
                   finished_at=_t.strftime('%H:%M:%S'), detail=detail)
        (td / (tid + '.json')).write_text(json.dumps(rec, ensure_ascii=False, indent=1), encoding='utf-8')
        return tid

    @app.route('/api/data/tasks')
    def api_data_tasks():
        td = FE / 'data' / 'tasks'
        out = []
        if td.is_dir():
            for f in sorted(td.glob('*.json'), reverse=True)[:10]:
                try:
                    out.append(json.loads(f.read_text(encoding='utf-8')))
                except Exception:
                    pass
        from full.mcp_tools import recent_tasks
        from datetime import datetime
        for job in recent_tasks():
            out.append(dict(task_id=job['task_id'],name=job['action'],
                            started_at=datetime.fromtimestamp(job['created_at']).isoformat(timespec='seconds'),
                            detail={'status':job['status']}))
        return jsonify(dict(tasks=out))

    @app.route('/api/research/mine', methods=['POST'])
    def api_mine():
        body = request.get_json(force=True) or {}
        from full.mining import mine
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')),409
        try:
            check_agent_binding(body)
            rep = mine(body.get('rule') or BASE_RULE,
                       p_days=[int(d) for d in (body.get('days') or recent_trading_days(5))],
                       purge=int(body.get('purge') or 1))
        except Exception as exc:
            return jsonify(dict(ok=False, error='%s:%s' % (type(exc).__name__, str(exc))[:160]))
        finally:
            _DATA_LOCK.release()
        return jsonify(rep)

    @app.route('/api/state')
    def api_state():
        from rules_lab.paths import STATE
        runs_root = STATE / 'runs'
        n, run_id = 0, None
        if runs_root.is_dir():
            rs = sorted(runs_root.glob('*/inbox/receipts.jsonl'), key=lambda p: p.stat().st_mtime)
            if rs:
                run_id = rs[-1].parents[1].name
                n = sum(1 for _ in rs[-1].open(encoding='utf-8'))
        man = json.loads((SNAPSHOT / 'snapshot_manifest.json').read_text(encoding='utf-8'))
        # 数据更新至哪天（快照日线最大日期，抽样探测）
        data_through = None
        try:
            import gzip
            d = json.loads(gzip.decompress((SNAPSHOT / 'daily.json.gz').read_bytes()))
            di = d['columns'].index('date')
            data_through = max(rows[-1][di] for rows in d['codes'].values() if rows)
        except Exception:
            pass
        return jsonify(dict(app='full_edition_round8', port=17891, latest_run=run_id,
                            inbox_messages=n, snapshot_fingerprint=man['snapshot_fingerprint'][:16],
                            p_day=man.get('p_day'), monitor_day=man.get('monitor_day'),
                            minute_days=man.get('minute_days'),
                            data_through=data_through,
                            recent_days=recent_trading_days(7),
                            calendar_through=str(json.loads(
                                (SNAPSHOT / 'calendar.json').read_text(encoding='utf-8'))
                                ['days'][-1]),
                            model_switch=dict(model_switch()[1],risk=False,risk_available=False),
                            source=dict(source_config()[1],active_source='tdx',stockdb_research_only=True)))

    @app.route('/api/last_run')
    def api_last_run():
        """最近一次运行的分列：筛中 / 已冻结 / 未冻结原因 / 触位 / 回执。"""
        from rules_lab.paths import STATE
        rs = sorted((STATE / 'runs').glob('*/run_manifest.json'), key=lambda p: p.stat().st_mtime)
        if not rs:
            return jsonify(dict(error='NO_RUN'))
        run_dir = rs[-1].parent
        out = dict(run_id=run_dir.name)
        sel_files = sorted((run_dir / 'out').glob('selection_*.json'))
        if sel_files:
            s = json.loads(sel_files[-1].read_text(encoding='utf-8'))
            out['selected_n'] = s.get('accepted_count')
        fr_files = sorted((run_dir / 'freeze').glob('*.json'))
        if fr_files:
            fr = json.loads(fr_files[-1].read_text(encoding='utf-8'))
            out['frozen_n'] = fr.get('candidate_count')
            out['not_frozen'] = fr.get('dropped', [])
        ev_files = sorted((run_dir / 'events').glob('*.json'))
        if ev_files:
            ev = json.loads(ev_files[-1].read_text(encoding='utf-8'))
            rows = ev.get('events') or {}
            out['touches'] = sum(1 for v in rows.values()
                                 if (v.get('delivery_status') in ('SENT', 'UNKNOWN', 'EXPIRED'))
                                 and v.get('bar'))
            out['pending_events'] = sum(1 for v in rows.values()
                                        if v.get('delivery_status') in ('PENDING', 'SENDING'))
        mon_files = sorted((run_dir / 'out').glob('monitor_*.json'))
        if mon_files:
            m = json.loads(mon_files[-1].read_text(encoding='utf-8'))
            out['inbox'] = m.get('inbox_counts')
        return jsonify(out)

    @app.route('/api/data/scan', methods=['POST'])
    def api_data_scan():
        body = request.get_json(force=True) or {}
        src = Path(body.get('src') or (FE / 'import_dir'))
        if not src.is_dir():
            return jsonify(dict(ok=False, error='IMPORT_DIR_MISSING:' + str(src)))
        from full.data_center import scan, rule_window
        rep = scan(src, SNAPSHOT)
        rep['rule_check_window_days'] = rule_window(body.get('rule') or BASE_RULE)
        rep['task_id'] = _task_log('scan', dict(summary=rep.get('summary')))
        return jsonify(dict(ok=True, **rep))

    @app.route('/api/webhook/test', methods=['POST'])
    def api_webhook_test():
        from full.webhook import notify, webhook_url
        if not webhook_url():
            return jsonify(dict(ok=True, configured=False,
                                note='未配置 Webhook（默认本地收件箱）；'
                                     '配 data/webhook_url.txt 或 STARGAZER_WEBHOOK_URL 启用'))
        r = notify(dict(kind='WEBHOOK_TEST', event_id='webhook-test',
                        note='Stargazer 完整版 Webhook 联通测试'))
        return jsonify(dict(ok=bool(r.get('sent')), configured=True, result=r))

    @app.route('/api/data/runnable_days')
    def api_runnable_days():
        from full.data_center import strategy_runnable_days
        import json as _j
        body = request.get_json(silent=True) or {}
        return jsonify(strategy_runnable_days(SNAPSHOT, body.get('rule') or BASE_RULE))

    @app.route('/api/review')
    def api_review():
        from rules_lab.paths import run_root
        try:
            run_dir = _run_directory(run_root(),request.args.get('run_id'))
        except ValueError as exc:
            return jsonify(dict(ok=False,error=str(exc))),400
        if not run_dir:
            return jsonify(dict(ok=False, error='NO_RUN'))
        out = dict(ok=True, run_id=run_dir.name)
        sp = run_dir / 'out' / 'review_summary.json'
        if sp.is_file():
            out['summary'] = json.loads(sp.read_text(encoding='utf-8'))
        rp = run_dir / 'out' / 'review_samples.jsonl'
        rows = []
        if rp.is_file():
            for line in rp.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        out['samples'] = rows[-60:]
        out['total'] = len(rows)
        return jsonify(out)

    @app.route('/api/backtest/run', methods=['POST'])
    def api_backtest():
        body = request.get_json(force=True) or {}
        from rules_lab.paths import run_root,sha256_file
        from full.backtest import backtest_run
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')),409
        try:
            check_agent_binding(body)
            run_dir = _run_directory(run_root(),body.get('run_id'))
            if not run_dir:
                return jsonify(dict(ok=False,error='NO_RUN')),404
            stamp = (body.get('_agent_binding') or {}).get('run_sha')
            if stamp and stamp != sha256_file(run_dir/'run_manifest.json'):
                return jsonify(dict(ok=False,error='STALE_RUN')),409
            rep = backtest_run(run_dir)
            rep['run_id'] = run_dir.name
        except ValueError as exc:
            return jsonify(dict(ok=False,error=str(exc))),409
        finally:
            _DATA_LOCK.release()
        return jsonify(rep)

    @app.route('/api/research/evaluate', methods=['POST'])
    def api_research():
        body = request.get_json(force=True) or {}
        from full.factor_lib import FACTOR_LIBRARY, evaluate_days
        name = str(body.get('factor') or '')
        expr = str(body.get('expr') or '').strip()
        for fam in FACTOR_LIBRARY.values():
            if name in fam:
                expr = fam[name][0]
                break
        if not expr:
            expr = 'ret_5d'
        rule = body.get('rule') or BASE_RULE
        p_days = body.get('days') or [body.get('p_day') or default_p_day()]
        try:
            rep = evaluate_days(rule, expr, [int(d) for d in p_days])
        except Exception as exc:
            return jsonify(dict(ok=False, error='%s:%s' % (type(exc).__name__, str(exc))[:160]))
        return jsonify(rep)

    @app.route('/api/data/fill_gaps', methods=['POST'])
    def api_fill_gaps():
        body = request.get_json(force=True) or {}
        from full.gap_fill import fill_gaps
        days = body.get('days') or None
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')), 409
        try:
            check_agent_binding(body)
            rep = fill_gaps(SNAPSHOT, days=[int(d) for d in days] if days else None)
        except ValueError as exc:
            return jsonify(dict(ok=False,error=str(exc))),409
        finally:
            _DATA_LOCK.release()
        rep['task_id'] = _task_log('fill_gaps', dict(status=rep.get('status'), filled=rep.get('filled')))
        return jsonify(dict(ok=rep['status'] in ('PUBLISHED', 'NO_GAPS'), **rep))

    @app.route('/api/data/import', methods=['POST'])
    def api_data_import():
        body = request.get_json(force=True) or {}
        src = Path(body.get('src') or (FE / 'import_dir'))
        from full.data_center import import_daily
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')), 409
        try:
            check_agent_binding(body)
            rep = import_daily(src, SNAPSHOT, body.get('rule') or BASE_RULE,
                               include_minutes=body.get('include_minutes',True) is not False)
        except ValueError as exc:
            return jsonify(dict(ok=False,error=str(exc))),409
        finally:
            _DATA_LOCK.release()
        rep['task_id'] = _task_log('import', dict(status=rep.get('status'), imported=rep.get('imported'), error=rep.get('error')))
        return jsonify(rep)

    @app.route('/api/inbox')
    def api_inbox():
        from rules_lab.paths import STATE
        rs = sorted((STATE / 'runs').glob('*/inbox/receipts.jsonl'), key=lambda p: p.stat().st_mtime)
        out, run_id = [], None
        if rs:
            run_id = rs[-1].parents[1].name
            for line in rs[-1].read_text(encoding='utf-8').splitlines()[-50:]:
                out.append(json.loads(line))
        return jsonify(dict(run=run_id, messages=out))

    # ---------- 推送配置（钉钉机器人 / Pushplus，保存才生效） ----------
    @app.route('/api/push/config', methods=['GET', 'POST'])
    def api_push_config():
        from full.push_config import load, public_view, save
        if request.method == 'POST':
            body = request.get_json(force=True) or {}
            cur = load()
            # 空字符串字段保留原值（界面只回显掩码，不回传完整密钥）
            for k in ('dingtalk_webhook', 'dingtalk_secret', 'pushplus_token'):
                if body.get(k) is not None and str(body.get(k)).strip():
                    cur[k] = str(body[k]).strip()
            if body.get('channel') in ('none', 'dingtalk', 'pushplus'):
                cur['channel'] = body['channel']
            save(cur)
        return jsonify(public_view())

    @app.route('/api/push/test', methods=['POST'])
    def api_push_test():
        from full.push_config import send_summary
        r = send_summary('Stargazer 完整版推送测试',
                         '渠道联通测试：看到这条说明推送配置已生效。')
        return jsonify(r)

    # ---------- 指数 K 线（本地通达信，只读） ----------
    @app.route('/api/index_kline')
    def api_index_kline():
        symbol = str(request.args.get('symbol') or 'SSE').upper()
        period = request.args.get('period') or '1d'
        if period not in ('1d','5m'):
            return jsonify(dict(ok=False,error='BAD_INDEX_PERIOD')), 400
        try:
            from full.index_kline import STEMS, index_daily, index_minute
            bars, dropped = (index_minute if period=='5m' else index_daily)(symbol,120)
        except Exception as exc:
            return jsonify(dict(ok=False, error='%s:%s' % (type(exc).__name__, str(exc)[:120])))
        return jsonify(dict(ok=bool(bars), symbol=symbol, period=period, bars=bars,
                            dropped_records=dropped,
                            source='local_tdx_vipdoc',
                            source_file=STEMS[symbol]+('.lc5' if period=='5m' else '.day'),
                            error=None if bars else '未找到满足时间网格的指数 K 线',
                            last_complete_ts=bars[-1]['ts'] if bars else None))

    # ---------- 生产版交易计划只读引用 ----------
    @app.route('/api/plan/production')
    def api_plan_production():
        from full.plan_reference import load_production_plan
        return jsonify(load_production_plan())

    @app.route('/api/plan/select', methods=['POST'])
    def api_plan_select():
        from datetime import datetime
        from full.cli import _verify_snapshot
        from full.strong_pool import ZONE, _day_pair
        from full.vipdoc_reader import calendar_days
        from rules_lab.freeze import run_freeze
        from rules_lab.paths import STATE
        from rules_lab.state_store import make_run_id

        if not _PLAN_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='规则选股正在运行')), 409
        if not _DATA_LOCK.acquire(blocking=False):
            _PLAN_LOCK.release()
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')), 409
        try:
            now = datetime.now(ZONE)
            p_day, day = _day_pair(now,calendar_days())
            body = request.get_json(silent=True) or {}
            check_agent_binding(body)
            binding = body.get('_agent_binding') or {}
            if binding and (p_day,day) != (binding.get('p_day'),binding.get('observation_day')):
                return jsonify(dict(ok=False,error='STALE_OBSERVATION_DAY')),409
            if sync_daily and int(snapshot_manifest().get('daily_through') or 0) < p_day:
                if binding:
                    return jsonify(dict(ok=False,error='规则快照待同步，请更新数据后重新提交任务')),409
                sync_daily(p_day)
            _verify_snapshot()
            snap = open_snapshot(p_day,daily_only=True)
            if not any(snap.daily_row(c,p_day) for c in snap.codes()):
                return jsonify(dict(ok=False,error=f'规则快照缺 {p_day} 日线，请先导入')), 409
            rule = rules.get(body.get('rule') or BASE_RULE)
            run_id = make_run_id(mode='postclose_selection',day=day,rule=rule['sha256'],snapshot=snap.fingerprint)
            run = RunContext(run_id,root_base=STATE/'plans')
            universe = [c for c in snap.codes() if snap.daily_row(c,p_day)]
            selection = run_selection(snap,rule,p_day,run,universe=universe,day=day)
            plan = run_freeze(snap,rule,p_day,day,selection,run)
            from full.golden import golden_references
            for candidate in plan['candidates']:
                candidate['available_at'] = now.strftime('%Y%m%d%H%M%S')
                candidate['golden_references'] = golden_references(
                    snap.daily_asof(candidate['code'],p_day),candidate['reference_close'],candidate['atr14'] or 0.0)
            plan.update(generated_at=now.isoformat(),built_at=now.strftime('%Y%m%d%H%M%S'),source='LOCAL_RULE_SELECTION')
            run.write('freeze',f'{day}.json',value=plan)
            run.finish({'mode':'postclose_selection'})
            return jsonify(dict(ok=True,run_id=run_id,day=day,p_day=p_day,count=plan['candidate_count']))
        except (ValueError, KeyError, OSError, RuntimeError, SystemExit) as exc:
            return jsonify(dict(ok=False,error=str(exc)[:240])), 409
        finally:
            _DATA_LOCK.release()
            _PLAN_LOCK.release()

    # ---------- 盘后选股结果（冻结计划卡片） ----------
    @app.route('/api/plan/latest')
    def api_plan_latest():
        from datetime import datetime
        from rules_lab.paths import STATE,run_root
        from full.strong_pool import ZONE,_day_pair
        from full.vipdoc_reader import calendar_days,stock_names
        ref = request.args.get('run_id')
        try:
            if ref is None:
                rs = sorted((STATE/'plans').glob('*/freeze/*.json'),key=lambda p:p.stat().st_mtime)
                if not rs:
                    rs = sorted(run_root().glob('*/freeze/*.json'),key=lambda p:p.stat().st_mtime)
            else:
                path = _run_directory(STATE/'plans',ref) or _run_directory(run_root(),ref)
                rs = sorted((path/'freeze').glob('*.json')) if path else []
        except ValueError as exc:
            return jsonify(dict(ok=False,error=str(exc))),400
        if not rs:
            return jsonify(dict(ok=False, error='暂无规则候选，请先运行盘后选股'))
        d = json.loads(rs[-1].read_text(encoding='utf-8'))
        source = d.get('source') or 'REPLAY'
        expected = None
        plan_status = 'REPLAY'
        if source=='LOCAL_RULE_SELECTION':
            try:
                target_p,expected = _day_pair(datetime.now(ZONE),calendar_days())
                plan_status = ('READY' if (str(target_p),str(expected)) ==
                               (str(d.get('observation_day')),str(d.get('day'))) else 'STALE')
            except (OSError,ValueError,KeyError):
                plan_status = 'UNVERIFIED'
        names = {}
        try:
            names = stock_names()
        except Exception:
            pass
        cands = []
        for c in d.get('candidates', []):
            cands.append(dict(code=c.get('code'), name=names.get(c.get('code'), ''),
                              reference_close=c.get('reference_close'),
                              zone_lower=c.get('zone_lower'), zone_upper=c.get('zone_upper'),
                              golden_ratio=c.get('golden_ratio'), golden_node=c.get('golden_node'),
                              wave_low_day=c.get('wave_low_day'), wave_high_day=c.get('wave_high_day'),
                              atr14=c.get('atr14')))
        return jsonify(dict(ok=True, run_id=rs[-1].parents[1].name,
                            rule_ref=d.get('rule_ref'), day=d.get('day'),
                            p_day=d.get('observation_day'), generated_at=d.get('generated_at'), source=source,
                            status=plan_status,expected_observation_day=expected,
                            count=d.get('candidate_count'), dropped=d.get('dropped', []),
                            candidates=cands))

    # ---------- 模拟持仓（最近运行的成交信号） ----------
    @app.route('/api/positions')
    def api_positions():
        from rules_lab.paths import STATE
        rs = sorted((STATE / 'runs').glob('*/run_manifest.json'), key=lambda p: p.stat().st_mtime)
        if not rs:
            return jsonify(dict(ok=False, error='NO_RUN'))
        run_dir = rs[-1].parent
        cache = run_dir / 'out' / 'backtest_cache.json'
        rep = None
        if cache.is_file():
            try:
                rep = json.loads(cache.read_text(encoding='utf-8'))
            except Exception:
                rep = None
        if rep is None:
            from full.backtest import backtest_run
            rep = backtest_run(run_dir)
            try:
                cache.write_text(json.dumps(rep, ensure_ascii=False), encoding='utf-8')
            except Exception:
                pass
        rows = [r for r in rep.get('rows', []) if r.get('status') == 'FILLED']
        # 最新收盘（快照日线）
        import gzip as _gz
        daily = json.loads(_gz.decompress((SNAPSHOT / 'daily.json.gz').read_bytes()))
        cidx = daily['columns'].index('close')
        didx = daily['columns'].index('date')
        latest = {}
        for code, rr in daily['codes'].items():
            if rr:
                latest[code] = (rr[-1][didx], rr[-1][cidx])
        from full.vipdoc_reader import stock_names
        try:
            names = stock_names()
        except Exception:
            names = {}
        out = []
        for r in rows:
            day_close = latest.get(r['code'])
            entry = r.get('entry')
            pnl = ((day_close[1] - entry) / entry) if (day_close and entry) else None
            shares = int(100000 / entry / 100) * 100 if entry else 0  # 模拟：每笔 10 万元等额
            out.append(dict(code=r['code'], name=names.get(r['code'], ''),
                            entry_ts=r.get('entry_ts'), entry=entry,
                            shares=shares,
                            latest_close=day_close[1] if day_close else None,
                            latest_day=day_close[0] if day_close else None,
                            change_pct=((day_close[1] / entry - 1) * 100) if day_close else None,
                            pnl=round(shares * (day_close[1] - entry), 2) if (day_close and entry) else None,
                            pnl_pct=pnl))
        return jsonify(dict(ok=True, run_id=run_dir.name, positions=out,
                            note='模拟持仓=最近运行触位且回测成交的信号；每笔按 10 万元等额模拟股数，浮盈按快照最新收盘，非真实账户'))

    # ---------- 因子库总览（多因子 IC 一览，结果落缓存） ----------
    @app.route('/api/research/library', methods=['POST'])
    def api_research_library():
        body = request.get_json(force=True) or {}
        rule = body.get('rule') or BASE_RULE
        days = [int(d) for d in (body.get('days') or recent_trading_days(3))]
        import hashlib as _h
        key = _h.sha256((rule + '|' + ','.join(map(str, sorted(days)))).encode()).hexdigest()[:16]
        cd = FE / 'data' / 'research_cache'
        cd.mkdir(parents=True, exist_ok=True)
        cf = cd / ('lib_' + key + '.json')
        if cf.is_file():
            return jsonify(json.loads(cf.read_text(encoding='utf-8')))
        from full.factor_lib import FACTOR_LIBRARY, evaluate_days
        rows = []
        for fam, items in FACTOR_LIBRARY.items():
            for name, (expr, label) in items.items():
                try:
                    rep = evaluate_days(rule, expr, days)
                    sm = rep.get('summary') or {}
                    rows.append(dict(family=fam, name=name, label=label, expr=expr,
                                     ic=sm.get('ic'), n=sm.get('n'),
                                     ok=not rep.get('error'), error=rep.get('error')))
                except Exception as exc:
                    rows.append(dict(family=fam, name=name, label=label, expr=expr,
                                     ic=None, n=None, ok=False,
                                     error='%s:%s' % (type(exc).__name__, str(exc)[:120])))
        out = dict(ok=True, rule=rule, days=days, rows=rows,
                   note='IC=因子值与次日收益的按日 Spearman 相关；首次计算约数分钟，结果已缓存')
        cf.write_text(json.dumps(out, ensure_ascii=False), encoding='utf-8')
        return jsonify(out)

    # ---------- 强势板块（同花顺板块日线，本地只读） ----------
    @app.route('/api/boards/strong')
    def api_boards_strong():
        from full.vipdoc_reader import vipdoc_root as _vr
        ths = _vr().parent / 'ths'
        catalog_f = ths / 'catalog' / 'index_codes.json'
        if not catalog_f.is_file():
            return jsonify(dict(ok=False, error='THS_BOARD_DATA_MISSING:' + str(ths)))
        cat = json.loads(catalog_f.read_text(encoding='utf-8'))
        rows = []
        for f in sorted((ths / 'kline_daily').glob('*.json')):
            try:
                d = json.loads(f.read_text(encoding='utf-8'))
                kl = d['rows']
                if len(kl) < 21:
                    continue
                close5, close20 = kl[-1][4] / kl[-6][4] - 1, kl[-1][4] / kl[-21][4] - 1
                # 成分股数：catalog 反查
                members = sum(1 for v in cat.values()
                              if isinstance(v, dict) and str(v.get('index_code') or '') == d['code'])
                rows.append(dict(code=d['code'], ret_5d=round(close5, 4),
                                 ret_20d=round(close20, 4), members=members,
                                 last_day=kl[-1][0]))
            except Exception:
                continue
        rows.sort(key=lambda x: -x['ret_5d'])
        names = {}
        for v in cat.values():
            if isinstance(v, dict) and v.get('index_code'):
                names[str(v['index_code'])] = v.get('name')
        for r in rows:
            r['name'] = names.get(r['code'], r['code'])
        return jsonify(dict(ok=True, boards=rows[:30], total=len(rows),
                            note='同花顺概念板块日线（本地归档）；5日/20日涨幅为板块指数收益'))

    # ---------- 生产版归档只读引用（指数地图 / 全球市场） ----------
    @app.route('/api/prod/index_maps')
    def api_prod_index_maps():
        from full.prod_reference import index_maps
        return jsonify(index_maps())

    @app.route('/api/prod/global_quotes')
    def api_prod_global_quotes():
        from full.prod_reference import global_quotes
        return jsonify(global_quotes())

    # ---------- 观察名单（完整版本地 CRUD） ----------
    @app.route('/api/watch', methods=['GET', 'POST', 'DELETE'])
    def api_watch():
        import json as _j
        p = FE / 'data' / 'watch.json'
        if request.method == 'POST':
            body = request.get_json(force=True) or {}
            code = str(body.get('code') or '').strip()
            if not (code.isdigit() and len(code) == 6):
                return jsonify(dict(ok=False, error='BAD_CODE'))
            items = _j.loads(p.read_text(encoding='utf-8'))['items'] if p.is_file() else []
            entry = dict(code=code, name=str(body.get('name') or ''),
                         target_buy=body.get('target_buy'), stop_loss=body.get('stop_loss'),
                         strategy=body.get('strategy') or '', reason=body.get('reason') or '',
                         updated_at='%s' % __import__('datetime').datetime.now().isoformat(timespec='seconds'))
            items = [x for x in items if x['code'] != code] + [entry]
            p.write_text(_j.dumps(dict(items=items), ensure_ascii=False, indent=1), encoding='utf-8')
            return jsonify(dict(ok=True, saved=code))
        if request.method == 'DELETE':
            code = str(request.args.get('code') or '')
            items = _j.loads(p.read_text(encoding='utf-8'))['items'] if p.is_file() else []
            n = len(items)
            items = [x for x in items if x['code'] != code]
            p.write_text(_j.dumps(dict(items=items), ensure_ascii=False, indent=1), encoding='utf-8')
            return jsonify(dict(ok=len(items) < n, removed=len(items) < n))
        items = _j.loads(p.read_text(encoding='utf-8'))['items'] if p.is_file() else []
        # 附快照最新收盘
        import gzip as _gz
        out = []
        try:
            d = _j.loads(_gz.decompress((SNAPSHOT / 'daily.json.gz').read_bytes()))
            cidx, didx = d['columns'].index('close'), d['columns'].index('date')
            from full.vipdoc_reader import stock_names
            names = stock_names()
            for it in items:
                rr = d['codes'].get(it['code'])
                close = rr[-1][cidx] if rr else None
                day = rr[-1][didx] if rr else None
                prev = rr[-2][cidx] if rr and len(rr) > 1 else None
                out.append(dict(it, name=it.get('name') or names.get(it['code'], ''),
                                latest_close=close, latest_day=day,
                                change_pct=(round((close / prev - 1) * 100, 2) if (close and prev) else None)))
        except Exception:
            out = items
        return jsonify(dict(ok=True, items=out, note='现价为快照最新收盘（非实时）'))

    # ---------- 推送日志（助手页查看通知） ----------
    @app.route('/api/push/log')
    def api_push_log():
        p = FE / 'data' / 'push_log.jsonl'
        rows = []
        if p.is_file():
            for line in p.read_text(encoding='utf-8').splitlines()[-50:]:
                if line.strip():
                    rows.append(json.loads(line))
        return jsonify(dict(ok=True, notifications=rows[::-1]))

    # ---------- 强势板块池（监控板块强势股，字段对齐生产版） ----------
    @app.route('/api/strong_pool/state')
    def api_strong_pool_state():
        from full.strong_pool import load_or_compute
        return jsonify(load_or_compute())

    @app.route('/api/strong_pool/watch', methods=['POST'])
    def api_strong_pool_watch():
        from full.strong_pool import change_watch
        body = request.get_json(silent=True) or {}
        try:
            cfg = change_watch(body.get('action'),str(body.get('board') or '').strip(),strong=True)
            return jsonify(dict(ok=True,boards=cfg['boards']))
        except (ValueError, OSError, ImportError) as exc:
            return jsonify(dict(ok=False,error=str(exc)[:220])),400

    @app.route('/api/strong_pool/prepare', methods=['POST'])
    def api_strong_pool_prepare():
        from full.strong_pool import prepare
        if not _DATA_LOCK.acquire(blocking=False):
            return jsonify(dict(ok=False,error='行情快照正在更新或使用')), 409
        try:
            result = prepare()
        except (ValueError, OSError, RuntimeError, ImportError, SystemExit) as exc:
            result = dict(ok=False,error=str(exc)[:260])
        finally:
            _DATA_LOCK.release()
        _task_log('strong_pool_prepare', result)
        return jsonify(result), 200 if result['ok'] else 409

    @app.route('/api/strong_pool/board')
    def api_strong_pool_board():
        from full.strong_pool import load_or_compute
        name = str(request.args.get('board') or '')
        d = load_or_compute()
        for b in d.get('boards', []):
            if b['board'] == name:
                return jsonify(dict(ok=True, board=name, status=b['pool_status'],
                                    qualified_count=b['qualified_count'],
                                    rows=b['qualified'], note=b.get('note')))
        return jsonify(dict(ok=False, error='BOARD_NOT_FOUND:' + name))

    @app.route('/api/board_monitor', methods=['GET','POST'])
    def api_board_monitor():
        from full.board_monitor import state
        from full.strong_pool import change_watch
        try:
            if request.method=='POST':
                body = request.get_json(silent=True) or {}
                change_watch(body.get('action'),str(body.get('board') or '').strip())
            return jsonify(state())
        except (OSError,RuntimeError,ValueError,ImportError) as exc:
            return jsonify(dict(ok=False,error=str(exc)[:220])), 400

    # ---------- 助手（本地知识库，不调外部模型） ----------
    @app.route('/api/assistant/chat', methods=['POST'])
    def api_assistant_chat():
        from full.assistant_faq import answer
        body = request.get_json(force=True) or {}
        st = None
        try:
            man = json.loads((SNAPSHOT / 'snapshot_manifest.json').read_text(encoding='utf-8'))
            st = dict(data_through=None, p_day=man.get('p_day'), monitor_day=man.get('monitor_day'),
                      latest_run=None, model_switch=model_switch()[1], source=source_config()[1])
        except Exception:
            pass
        return jsonify(dict(ok=True, **answer(str(body.get('q') or ''), st)))

    return app


def main(argv=None):
    import argparse
    import socket
    _orig_fqdn = socket.getfqdn
    def _safe_fqdn(name=''):
        try:
            return _orig_fqdn(name)
        except Exception:
            return 'localhost'   # 中文主机名等解析失败时不影响起服务
    socket.getfqdn = _safe_fqdn
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=17891)
    a = ap.parse_args(argv)
    app = create_app()
    from werkzeug.serving import make_server
    server = make_server('127.0.0.1',a.port,app,threaded=True)
    from full.board_monitor import start
    start()
    print(f'完整版已启动 http://127.0.0.1:{a.port}',flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
