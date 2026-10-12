import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from cthulhu import backends
from cthulhu.runtime import TurnResult, CodexRuntime
from tests import test_universe as universe


class RecoveryPolicyTests(unittest.IsolatedAsyncioTestCase):
    def pair(self, fallback=True):
        pair = object.__new__(backends.Backends)
        pair.primary = object()
        pair.fallback = object() if fallback else None
        pair.cfg = {'backend':'codex','model':'primary','fallback':{'backend':'claude','model':'fallback'}}
        pair.failed_at = 0
        pair.hold = backends.FALLBACK_HOLD
        return pair

    async def test_exact_attempts_and_real_fallback_with_checked_context(self):
        pair = self.pair()
        calls, statuses = [], []
        async def attempt(rt, note):
            calls.append((rt,note))
            return TurnResult('failed',error='invalid_stream') if rt is pair.primary else TurnResult('completed','done')
        prepare = AsyncMock(return_value='checked persisted execution')
        async def report(s): statuses.append(s)
        with patch.object(backends,'RETRY_DELAYS',(0,)*5):
            result = await pair.recover(attempt,prepare=prepare,report=report)
        self.assertEqual(result.status,'completed')
        self.assertEqual([r for r,n in calls],[pair.primary]*6+[pair.fallback])
        self.assertEqual(calls[-1][1],'checked persisted execution')
        self.assertEqual(prepare.await_count,6)
        self.assertEqual(statuses[-1]['attempt'],7)
        self.assertTrue(statuses[-1]['fallback'])

    async def test_cancellation_during_delay_never_calls_again(self):
        pair = self.pair(); cancel=asyncio.Event()
        attempt=AsyncMock(return_value=TurnResult('failed',error='timeout'))
        async def report(s):
            if s['phase']=='retrying': cancel.set()
        result=await pair.recover(attempt,report=report,cancel=cancel)
        self.assertEqual(result.status,'interrupted'); attempt.assert_awaited_once()
        self.assertFalse(pair.using_fallback)

    async def test_source_is_pinned_until_long_fallback_turn_finishes(self):
        pair=self.pair(); pair.trip()
        async def attempt(rt,note):
            pair.failed_at=0  # The hold expired while this turn was still running.
            self.assertIs(pair.active,rt)
            self.assertEqual(pair.active_cfg['model'],'fallback')
            return TurnResult('completed')
        result=await pair.recover(attempt)
        self.assertEqual(result.status,'completed')
        self.assertTrue(pair.using_fallback)

    async def test_cancel_during_status_publish_does_not_dispatch(self):
        pair=self.pair(); cancel=asyncio.Event(); attempt=AsyncMock()
        async def report(s):
            if s['phase']=='running': cancel.set()
        result=await pair.recover(attempt,report=report,cancel=cancel)
        self.assertEqual(result.status,'interrupted')
        attempt.assert_not_awaited()

    async def test_success_arriving_during_timeout_settlement_is_preserved(self):
        rt=CodexRuntime('unused','.')
        rt.request=AsyncMock(return_value={'turn':{'id':'turn'}})
        async def interrupt(thread, turn):
            rt.turns[turn].set_result(TurnResult('completed','already completed'))
        rt.interrupt=interrupt
        result=await rt.run_turn('thread','task',timeout=.001)
        self.assertEqual(result.status,'completed')
        self.assertEqual(result.text,'already completed')

    async def test_nonretryable_and_exhaustion_are_explicit(self):
        for error in ['401 unauthorized','403 forbidden','invalid argument','permission denied']:
            pair=self.pair(); attempt=AsyncMock(return_value=TurnResult('failed',error=error))
            result=await pair.recover(attempt)
            attempt.assert_awaited_once(); self.assertEqual(result.error,error)
        pair=self.pair(False); attempt=AsyncMock(return_value=TurnResult('failed',error='provider_disconnected'))
        with patch.object(backends,'RETRY_DELAYS',(0,)*5):
            result=await pair.recover(attempt)
        self.assertEqual(attempt.await_count,6)
        self.assertIn('no configured fallback',result.error)

    async def test_routing_discovery_failure_does_not_stop_at_retry_three(self):
        pair = self.pair(False)
        errors = ['workspace routing discovery timed out'] * 3 + ['workspace routing discovery failed'] * 3
        attempt = AsyncMock(side_effect=[TurnResult('failed', error=e) for e in errors])
        statuses = []
        async def report(state): statuses.append(state)
        with patch.object(backends, 'RETRY_DELAYS', (0,) * 5):
            result = await pair.recover(attempt, report=report)
        self.assertEqual(attempt.await_count, 6)
        self.assertEqual(statuses[-1]['attempt'], 6)
        self.assertEqual(statuses[-1]['retry'], 5)
        self.assertIn('no configured fallback is available', result.error)
        self.assertFalse(backends.recoverable('workspace routing discovery failed: 401 unauthorized'))
        self.assertFalse(backends.recoverable('workspace routing discovery failed: invalid argument'))

    async def test_unsettled_execution_blocks_new_inference(self):
        pair=self.pair(); attempt=AsyncMock(return_value=TurnResult('failed',error='timeout'))
        prepare=AsyncMock(side_effect=RuntimeError('previous tool still running'))
        result=await pair.recover(attempt,prepare=prepare)
        attempt.assert_awaited_once(); self.assertIn('cannot safely continue',result.error)

    async def test_checkpoint_waits_for_dispatched_tool_and_reads_persisted_turn(self):
        rt=CodexRuntime('unused','.')
        rt.proc=SimpleNamespace(returncode=None)
        finished=asyncio.Event()
        async def tool():
            await asyncio.sleep(.03); finished.set()
        task=asyncio.create_task(tool());rt.serving[task]='thread'
        async def read(*a,**kw):
            self.assertTrue(finished.is_set())
            return {'thread':{'turns':[{'status':'failed','items':[{'result':'already written'}]}]}}
        rt.request=AsyncMock(side_effect=read)
        snapshot=await rt.recovery_checkpoint('thread',True)
        self.assertIn('already written',snapshot)
        await task


