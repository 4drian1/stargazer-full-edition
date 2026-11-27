"""Regression for local board monitoring and the independent strong-pool entry."""
from datetime import datetime
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from full import board_monitor, strong_pool
from full.webapp import create_app
from rules_lab.paths import TMP


class BoardReadiness(unittest.TestCase):
    def setUp(self):
        store = TMP / 'board_tests' / uuid4().hex
        self.addCleanup(patch.stopall)
        patch.object(strong_pool, 'STORE', store).start()
        patch.object(strong_pool, 'PROD_DATA', store / 'production-fixture').start()
        patch.object(strong_pool, 'PROD_ROOT', store / 'production-fixture/postclose_risk/strong_sector').start()
        self.channel = patch.object(board_monitor, 'push_config', return_value=dict(channel='none')).start()
        self.now = datetime(2026, 11, 26, 10, tzinfo=strong_pool.ZONE)

    def test_fresh_current_quote_and_repeat_save_only_one_touch(self):
        client = create_app().test_client()
        board = '人工智能'
        membership = dict(identity=dict(name=board, index_code='885728'), members=['000001','000002'],
                          provenance=dict(kind='FETCHED',day='20261125'))
        frozen = dict(observation_date=20261126, rows=[dict(code='000001', name='样本', boards=[board],
                      reference_close=10, positions=[dict(lower=9.55,upper=9.70),
                      dict(lower=9.58,upper=9.64,primary=True,source_waves=3),
                      dict(lower=9.2,upper=9.4)])])
        quotes = {'000001':dict(time='20261126095950', price=9.60, prev=10),
                  '000002':dict(time='20261126095500', price=8.90, prev=9)}
        with patch.object(strong_pool, 'identity', return_value=membership), \
             patch.object(strong_pool, 'current_pool', return_value=(frozen,'TEST')), \
             patch('full.vipdoc_reader.calendar_days', return_value=[20261125,20261126,20261127]), \
             patch.object(board_monitor,'send_summary',return_value=dict(ok=True)) as send:
            self.assertEqual(client.post('/api/board_monitor', json=dict(action='add',board=board)).status_code,200)
            self.assertEqual(strong_pool.strong_watch(),[])
            self.assertEqual(client.post('/api/strong_pool/watch', json=dict(action='add',board=board)).status_code,200)
            self.assertEqual(strong_pool.strong_watch(),[board])
            self.channel.return_value = dict(channel='dingtalk',dingtalk_webhook='fixture-signing-endpoint')
            self.assertTrue(client.post('/api/board_monitor',json=dict(action='enable')).json['enabled'])
            for _ in range(2):
                result = board_monitor.tick(self.now, lambda _:quotes)
                self.assertEqual(result['status'],'LIVE')
                self.assertEqual(result['rows'][0]['constituents']['covered'],1)
                self.assertEqual(result['rows'][0]['constituents']['total'],2)
                self.assertEqual(len(result['alerts']),1)
            self.assertEqual(result['alerts'][0]['delivery'],'PENDING')
            self.assertTrue(board_monitor.deliver_once(self.now))
            self.assertFalse(board_monitor.deliver_once(self.now))
            send.assert_called_once()
            title,body=send.call_args.args
            self.assertIn('异动·P1',title)
            self.assertIn('下一黄金位9.20—9.40',body)
            self.assertNotIn('RSI',body)
            later = dict(quotes,**{'000001':dict(time='20261126100000', price=9.3,prev=10)})
            self.assertEqual(len(board_monitor.tick(self.now,lambda _:later)['alerts']),1)
            pending = strong_pool._read(board_monitor._file(self.now))
            next(iter(pending['alerts'].values()))['delivery'] = 'SENDING'
            board_monitor.atomic_write_json(board_monitor._file(self.now),pending)
            board_monitor._recover(self.now)
            result = board_monitor.state(self.now)
            self.assertEqual(result['alerts'][0]['delivery'],'UNKNOWN')
            send.assert_called_once()
            frozen['observation_date'] = 20261125
            result = board_monitor.tick(self.now, lambda _:quotes)
            self.assertEqual(result['pool_state'],'WAIT_SAME_DAY_POOL')
            self.assertEqual(len(result['alerts']),1)
            self.assertTrue(client.post('/api/board_monitor',json=dict(action='remove',board=board)).json['ok'])
            self.assertEqual(strong_pool.strong_watch(),[board])
            client.post('/api/board_monitor', json=dict(action='disable'))
            provider = Mock()
            self.assertEqual(board_monitor.tick(self.now,provider)['status'],'OFF')
            provider.assert_not_called()

    def test_indicators_in_first_or_a_single_supplement(self):
        from full.strong_touch import make_event
        self.channel.return_value = dict(channel='dingtalk',dingtalk_webhook='fixture')
        quote = dict(time='20261126095950', price=9.6, prev=10)
        facts = dict(periods={'5':{'status':'KNOWN','provisional':True},'15':{'status':'KNOWN','provisional':True}},
                     volume_ratio=.9)
        for immediate in (False,True):
            code = '000002' if immediate else '000001'
            row = dict(code=code,name='样本',reference_close=10,positions=[dict(lower=9.5,upper=9.7)])
            event = make_event(row,quote,self.now,['样本概念'],'TEST')
            board_monitor.atomic_write_json(board_monitor._file(self.now),{'alerts':{code:event}})
            with patch.object(strong_pool,'watch_config',return_value=dict(enabled=True,boards=[])), \
                 patch('full.strong_touch.compute_indicators',return_value=facts), \
                 patch.object(board_monitor,'send_summary',return_value={'ok':True}) as send:
                if immediate:
                    self.assertTrue(board_monitor.indicator_once(self.now))
                self.assertTrue(board_monitor.deliver_once(self.now))
                if not immediate:
                    self.assertTrue(board_monitor.indicator_once(self.now))
                    self.assertTrue(board_monitor.deliver_once(self.now))
                    self.assertIn('量能比率: 0.90',send.call_args.args[1])
                else:
                    self.assertIn('跨日底背离',send.call_args.args[1])
                self.assertFalse(board_monitor.deliver_once(self.now))
                self.assertEqual(send.call_count,1 if immediate else 2)

    def test_live_aggregate_has_no_future_minutes(self):
        from full.strong_touch import _live_five
        cut = 20261126093718
        native = [dict(date=20261126093000+i*100,open=10,high=10,low=10,close=10,volume=1)
                  for i in range(1,8)]
        first,forming = _live_five(native,{'price':9.6},20261126,cut)
        self.assertEqual(first['volume'],5)
        self.assertTrue(first['complete'])
        self.assertFalse(forming['complete'])
        self.assertEqual(forming['t'],93718)
        self.assertEqual(forming['low'],9.6)

    def test_failed_prepare_preserves_old_pool(self):
        file = strong_pool.STORE / 'strong_pool.json'
        file.parent.mkdir(parents=True)
        file.write_bytes(b'{"previous":true}')
        with patch.object(strong_pool,'strong_watch',return_value=['测试']), \
             patch.object(strong_pool,'_day_pair',return_value=(20261125,20261126)), \
             patch('full.vipdoc_reader.calendar_days',return_value=[20261125,20261126]), \
             patch('full.cli._verify_snapshot',side_effect=SystemExit('SNAPSHOT_DATA_MISMATCH')):
            response = create_app().test_client().post('/api/strong_pool/prepare')
        self.assertEqual(response.status_code,409)
        self.assertIn('SNAPSHOT_DATA_MISMATCH',response.json['error'])
        self.assertEqual(file.read_bytes(),b'{"previous":true}')

    def test_after_close_marks_previous_observation_historical(self):
        close = datetime(2026,11,26,16,tzinfo=strong_pool.ZONE)
        dates = [20261125,20261126,20261127]
        with patch.object(strong_pool,'strong_watch',return_value=[]), \
             patch('full.vipdoc_reader.calendar_days',return_value=dates), \
             patch.object(strong_pool,'current_pool',return_value=(dict(observation_date=20261126,rows=[]),'TEST')):
            s = strong_pool.load_or_compute(close)
        self.assertEqual((s['status'],s['observation_day'],s['expected_observation_day']),('STALE',20261126,20261127))

    def test_history_button_does_not_send_a_webhook(self):
        api = create_app().test_client()
        with patch('full.cli.main',return_value=0) as cli, \
             patch('full.push_config.send_summary') as notify:
            response = api.post('/api/run',json=dict(rule='lvp_golden',days=[20261118],with_tarot_shadow=False))
        self.assertEqual(response.json['mode'],'REPLAY')
        self.assertNotIn('--with-tarot-shadow',cli.call_args.args[0])
        notify.assert_not_called()

    def test_dingtalk_signing_and_acknowledgment(self):
        from full.push_config import send_dingtalk
        from urllib.parse import parse_qs, urlsplit
        reply = Mock()
        reply.__enter__ = Mock(return_value=reply)
        reply.__exit__ = Mock(return_value=False)
        reply.read.return_value = b'{"errcode":0}'
        config = dict(dingtalk_webhook='https://oapi.dingtalk.com/robot/send?access_token=fixture',
                      dingtalk_secret='fixture-secret')
        with patch('urllib.request.urlopen',return_value=reply) as transport:
            self.assertTrue(send_dingtalk('触位','测试',config,include_heading=False)['ok'])
        request = transport.call_args.args[0]
        import json
        self.assertEqual(json.loads(request.data)['markdown']['text'],'测试')
        query = parse_qs(urlsplit(request.full_url).query)
        self.assertIn('timestamp',query)
        self.assertIn('sign',query)
        self.assertNotIn(b'fixture-secret',request.data)


if __name__ == '__main__':
    unittest.main()
