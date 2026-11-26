"""推送渠道配置：钉钉机器人 / Pushplus 二选一，保存才生效。

配置文件 data/push_config.json：
  {"channel": "dingtalk", "dingtalk_webhook": "https://oapi.dingtalk.com/robot/send?access_token=...",
   "dingtalk_secret": "SEC...", "pushplus_token": "..."}
channel 为空/none = 不推送（仅本地收件箱）。钉钉加签口径与生产版 notifier 一致：
timestamp + "\\n" + secret 做 HMAC-SHA256，base64 后 URL 编码。
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
CFG = FE / 'data' / 'push_config.json'

DEFAULT = dict(channel='none', dingtalk_webhook='', dingtalk_secret='', pushplus_token='')


def load():
    if CFG.is_file():
        try:
            d = json.loads(CFG.read_text(encoding='utf-8'))
            return {**DEFAULT, **{k: v for k, v in d.items() if k in DEFAULT}}
        except Exception:
            pass
    return dict(DEFAULT)


def save(doc):
    CFG.parent.mkdir(parents=True, exist_ok=True)
    merged = {**DEFAULT, **{k: str(doc.get(k) or '').strip() for k in DEFAULT}}
    CFG.write_text(json.dumps(merged, ensure_ascii=False, indent=1), encoding='utf-8')
    return merged


def public_view():
    """给界面：令牌只回显后 4 位，不回传完整密钥。"""
    c = load()
    def mask(v):
        return ('…' + v[-4:]) if v else ''
    return dict(channel=c['channel'],
                dingtalk_webhook_set=bool(c['dingtalk_webhook']),
                dingtalk_webhook_tail=mask(c['dingtalk_webhook']),
                dingtalk_secret_set=bool(c['dingtalk_secret']),
                pushplus_token_set=bool(c['pushplus_token']),
                pushplus_token_tail=mask(c['pushplus_token']))


def send_dingtalk(title, text, cfg=None, timeout=10):
    cfg = cfg or load()
    url = cfg['dingtalk_webhook']
    if not url:
        return dict(ok=False, reason='DINGTALK_WEBHOOK_NOT_SET')
    if cfg['dingtalk_secret']:
        ts = str(round(time.time() * 1000))
        sign = urllib.parse.quote_plus(base64.b64encode(
            hmac.new(cfg['dingtalk_secret'].encode('utf-8'),
                     (ts + '\n' + cfg['dingtalk_secret']).encode('utf-8'),
                     hashlib.sha256).digest()).decode('utf-8'))
        url += ('&' if '?' in url else '?') + 'timestamp=' + ts + '&sign=' + sign
    # 钉钉 markdown 折叠单个换行：段落用空行分隔
    body = json.dumps({'msgtype': 'markdown',
                       'markdown': {'title': title,
                                    'text': '### ' + title + '\n\n' + text.replace('\n', '\n\n')}},
                      ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body, headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            resp = json.loads(r.read().decode('utf-8', 'replace'))
        ok = resp.get('errcode') == 0
        return dict(ok=ok, resp=resp if not ok else dict(errcode=0))
    except Exception as exc:
        return dict(ok=False, reason='%s:%s' % (type(exc).__name__, str(exc)[:120]))


def send_pushplus(title, content, cfg=None):
    cfg = cfg or load()
    from full.pushplus import send
    return send(title, content, tk=cfg['pushplus_token'] or None)


def send_summary(title, content):
    """按配置的渠道发运行汇总；channel=none 返回未配置（不影响本地回执）。每次发送落盘日志。"""
    c = load()
    import datetime
    log_entry = None
    if c['channel'] == 'dingtalk':
        r = send_dingtalk(title, content, c)
        log_entry = dict(channel='dingtalk', ok=bool(r.get('ok')),
                         error=None if r.get('ok') else str(r.get('reason', r.get('resp')))[:200])
    elif c['channel'] == 'pushplus':
        r = send_pushplus(title, content, c)
        log_entry = dict(channel='pushplus', ok=bool(r.get('ok')),
                         error=None if r.get('ok') else str(r.get('reason', r.get('resp')))[:200])
    else:
        return dict(channel='none', ok=False, reason='CHANNEL_NONE（仅本地收件箱）')
    log_entry.update(title=title[:80], sent_at=datetime.datetime.now().isoformat(timespec='seconds'),
                     content_head=content.split('\n')[1][:60] if '\n' in content else '')
    try:
        with (FE / 'data' / 'push_log.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(log_entry, ensure_ascii=False) + '\n')
    except Exception:
        pass
    return dict(channel=log_entry['channel'], **r)
