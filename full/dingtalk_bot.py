"""Exact DingTalk commands on the same job ledger as MCP."""
import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from full import mcp_tools as tools
from rules_lab.paths import ROOT,canonical_sha

HELP = ('状态 / 选股 / 计划 / 持仓 / 复盘 / 回测 [运行ID] / 回放 YYYYMMDD / 挖掘 / '
        '数据更新 / 数据补缺 YYYYMMDD / 推送测试 / 任务 [任务ID] / 帮助')
CONFIRM = {'数据更新':('import_data',{'source':'configured_tdx','include_minutes':False}),
           '推送测试':('push_test',{})}


def summarize_task(task):
    if not task.get('ok'):
        return f"任务不可用（{task.get('error','ERROR')}）"
    result = task.get('result') or {}
    state = task['status']
    label = {'ACCEPTED':'已受理','RUNNING':'运行中','SUCCEEDED':'已完成',
             'FAILED':'失败','INTERRUPTED':'中断'}[state]
    fields = [f'{label} · {task["task_id"]}']
    for key, name in (('run_id','运行'),('count','冻结'),('day','观察日'),('imported','导入')):
        if result.get(key) is not None:
            fields.append(f'{name} {result[key]}')
    if task['action']=='backtest' and result.get('summary'):
        summary = result['summary']
        fields.append(f'模拟成交 {summary.get("filled",0)}/{summary.get("signals",0)}')
    if state in ('FAILED','INTERRUPTED') and result.get('error'):
        fields.append(str(result['error'])[:150])
    return ' ｜ '.join(fields)


class CommandRouter:
    def __init__(self, users, groups, *, allow_push_test=False):
        self.users, self.groups = set(users), set(groups)
        self.allow_push_test = allow_push_test

    def receive(self, data):
        if not isinstance(data,dict):
            return None,None,None
        user, group = data.get('senderStaffId'),data.get('conversationId')
        msg, text = data.get('msgId'),data.get('text')
        if (data.get('conversationType')!='2' and data.get('conversationType')!=2) or not all(
                isinstance(value,str) and value for value in (user,group,msg)):
            return None,None,None
        if user not in self.users or group not in self.groups or not isinstance(text,dict):
            return None,None,None
        command = text.get('content')
        if not isinstance(command,str) or len(command)>300:
            return None,None,None
        actor = canonical_sha(['dingtalk',group,user])
        request = canonical_sha([actor,msg])
        with tools.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            fresh = db.execute('INSERT OR IGNORE INTO inbox(id,actor) VALUES(?,?)',(request,actor)).rowcount
        if not fresh:
            return None,None,None
        reply, task_id = self.dispatch(command.strip(),actor,request)
        return reply,task_id,actor

    def dispatch(self, command, actor, request):
        if command=='帮助':
            return HELP,None
        if command=='状态':
            s = tools.status()
            return f"日线 {s['daily_through']} ｜ 观察日 {s['observation_day']} ｜ 回放日 {s['replay_day']}",None
        if command in ('计划','持仓','复盘'):
            call = {'计划':tools.plan,'持仓':tools.positions,'复盘':tools.review}[command]
            result = call()
            if not result.get('ok'):
                return f"{command}暂无结果（{result.get('error','ERROR')}）",None
            if command=='计划':
                return f"冻结计划 {result['run_id']} ｜ 观察日 {result['day']} ｜ {result['count']} 只",None
            if command=='持仓':
                return f"模拟持仓 {len(result['positions'])} 条 ｜ 回放 {result['run_id']}",None
            return f"复盘 {result['run_id']} ｜ 样本 {result['total']} 条",None
        if match := re.fullmatch(r'任务 ([a-f0-9]{32})',command):
            return summarize_task(tools.task_status(match[1],actor=actor)),None
        if match := re.fullmatch(r'确认 ([a-f0-9]{8})',command):
            return self.confirm(match[1],actor,request)
        if command=='推送测试' and not self.allow_push_test:
            return '现有 Webhook 推送测试需要管理员为此调用层启用。',None
        gated = CONFIRM.get(command)
        if match := re.fullmatch(r'数据补缺 (20\d{6})',command):
            gated = ('fill_gaps',{'days':[int(match[1])]})
        if gated:
            # Capture the input before the user approves it.
            try:
                prepared = tools._bound_payload(*gated)
            except (OSError,ValueError,KeyError,RuntimeError) as exc:
                return f'无法准备 {type(exc).__name__}',None
            token = uuid4().hex[:8]
            action, _ = gated
            with tools.ledger() as db:
                db.execute('INSERT INTO inbox(id,actor,command,expires) VALUES(?,?,?,?)',
                           ('confirm:'+token,actor,json.dumps([action,prepared]),time.time()+300))
            bound = prepared.get('_agent_binding')
            stamp = f" ｜ 快照 {bound['snapshot'][:12]}" if bound else ''
            return f'{command}{stamp}。请在 5 分钟内发送「确认 {token}」。',None
        if command=='选股':
            return self.enqueue('select',{},actor,request)
        if command=='挖掘':
            return self.enqueue('mine',{},actor,request)
        if match := re.fullmatch(r'回测(?: ([a-f0-9]{16}))?',command):
            return self.enqueue('backtest',{'run_id':match[1]},actor,request)
        if match := re.fullmatch(r'回放 (20\d{6})',command):
            return self.enqueue('replay',{'days':[int(match[1])]},actor,request)
        return '请输入精确命令。'+HELP,None

    def confirm(self, token, actor, request):
        with tools.ledger() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM inbox WHERE id=? AND actor=? AND expires>? AND consumed=0',
                             ('confirm:'+token,actor,time.time())).fetchone()
            if not row:
                return '确认码无效或已使用。',None
            db.execute('UPDATE inbox SET consumed=1 WHERE id=?',(row['id'],))
        action, body = json.loads(row['command'])
        if action=='push_test' and not self.allow_push_test:
            return '现有 Webhook 推送测试需要管理员为此调用层启用。',None
        return self.enqueue(action,{},actor,request,prepared=body)

    @staticmethod
    def enqueue(action, args, actor, request, *, prepared=None):
        # Request-level dedup applies even if DingTalk retransmits a failed command.
        task = tools.submit(action,args,actor=actor,request_id=request,prepared=prepared)
        return summarize_task(task),task.get('task_id')


