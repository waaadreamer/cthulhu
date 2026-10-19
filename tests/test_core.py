import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cthulhu_v4.center import Center
from cthulhu_v4.util import process_identity


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.c = Center(self.tmp.name, clock=lambda: self.now)

    def tearDown(self):
        self.c.store.close(); self.tmp.cleanup()

    def create(self):
        return self.c.tool('master', 'create_task', dict(title='测试', description='只读验证', acceptance='给出证据',
            deadline=datetime.fromtimestamp(99999, timezone.utc).isoformat(), cwd=self.tmp.name))

    def clear_messages(self):
        for m in self.c.store.all('message'):
            m['state'] = 'handled'; self.c.store.put('message', m)

    def question(self, t):
        return self.c.tool(t['agent'], 'ask_master', dict(text='需要选择', recommendation='选择 A', blocking=True))

    def test_timeout_and_late_owner_answer_reject_stale_master(self):
        t = self.create(); q = self.question(t)
        q = self.c.tool('master', 'ask_owner', dict(question_id=q['id'], version=1, recommendation='选择 A'))
        self.now += 300; self.c.tick()
        timed = self.c.store.get('question', q['id'])
        self.assertEqual(timed['state'], 'master')
        self.assertEqual(timed['version'], 3)
        with self.c.store.atomic():
            self.c.answer('owner', dict(question_id=q['id'], version=3, answer='选择 B'))
        with self.assertRaises(ValueError):
            self.c.tool('master', 'answer_question', dict(question_id=q['id'], version=3, answer='选择 A'))
        self.assertEqual(self.c.store.get('question', q['id'])['history'][-1]['text'], '选择 B')
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'running')

    def test_owner_delegates_question_and_master_decides_mode(self):
        t = self.create(); q = self.question(t)
        q = self.c.tool('master', 'ask_owner', dict(question_id=q['id'], version=1, recommendation='A'))
        with self.c.store.atomic(): q = self.c.delegate({'question_id': q['id']})
        self.assertEqual((q['state'], q['version'], q['deadline']), ('master', 3, None))
        with self.assertRaises(ValueError):  # 已不在等主人
            with self.c.store.atomic(): self.c.delegate({'question_id': q['id']})
        # 全权模式：Master 不能再转问；开启时等待主人的问题自动交回。
        t2 = self.create(); q2 = self.question(t2)
        self.c.tool('master', 'ask_owner', dict(question_id=q2['id'], version=1, recommendation='B'))
        before = len(self.c.store.all('message'))
        with self.c.store.atomic(): result = self.c.set_master_decides(True)
        self.assertEqual(result['delegated'], 1)
        # 开关本身不额外唤醒 Master（只有交回的那个问题会通知）。
        self.assertEqual(len(self.c.store.all('message')) - before, 1)
        with self.c.store.atomic(): self.c.set_master_decides(False)
        self.assertEqual(len(self.c.store.all('message')) - before, 1)
        with self.c.store.atomic(): self.c.set_master_decides(True)
        self.assertEqual(self.c.store.get('question', q2['id'])['state'], 'master')
        with self.assertRaisesRegex(ValueError, '全权'):
            self.c.tool('master', 'ask_owner', dict(question_id=q['id'], version=3, recommendation='A'))
        self.assertTrue(self.c.snapshot()['prefs']['master_decides'])
        self.c.tool('master', 'answer_question', dict(question_id=q['id'], version=3, answer='A'))
        # 主人仍可事后修正。
        with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=4, answer='B'))
        self.assertEqual([h.get('text') for h in self.c.store.get('question', q['id'])['history'] if h.get('text')][-2:], ['A', 'B'])

    def test_archive_only_finished_and_hidden_from_master(self):
        t = self.create()
        with self.assertRaisesRegex(ValueError, '已完成或已取消'):
            with self.c.store.atomic(): self.c.archive_task({'task_id': t['id']})
        self.c.tool('master', 'control_task', dict(task_id=t['id'], action='cancel', reason='不需要了'))
        with self.c.store.atomic(): self.c.archive_task({'task_id': t['id']})
        listed = self.c.tool('master', 'list_tasks', {})
        self.assertNotIn(t['id'], [x['id'] for x in listed['tasks']])
        self.assertNotIn(t['agent'], [a['id'] for a in listed['agents']])
        self.assertTrue(self.c.snapshot()['tasks'][0]['archived'])  # 界面仍能看到
        with self.c.store.atomic(): self.c.archive_task({'task_id': t['id'], 'archived': False})
        self.assertIn(t['id'], [x['id'] for x in self.c.tool('master', 'list_tasks', {})['tasks']])

    def test_read_task_events_is_bounded(self):
        t = self.create()
        for i in range(200):
            self.c.store.emit('native:item/agentMessage/delta', actor=t['agent'], data={'itemId': 'x', 'delta': 'y' * 50})
            self.c.store.emit('native:thread/tokenUsage/updated', actor=t['agent'], data={'pad': 'z' * 5000})
        self.c.store.emit('native:item/completed', actor=t['agent'],
                          data={'item': {'id': 'c', 'type': 'commandExecution', 'aggregatedOutput': 'o' * 100000}})
        r = self.c.tool('master', 'read_task_events', {'task_id': t['id']})
        kinds = {e['kind'] for e in r['events']}
        self.assertNotIn('native:thread/tokenUsage/updated', kinds)
        self.assertNotIn('native:item/agentMessage/delta', kinds)
        self.assertLess(len(json.dumps(r, ensure_ascii=False)), 30000)
        out = next(e for e in r['events'] if e['kind'] == 'native:item/completed')['data']['item']['aggregatedOutput']
        self.assertIn('截断，共 100000 字符', out)

    def test_owner_late_correction_is_preserved(self):
        t = self.create(); q = self.question(t)
        self.c.tool('master', 'answer_question', dict(question_id=q['id'], version=1, answer='A'))
        with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=2, answer='B'))
        q = self.c.store.get('question', q['id'])
        self.assertEqual([h['text'] for h in q['history']], ['A', 'B'])
        self.assertEqual(q['version'], 3)

    def test_paused_waiting_and_live_turn_never_recovered(self):
        t = self.create(); self.clear_messages()
        lease = {'id': 'turn_x', 'node': 'local', 'agent': t['agent'], 'message': 'm', 'state': 'running', 'until': self.now + 9999}
        self.c.store.put('lease', lease)
        self.now += 1000; self.c.tick()
        self.assertFalse(any(m.get('key', '').startswith('recover:') for m in self.c.store.all('message') if m.get('key')))
        lease['state'] = 'done'; self.c.store.put('lease', lease)
        self.c.tool(t['agent'], 'wait_until', dict(reason='作业运行中', operation='job-1', seconds=500))
        self.now += 60; self.c.tick()
        self.assertEqual(self.c.store.get('task', t['id'])['recoveries'], 0)
        self.c.tool('master', 'control_task', dict(task_id=t['id'], action='pause', reason='用户暂停'))
        self.now += 10000; self.c.tick()
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')

    def test_recovery_is_bounded_and_preserves_session(self):
        t = self.create(); self.clear_messages()
        self.c.save_session(t['agent'], 'native-original')
        for _ in range(4):
            self.now += 1000; self.c.tick(); self.clear_messages()
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        self.assertEqual(self.c.store.get('agent', t['agent'])['session'], 'native-original')
        self.assertEqual(self.c.store.get('task', t['id'])['recoveries'], 3)

    def test_question_is_immediately_visible_without_rate_limit(self):
        t = self.create()
        self.c.tool(t['agent'], 'send_message', dict(to='master', text='普通进展'))
        q = self.question(t)
        self.assertEqual(q['state'], 'master')
        self.assertTrue(any(e['kind'] == 'question' for e in self.c.store.events(visible=True)))

    def test_cancelled_task_cannot_be_resurrected_by_answer(self):
        t = self.create(); q = self.question(t)
        self.c.tool('master', 'control_task', dict(task_id=t['id'], action='cancel', reason='取消'))
        with self.assertRaises(ValueError):
            with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=2, answer='继续'))
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'cancelled')

    def test_failed_tool_transaction_leaves_no_orphan_question(self):
        t = self.create(); self.question(t)
        with self.assertRaises(ValueError): self.question(t)
        self.assertEqual(len(self.c.store.all('question')), 1)
        self.assertEqual(len([e for e in self.c.store.events() if e['kind'] == 'question']), 1)

    def test_read_events_includes_incoming_outgoing_and_control(self):
        a, b = self.create(), self.create()
        m = self.c.tool(a['agent'], 'send_message', dict(to=b['agent'], text='协作'))
        self.c.tool(a['agent'], 'wait_for_reply', dict(message_id=m['id']))
        self.c.tool(b['agent'], 'send_message', dict(to=a['agent'], text='答案', reply_to=m['id']))
        result = self.c.tool(a['agent'], 'read_task_events', dict(task_id=a['id']))
        self.assertTrue(any(e['text'] == '协作' for e in result['events']))
        self.assertTrue(any(e['text'] == '答案' for e in result['events']))
        self.assertEqual(self.c.store.get('task', a['id'])['state'], 'running')

    def test_review_coalesces_and_deadline_does_not_extend(self):
        t = self.create(); original = t['deadline']
        self.now += 600; self.c.tick()
        self.now += 600; self.c.tick()
        reviews = [m for m in self.c.store.all('message') if m.get('key') == 'review']
        self.assertEqual(len(reviews), 1)
        self.assertEqual(self.c.store.get('task', t['id'])['deadline'], original)

    def paused_report(self):
        t = self.create()
        self.c.tool('master', 'control_task', dict(task_id=t['id'], action='pause', reason='环境阻塞'))
        self.report(t)
        self.clear_messages()
        return t

    def report(self, t):
        self.c.tool('master', 'list_tasks', {})
        return self.c.tool('master', 'finish_review', dict(text='本轮增量', cursors={t['id']: self.c.store.highwater()}))

    def queued_reviews(self):
        return [m for m in self.c.store.all('message') if m.get('key') == 'review' and m['state'] == 'queued']

    def test_unchanged_paused_task_and_telemetry_never_trigger_review(self):
        t = self.paused_report()
        for _ in range(8):
            for aid in ('master', t['agent']):
                for kind in ('account/rateLimits/updated', 'thread/tokenUsage/updated', 'thread/status/changed'):
                    self.c.store.emit('native:' + kind, actor=aid, data={})
            self.c.store.emit('native:' + 'item/completed', actor='master', data={'text': '审核结束'})
            self.c.tool('master', 'tell_owner', {'text': '此前报告的补充说明'})
            self.now += 600; self.c.tick()
            self.assertEqual(self.queued_reviews(), [])
        self.assertTrue(self.report(t)['skipped'])
        self.assertEqual(len([e for e in self.c.store.events(limit=500) if e['kind'] == 'review']), 1)

    def test_owner_and_worker_updates_each_trigger_one_new_review(self):
        t = self.paused_report()
        for source in ('owner', 'worker'):
            if source == 'owner':
                self.c.enqueue('owner', 'master', '环境已修复，请关注')
            else:
                self.c.store.emit('native:' + 'item/completed', actor=t['agent'], data={'text': '新的核验结果'})
            self.now += 600; self.c.tick()
            self.assertEqual(len(self.queued_reviews()), 1)
            self.report(t); self.clear_messages()
            self.now += 600; self.c.tick()
            self.assertEqual(self.queued_reviews(), [])

    def test_input_arriving_during_review_is_not_acknowledged(self):
        t = self.paused_report()
        self.c.enqueue('owner', 'master', '第一个变化')
        self.c.tool('master', 'list_tasks', {})
        self.c.enqueue('owner', 'master', '审核过程中刚到的新指令')
        self.c.tool('master', 'finish_review', dict(text='仅覆盖第一个变化', cursors={}))
        self.now += 600; self.c.tick()
        self.assertEqual(len(self.queued_reviews()), 1)
        self.report(t); self.clear_messages()
        self.now += 600; self.c.tick()
        self.assertEqual(self.queued_reviews(), [])

    def test_report_boundary_survives_restart_and_legacy_upgrade(self):
        self.paused_report()
        for legacy in (False, True):
            if legacy:
                schedule = self.c.store.get('settings', 'schedule')
                schedule.pop('reported_through'); schedule.pop('review_snapshot')
                self.c.store.put('settings', schedule)
            self.c.store.close()
            self.c = Center(self.tmp.name, clock=lambda: self.now)
            self.now += 600; self.c.tick()
            self.assertEqual(self.queued_reviews(), [])

    def test_new_fault_is_reviewable_but_read_only_inspection_is_not(self):
        t = self.paused_report()
        self.c.tool(t['agent'], 'list_tasks', {})
        self.c.tool(t['agent'], 'read_task_events', {'task_id': t['id']})
        self.now += 600; self.c.tick()
        self.assertEqual(self.queued_reviews(), [])
        self.c.store.emit('node_attention', '节点断开，结果未知', task=t['id'], target=t['agent'])
        self.now += 600; self.c.tick()
        self.assertEqual(len(self.queued_reviews()), 1)

    def test_priority_authority_and_roles(self):
        t = self.create()
        with self.assertRaises(ValueError): self.c.tool(t['agent'], 'send_message', dict(to='master', text='抢占', priority=10))
        with self.assertRaises(ValueError): self.c.tool(t['agent'], 'create_task', {})
        self.c.save_session('master', 'first')
        with self.assertRaises(RuntimeError):
            self.c.save_session('master', 'different')

    def test_live_orphan_blocks_duplicate_session_restart(self):
        import os
        t = self.create(); self.clear_messages()
        a = self.c.store.get('agent', t['agent'])
        a.update(status='running', session='original', pid=os.getpid(), pid_identity=process_identity(os.getpid()))
        self.c.store.put('agent', a); self.c.store.close()
        self.c = Center(self.tmp.name, clock=lambda: self.now)
        self.assertTrue(self.c.store.get('agent', a['id'])['paused'])
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        self.assertNotIn('pid', self.c.store.get('agent', a['id']))

    def test_answer_does_not_unpause_task(self):
        t = self.create(); q = self.question(t)
        self.c.tool('master', 'control_task', dict(task_id=t['id'], action='pause', reason='暂停'))
        with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=1, answer='A'))
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        self.assertTrue(self.c.store.get('agent', t['agent'])['paused'])

    def test_master_instruction_is_visible_with_queued_status(self):
        t = self.create()
        m = self.c.tool('master', 'send_message', dict(to=t['agent'], text='纠正方向'))
        events = self.c.store.events(visible=True)
        e = next(e for e in events if e['kind'] == 'instruction')
        self.assertEqual(e['text'], '纠正方向')
        self.assertEqual(e['data']['message_id'], m['id'])
        self.assertEqual(e['data']['delivery'], 'queued')

    def test_restart_marks_inflight_unknown_without_replay(self):
        t = self.create()
        m = self.c.store.all('message')[0]; m['state'] = 'running'; self.c.store.put('message', m)
        a = self.c.store.get('agent', t['agent']); a.update(status='running', session='original'); self.c.store.put('agent', a)
        self.c.store.close()
        self.c = Center(self.tmp.name, clock=lambda: self.now)
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'unknown')
        recovery = [x for x in self.c.store.all('message') if x['state'] == 'queued']
        self.assertEqual(len(recovery), 1)
        self.assertIn('先检查', recovery[0]['text'])
        self.assertEqual(self.c.store.get('agent', a['id'])['session'], 'original')


    def test_configure_master_only_replaces_demo_and_keeps_nodes(self):
        with self.c.store.atomic(): self.c.hub.register('remote')
        token = self.c.store.get('node', 'remote')['token']
        self.c.configure_master('codex', 'gpt-6-astra', 'medium')
        self.assertEqual(self.c.store.get('node', 'remote')['token'], token)
        a = self.c.store.get('agent', 'master')
        self.assertEqual((a['backend'], a['model'], a['reasoning_effort']), ('codex', 'gpt-6-astra', 'medium'))
        self.c.save_session('master', 'native-session')
        self.c.configure_master('codex', 'gpt-6-astra', 'medium')
        self.assertEqual(self.c.store.get('agent', 'master')['session'], 'native-session')
        # 已有真实会话：启动参数不同只给提示，以页面保存的配置为准。
        self.assertIn('沿用', self.c.configure_master('codex', 'another-model', 'medium'))
        self.assertEqual(self.c.store.get('agent', 'master')['model'], 'gpt-6-astra')

    def test_web_model_switch_keeps_session_and_backend_switch_needs_confirmation(self):
        self.c.configure_master('codex', 'gpt-6-astra', 'medium')
        self.c.save_session('master', 'native-session')
        with self.c.store.atomic():
            self.c.set_agent_config({'agent': 'master', 'backend': 'codex', 'model': 'gpt-6.1-sol', 'reasoning_effort': 'high'})
        a = self.c.store.get('agent', 'master')
        self.assertEqual((a['model'], a['reasoning_effort'], a['session']), ('gpt-6.1-sol', 'high', 'native-session'))
        with self.assertRaisesRegex(ValueError, '新的原生会话'):
            self.c.set_agent_config({'agent': 'master', 'backend': 'claude', 'model': 'claude-opus-5-5'})
        epoch = self.c.store.get('agent', 'master').get('epoch', 0)
        with self.c.store.atomic():
            self.c.set_agent_config({'agent': 'master', 'backend': 'claude', 'model': 'claude-opus-5-5', 'new_session': True})
        a = self.c.store.get('agent', 'master')
        self.assertEqual((a['backend'], a['session'], a['reasoning_effort'], a['epoch']), ('claude', None, None, epoch + 1))
        self.assertEqual(a['previous_sessions'][0]['session'], 'native-session')
        self.assertTrue(any(e['kind'] == 'agent_config' for e in self.c.store.events(visible=True, limit=500)))

    def test_configure_master_does_not_discard_pending_input(self):
        self.c.enqueue('owner', 'master', '尚未处理的请求')
        with self.assertRaises(ValueError): self.c.configure_master('codex', 'gpt-6-astra', 'medium')
        self.assertEqual(self.c.store.get('agent', 'master')['backend'], 'demo')

    def test_node_defaults_fill_backend_model_and_cwd(self):
        with self.c.store.atomic():
            self.c.hub.register('gpu')
            self.c.set_node_defaults({'node': 'gpu', 'backend': 'claude', 'model': 'claude-opus-5-5', 'cwd': '/srv/project'})
        args = dict(title='测试', description='只读验证', acceptance='给出证据', deadline='2099-01-01T00:00:00+00:00', node='gpu')
        t = self.c.tool('master', 'create_task', args)
        a = self.c.store.get('agent', t['agent'])
        self.assertEqual((a['backend'], a['model'], a['cwd']), ('claude', 'claude-opus-5-5', '/srv/project'))
        # 显式参数优先；换了后端时不沿用别的后端的模型名。
        t = self.c.tool('master', 'create_task', args | {'backend': 'demo', 'cwd': '/srv/other'})
        a = self.c.store.get('agent', t['agent'])
        self.assertEqual((a['backend'], a['model'], a['cwd']), ('demo', None, '/srv/other'))
        with self.c.store.atomic():
            self.c.set_node_defaults({'node': 'gpu'})
        with self.assertRaisesRegex(ValueError, '默认工作目录|cwd'):
            self.c.tool('master', 'create_task', args)

    def test_heartbeat_updates_keep_listing_order(self):
        with self.c.store.atomic():
            for name in ('zeta', 'alpha', 'mid'): self.c.hub.register(name)
        before = [n['id'] for n in self.c.store.all('node')]
        for n in self.c.store.all('node'):
            n['seen'] = self.now; self.c.store.put('node', n)
        self.assertEqual([n['id'] for n in self.c.store.all('node')], before)

    def test_remote_nodes_require_explicit_node(self):
        with self.c.store.atomic(): self.c.hub.register('remote')
        with self.assertRaisesRegex(ValueError, 'node'): self.create()

    def test_legacy_state_upgrade_keeps_sessions_and_marks_inflight_unknown(self):
        t = self.create()
        a = self.c.store.get('agent', t['agent'])
        a.update(token='legacy', session='keep'); a.pop('node'); self.c.store.put('agent', a)
        m = self.c.store.all('message')[0]; m['state'] = 'running'; self.c.store.put('message', m)
        self.c.store.close()
        self.c = Center(self.tmp.name, clock=lambda: self.now)
        a = self.c.store.get('agent', t['agent'])
        self.assertEqual((a['session'], a['node']), ('keep', 'local'))
        self.assertNotIn('token', a)
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'unknown')