class RecoveryServerTests(universe.HarnessServer):
    """Real temple + galaxy + agent + native RPC subprocess; only inference is fictional."""
    patrol_interval=3600

    def configure(self, fallback=True):
        cfg={'backend':'codex','model':'recovery-primary'}
        if fallback: cfg['fallback']={'backend':'codex','model':'recovery-fallback'}
        self.api('runtimes',{'key':'ghroth','config':cfg})
        self.wait(lambda: self.runtime()['model']=='recovery-primary')

    def runtime(self):
        return next(a for a in self.api('directory')['agents'] if a['id']=='gh')['runtime']

    def completed(self, mid):
        return next((t for t in self.api('chat?agent_id=gh') if t['id']==mid and t['status'] not in ('running','queued')),None)

    def test_five_retries_then_fallback_preserves_task_and_one_effect(self):
        self.configure()
        msg=self.api('send',{'to':'gh','text':'RECOVERY_FIXTURE fallback task_fixture'})
        turn=self.wait(lambda:self.completed(msg['id']),40)
        self.assertEqual(turn['status'],'done',turn)
        self.assertIn('RECOVERED_WITH_EXISTING_EFFECT once',turn['reply'])
        rows=[json.loads(s) for s in (self.dir/'recovery-fallback.jsonl').read_text().splitlines()]
        self.assertEqual([r['model'] for r in rows],['recovery-primary']*6+['recovery-fallback'])
        self.assertTrue(all(r['checked_context'] for r in rows[1:]))
        self.assertTrue(all(r['task_present'] for r in rows))
        self.assertEqual((self.dir/'recovery-fallback-effect').read_text(),'once')
        self.assertEqual(self.runtime()['recovery']['attempt'],7)
        self.assertEqual(self.runtime()['recovery']['phase'],'completed')
        self.assertTrue(self.runtime()['fallback_active'])

    def test_nonretryable_stays_failed_without_fallback(self):
        self.configure(False)
        msg=self.api('send',{'to':'gh','text':'RECOVERY_FIXTURE fatal'})
        turn=self.wait(lambda:self.completed(msg['id']))
        self.assertEqual(turn['status'],'failed')
        self.assertEqual(len((self.dir/'recovery-fatal.jsonl').read_text().splitlines()),1)
        self.assertEqual(self.runtime()['recovery']['phase'],'failed')

    def test_workspace_routing_discovery_fails_through_fifth_retry_then_fallback(self):
        self.configure()
        msg = self.api('send', {'to': 'gh', 'text': 'RECOVERY_FIXTURE routing task_fixture'})
        turn = self.wait(lambda: self.completed(msg['id']), 40)
        self.assertEqual(turn['status'], 'done', turn)
        rows = [json.loads(s) for s in (self.dir/'recovery-routing.jsonl').read_text().splitlines()]
        self.assertEqual([r['model'] for r in rows], ['recovery-primary'] * 6 + ['recovery-fallback'])
        self.assertTrue(all(r['checked_context'] for r in rows[1:]))
        self.assertTrue(all(r['task_present'] for r in rows))
        self.assertEqual((self.dir/'recovery-routing-effect').read_text(), 'once')
        self.assertEqual(self.runtime()['recovery']['attempt'], 7)
        self.assertTrue(self.runtime()['recovery']['fallback'])
        self.assertEqual(self.runtime()['recovery']['phase'], 'completed')

    def test_owner_cancel_during_recovery(self):
        self.configure()
        msg=self.api('send',{'to':'gh','text':'RECOVERY_FIXTURE cancelled'})
        self.wait(lambda:self.runtime().get('recovery',{}).get('phase')=='retrying')
        self.api('stop',{'agent_id':'gh'})
        turn=self.wait(lambda:self.completed(msg['id']))
        self.assertEqual(turn['status'],'interrupted',turn)
        self.assertFalse(self.runtime()['fallback_active'])