def _session_reply(data, text):
    import requests
    url = data.get('sessionWebhook','')
    try:
        expiry = data.get('sessionWebhookExpiredTime')
        parsed = urlsplit(url)
        if (parsed.scheme!='https' or parsed.hostname not in ('oapi.dingtalk.com','api.dingtalk.com')
                or parsed.username or (expiry and time.time()*1000>=int(expiry))):
            return False
        reply = requests.post(url,json={'msgtype':'text','text':{'content':text[:1000]}},
                              timeout=8,allow_redirects=False)
        return reply.status_code==200 and reply.json().get('errcode')==0
    except (requests.RequestException,ValueError,TypeError,AttributeError):
        return False


async def handle_callback(router, data, reply=_session_reply, *, poll=2):
    text, task_id, actor = await asyncio.to_thread(router.receive,data)
    if text is None or not await asyncio.to_thread(reply,data,text) or not task_id:
        return
    deadline = time.monotonic()+1800
    while time.monotonic()<deadline:
        await asyncio.sleep(poll)
        task = await asyncio.to_thread(tools.task_status,task_id,actor=actor)
        if task.get('status') in tools.TERMINAL:
            await asyncio.to_thread(reply,data,summarize_task(task))
            return


def main():
    import dingtalk_stream as sdk
    from dotenv import load_dotenv
    load_dotenv(ROOT/'data'/'dingtalk_stream.env',override=False)
    key,secret = os.getenv('STARGAZER_DINGTALK_APP_KEY'),os.getenv('STARGAZER_DINGTALK_APP_SECRET')
    users = {x.strip() for x in os.getenv('STARGAZER_DINGTALK_USERS','').split(',') if x.strip()}
    groups = {x.strip() for x in os.getenv('STARGAZER_DINGTALK_GROUPS','').split(',') if x.strip()}
    if not all((key,secret,users,groups)):
        raise SystemExit('请配置企业机器人 AppKey/AppSecret、成员及群白名单。')
    router = CommandRouter(users,groups,allow_push_test=os.getenv('STARGAZER_DINGTALK_ALLOW_PUSH_TEST')=='1')
    pending = set()

    async def dispatch(data):
        try:
            await handle_callback(router,data)
        except Exception as exc:
            logging.error('DingTalk callback failed: %s',type(exc).__name__)

    class Handler(sdk.ChatbotHandler):
        async def process(self, message):
            if len(pending)>=32:
                return sdk.AckMessage.STATUS_SYSTEM_EXCEPTION,'busy'
            task = asyncio.create_task(dispatch(message.data))
            pending.add(task)
            task.add_done_callback(pending.discard)
            return sdk.AckMessage.STATUS_OK,'OK'

    client = sdk.DingTalkStreamClient(sdk.Credential(key,secret))
    client.register_callback_handler(sdk.ChatbotMessage.TOPIC,Handler())
    client.start_forever()


if __name__ == '__main__':
    main()
