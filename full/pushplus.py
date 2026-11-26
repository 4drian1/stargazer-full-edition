"""Pushplus(PlusPush) 微信推送渠道：触位回执的运行级汇总推送。
配置：data/pushplus_token.txt 或环境变量 STARGAZER_PUSHPLUS_TOKEN。
"""
from __future__ import annotations
import json
import os
import urllib.request
from pathlib import Path

FE = Path(__file__).resolve().parents[1]
URL = "https://www.pushplus.plus/send"


def token():
    env = os.environ.get("STARGAZER_PUSHPLUS_TOKEN")
    if env:
        return env.strip()
    f = FE / "data" / "pushplus_token.txt"
    return f.read_text(encoding="utf-8").strip() if f.is_file() else None


def send(title, content, tk=None):
    tk = tk or token()
    if not tk:
        return dict(ok=False, reason="NO_TOKEN")
    body = json.dumps({"token": tk, "title": title, "content": content,
                       "template": "txt"}).encode("utf-8")
    req = urllib.request.Request(URL, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
        return dict(ok=resp.get("code") == 200, resp=resp)
    except Exception as exc:
        return dict(ok=False, reason="%s:%s" % (type(exc).__name__, str(exc)[:100]))


def build_summary(run_dir):
    """把一次运行的触位回执汇总成 (title, text)。无事件返回 (None, None)。"""
    run_dir = Path(run_dir)
    ev_f = sorted((run_dir / "events").glob("*.json"))
    if not ev_f:
        return None, None
    events = json.loads(ev_f[-1].read_text(encoding="utf-8"))["events"]
    day = ev_f[-1].stem
    touches = [e for e in events.values() if e.get("bar")]
    codes = sorted({e["code"] for e in touches})
    title = "Stargazer 触位提醒 %s：%d 只" % (day, len(codes))
    lines = ["触发 %d 次信号，涉及 %d 只：" % (len(touches), len(codes))]
    for e in sorted(touches, key=lambda x: x.get("bar", {}).get("ts", ""))[:15]:
        fv = e.get("factor_values") or {}
        lines.append("- %s  %s  收 %.2f  跌 %.1f%%" % (
            e["code"], str(e.get("bar", {}).get("ts", ""))[8:12],
            e.get("bar_close") or 0,
            (fv.get("decline_from_prev_close") or 0) * 100))
    if len(touches) > 15:
        lines.append("…另 %d 条见本地收件箱" % (len(touches) - 15))
    return title, chr(10).join(lines)


def push_run_summary(run_dir):
    title, text = build_summary(run_dir)
    if title is None:
        return dict(ok=False, reason="NO_EVENTS")
    return send(title, text)
