import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from cthulhu_v4.core import Core, process_identity


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = 1000.0
        self.c = Core(self.tmp.name, clock=lambda: self.now)

    def tearDown(self):
        self.c.store.db.close(); self.tmp.cleanup()

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

    def test_owner_late_correction_is_preserved(self):
        t = self.create(); q = self.question(t)
        self.c.tool('master', 'answer_question', dict(question_id=q['id'], version=1, answer='A'))
        with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=2, answer='B'))
        q = self.c.store.get('question', q['id'])
        self.assertEqual([h['text'] for h in q['history']], ['A', 'B'])
        self.assertEqual(q['version'], 3)

    def test_paused_waiting_and_live_turn_never_recovered(self):
        t = self.create(); self.clear_messages()
        self.c.running[t['agent']] = object()
        self.now += 1000; self.c.tick()
        self.assertFalse(any(m.get('key', '').startswith('recover:') for m in self.c.store.all('message') if m.get('key')))
        self.c.running.clear()
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
                    self.c.record_native(aid, kind, {})
            self.c.record_native('master', 'item/completed', {'text': '审核结束'})
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
                self.c.record_native(t['agent'], 'item/completed', {'text': '新的核验结果'})
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
            self.c.store.db.close()
            self.c = Core(self.tmp.name, clock=lambda: self.now)
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
        self.c.store.put('agent', a); self.c.store.db.close()
        self.c = Core(self.tmp.name, clock=lambda: self.now)
        self.assertTrue(self.c.store.get('agent', a['id'])['paused'])
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'paused')
        with self.assertRaises(ValueError):
            self.c.tool('master', 'control_task', dict(task_id=t['id'], action='resume', reason='test'))
        self.assertFalse(any(m['state'] == 'queued' and m['target'] == a['id'] for m in self.c.store.all('message')))

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
        self.c.store.db.close()
        self.c = Core(self.tmp.name, clock=lambda: self.now)
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'unknown')
        recovery = [x for x in self.c.store.all('message') if x['state'] == 'queued']
        self.assertEqual(len(recovery), 1)
        self.assertIn('先检查', recovery[0]['text'])
        self.assertEqual(self.c.store.get('agent', a['id'])['session'], 'original')


    def test_activate_demo_preserves_nodes_and_refuses_reset_real_session(self):
        self.c.galaxies.provision('remote')
        token = self.c.store.get('node', 'remote')['token']
        self.c.activate_codex_master('gpt-6-astra', 'medium')
        self.assertEqual(self.c.store.get('node', 'remote')['token'], token)
        a = self.c.store.get('agent', 'master')
        self.assertEqual((a['backend'], a['model'], a['reasoning_effort']), ('codex', 'gpt-6-astra', 'medium'))
        self.c.save_session('master', 'native-session')
        self.c.activate_codex_master('gpt-6-astra', 'medium')
        self.assertEqual(self.c.store.get('agent', 'master')['session'], 'native-session')
        with self.assertRaises(ValueError): self.c.activate_codex_master('another-model', 'medium')

    def test_activate_demo_does_not_discard_pending_input(self):
        self.c.enqueue('owner', 'master', '尚未处理的请求')
        with self.assertRaises(ValueError): self.c.activate_codex_master('gpt-6-astra', 'medium')
        self.assertEqual(self.c.store.get('agent', 'master')['backend'], 'demo')


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.c = Core(self.tmp.name)

    async def asyncTearDown(self):
        await self.c.close(); self.c.store.db.close(); self.tmp.cleanup()

    async def settle(self):
        for _ in range(80):
            await self.c.pump(); await asyncio.sleep(0.02)
            if not self.c.running and not any(m['state'] == 'queued' for m in self.c.store.all('message')): return
        self.fail('未能停止')

    async def test_demo_full_question_answer_complete_flow(self):
        t = self.c.tool('master', 'create_task', dict(title='演示', description='模拟', acceptance='问答完成',
            deadline='2099-01-01T00:00:00+00:00', cwd=self.tmp.name))
        await self.settle()
        q = self.c.store.all('question')[0]
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'waiting')
        with self.c.store.atomic(): self.c.answer('owner', dict(question_id=q['id'], version=q['version'], answer='继续'))
        await self.settle()
        self.assertEqual(self.c.store.get('task', t['id'])['state'], 'completed')
        self.assertTrue(self.c.store.get('agent', t['agent'])['session'].startswith('demo-'))

    async def test_one_active_turn_and_priority_order(self):
        gate = asyncio.Event(); seen = []
        class Fake:
            async def run(inner, text):
                seen.append(json.loads(text.split('\n', 1)[1])['message']['text']); await gate.wait(); return 'done'
            async def close(inner): pass
        self.c.runtime_factory = lambda *_: Fake()
        with self.c.store.atomic():
            self.c.enqueue('system', 'master', 'low', 1)
            self.c.enqueue('owner', 'master', 'high', 9)
        await self.c.pump(); await self.c.pump(); await asyncio.sleep(0.01)
        self.assertEqual(seen, ['high'])
        self.assertEqual(len(self.c.running), 1)
        gate.set(); await self.settle()
        self.assertEqual(seen, ['high', 'low'])

    async def test_runtime_failure_does_not_claim_handled(self):
        class Bad:
            async def run(inner, text): raise RuntimeError('broken')
            async def close(inner): pass
        self.c.runtime_factory = lambda *_: Bad()
        m = self.c.enqueue('owner', 'master', 'test')
        await self.settle()
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'failed')
        self.assertTrue(self.c.store.get('agent', 'master')['paused'])

    async def test_review_covered_while_queued_never_calls_runtime(self):
        def unexpected(*_):
            self.fail('无增量的排队审核不应启动模型')
        self.c.runtime_factory = unexpected
        m = self.c.enqueue('system', 'master', '排队审核', key='review')
        await self.c.pump()
        self.assertEqual(self.c.store.get('message', m['id'])['state'], 'cancelled')
        self.assertEqual(self.c.running, {})

    async def test_priority10_interrupts_before_new_turn_and_closes_runtime(self):
        seen, closed = [], []
        class Fake:
            async def run(inner, text):
                value = json.loads(text.split('\n', 1)[1])['message']['text']
                seen.append(value)
                if value == 'slow': await asyncio.sleep(60)
                return 'done'
            async def close(inner): closed.append(True)
        self.c.runtime_factory = lambda *_: Fake()
        old = self.c.enqueue('owner', 'master', 'slow', 9)
        await self.c.pump(); await asyncio.sleep(.01)
        urgent = self.c.enqueue('owner', 'master', 'urgent', 10)
        await self.settle()
        self.assertEqual(seen, ['slow', 'urgent'])
        self.assertTrue(closed)
        self.assertEqual(self.c.store.get('message', old['id'])['state'], 'unknown')
        self.assertEqual(self.c.store.get('message', urgent['id'])['state'], 'handled')

    async def test_priority10_before_coroutine_starts_does_not_leak_slot(self):
        old = self.c.enqueue('owner', 'master', 'queued', 9)
        await self.c.pump()
        urgent = self.c.enqueue('owner', 'master', 'urgent', 10)
        await self.settle()
        messages = self.c.store.all('message')
        self.assertTrue(all(m['state']=='handled' for m in messages))
        dispatch = [e['data']['message_id'] for e in self.c.store.events() if e['kind']=='dispatch']
        self.assertEqual(dispatch, [urgent['id'], old['id']])
