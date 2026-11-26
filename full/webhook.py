"""可选 Webhook 通知渠道（默认内置收件箱不变）。
设置 env STARGAZER_WEBHOOK_URL 或 data/webhook_url.txt 时，回执在落账本后转发
（失败仅记录，不影响本地回执；适合用户已有钉钉机器人/自有 Webhook）。"""
from __future__ import annotations
import json
import os
import urllib.request
from pathlib import Path

FE = Path(__file__).resolve().parents[1]


def webhook_url():
    env = os.environ.get('STARGAZER_WEBHOOK_URL')
    if env:
        return env
    f = FE / 'data' / 'webhook_url.txt'
    return f.read_text(encoding='utf-8').strip() if f.is_file() else None


def notify(receipt: dict, url=None, timeout=6):
    url = url or webhook_url()
    if not url:
        return dict(sent=False, reason='NO_WEBHOOK_CONFIGURED')
    body = json.dumps(receipt, ensure_ascii=False).encode('utf-8')
    req = urllib.request.Request(url, data=body,
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return dict(sent=200 <= r.status < 300, status=r.status)
    except Exception as exc:
        return dict(sent=False, reason='%s:%s' % (type(exc).__name__, str(exc)[:100]))
