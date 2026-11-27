"""MCP tool wiring and DingTalk callback checks without an enterprise account."""
import asyncio
import os
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from full import dingtalk_bot as bot, mcp_server as mcp, mcp_tools as tools


class AgentIntegration(unittest.TestCase):
    def setUp(self):
        target = tools.ROOT/'_tmp'/'agent_tests'/uuid4().hex/'jobs.sqlite3'
        env = patch.dict(os.environ,{'STARGAZER_AGENT_LEDGER':str(target)})
        env.start()
        self.addCleanup(env.stop)

    def test_all_tool_wiring(self):
        for label,args in [('status',()),('plan',('ab'*8,)),('positions',()),('review',('ab'*8,)),
                           ('task_status',('ab'*16,))]:
            with self.subTest(label=label),patch.object(tools,label,return_value={'ok':True}) as call:
                self.assertTrue(getattr(mcp,label)(*args)['ok'])
                call.assert_called_once_with(*args)
        for name,action,args in [('select_postclose','select',()),('run_replay','replay',()),
                                 ('backtest','backtest',('ab'*8,)),('mine_factors','mine',()),
                                 ('import_data','import_data',()),('fill_gaps','fill_gaps',([20261125],))]:
            with self.subTest(name=name),patch.object(tools,'submit',return_value={'task_id':'fixture'}) as call:
                self.assertEqual(getattr(mcp,name)(*args)['task_id'],'fixture')
                self.assertEqual(call.call_args.args[0],action)

    def test_dedupe_actor_and_worker_exit(self):
        with patch.object(tools,'_bound_payload',return_value={'_agent_binding':{'snapshot':'x'}}), \
             patch.object(tools,'_spawn',return_value=type('Worker',(),{'pid':os.getpid()})()) as spawn:
            a = tools.submit('select',actor='one')
            b = tools.submit('select',actor='one')
        self.assertEqual(a['task_id'],b['task_id'])
        spawn.assert_called_once()
        self.assertEqual(tools.task_status(a['task_id'],actor='two')['error'],'TASK_NOT_FOUND')
        with patch('rules_lab.state_store._pid_alive',return_value=False):
            self.assertEqual(tools.task_status(a['task_id'])['status'],'INTERRUPTED')

    @staticmethod
    def message(command, msg, user='alice', group='group'):
        return dict(senderStaffId=user,conversationId=group,conversationType='2',msgId=msg,
                    text={'content':command})

    def test_dingtalk_whitelist_confirmation_and_duplicate_callbacks(self):
        router = bot.CommandRouter({'alice','bob'},{'group'})
        a = self.message('数据更新','m1')
        with patch.object(tools,'_bound_payload',return_value={'_agent_binding':{'snapshot':'frozen'}}), \
             patch.object(tools,'submit',return_value=dict(ok=True,action='import_data',task_id='aa'*16,status='ACCEPTED')) as submit:
            self.assertEqual(router.receive(self.message('选股','x',group='other')),(None,None,None))
            self.assertEqual(router.receive(self.message('选股','x',user='external')),(None,None,None))
            text,_,actor = router.receive(a)
            code = re.search(r'确认 ([a-f0-9]{8})',text)[1]
            self.assertEqual(router.receive(a),(None,None,None))
            self.assertIn('无效',router.receive(self.message('确认 '+code,'m2',user='bob'))[0])
            self.assertIsNone(router.receive(self.message('请帮我选股','m3'))[1])
            submit.assert_not_called()
            text,task,who = router.receive(self.message('确认 '+code,'m4'))
            self.assertEqual((task,who),('aa'*16,actor))
            self.assertIn('已受理',text)
            self.assertEqual(submit.call_args.kwargs['prepared']['_agent_binding']['snapshot'],'frozen')
            self.assertIn('已使用',router.receive(self.message('确认 '+code,'m5'))[0])
            self.assertIn('管理员',router.receive(self.message('推送测试','m6'))[0])
            submit.assert_called_once()

    def test_dingtalk_two_receipts_same_session(self):
        router = bot.CommandRouter({'alice'},{'group'})
        a = self.message('选股','m1')
        sent = []
        def reply(data,text):
            sent.append((data['conversationId'],text))
            return True
        task_id = 'ab'*16
        with patch.object(tools,'submit',return_value=dict(ok=True,action='select',task_id=task_id,status='ACCEPTED')), \
             patch.object(tools,'task_status',return_value=dict(ok=True,action='select',task_id=task_id,
                                       status='SUCCEEDED',result={'count':3})):
            asyncio.run(bot.handle_callback(router,a,reply,poll=0))
            asyncio.run(bot.handle_callback(router,a,reply,poll=0))
        self.assertEqual(len(sent),2)
        self.assertEqual([group for group,_ in sent],['group','group'])
        self.assertIn('已受理',sent[0][1])
        self.assertIn('冻结 3',sent[1][1])

    def test_official_sdk_handler_registration_and_ack(self):
        import dingtalk_stream as sdk
        settings = {'STARGAZER_DINGTALK_APP_KEY':'fixture-key','STARGAZER_DINGTALK_APP_SECRET':'fixture-secret',
                    'STARGAZER_DINGTALK_USERS':'alice','STARGAZER_DINGTALK_GROUPS':'group'}
        with patch.dict(os.environ,settings),patch.object(sdk,'DingTalkStreamClient') as client:
            bot.main()
            instance = client.return_value
            instance.start_forever.assert_called_once()
            topic,handler = instance.register_callback_handler.call_args.args
        self.assertEqual(topic,sdk.ChatbotMessage.TOPIC)
        fixture = type('Callback',(),{'data':self.message('帮助','m1')})()

        async def drive():
            result = await handler.process(fixture)
            await asyncio.sleep(0)
            return result

        with patch.object(bot,'handle_callback') as dispatch:
            self.assertEqual(asyncio.run(drive()),(sdk.AckMessage.STATUS_OK,'OK'))
            dispatch.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
