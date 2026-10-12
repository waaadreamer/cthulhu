"""One task-local reminder lifecycle behind ordinary Yog sends and the existing patrol."""
import asyncio
import time

from .roles import Role, Denied
from .store import new_id

INSPECT = ('[Automatic waiting-state inspection] Check existing work and collect fresh evidence. '
           'This is NOT authorization to restart. Reuse any collection already in flight. '
           'Read temple_task_state and latest instructions, locate the existing executor from roster/history '
           'if legacy associations are empty, then collect/finalize only actually completed authorized work.')


class Continuations:
    def __init__(self, temple):
        self.temple = temple
        self.lock = asyncio.Lock()

    def records(self):
        """Fold old records into one task -> recipient map; no second runtime state machine."""
        t = self.temple
        records = t.store.setting('continuations') or {}
        old_workers = t.store.setting('worker_nudges') or {}
        changed = bool(old_workers)
        shub = t.god(Role.SHUB)
        for task, rec in list(records.items()):
            if 'phase' in rec and shub:
                records[task] = {shub['id']: rec}
                changed = True
        for task, workers in old_workers.items():
            records.setdefault(task, {}).update({aid: rec for aid, rec in workers.items() if rec.get('message_id')})
        if changed:
            for by_target in records.values():
                for rec in by_target.values():
                    rec['phase'] = {'reminded': 'sent', 'executing': 'sent', 'nudged': 'sent',
                                    'responding': 'responded', 'owner_blocked': 'blocked'}.get(rec['phase'], rec['phase'])
                    for key in ('check_at', 'recheck_seconds', 'revision', 'purpose', 'source'):
                        rec.pop(key, None)
            t.store.set_setting('continuations', records)
            t.store.x("DELETE FROM settings WHERE key='worker_nudges'")
        return records

    async def snapshot(self, task_id, results=None):
        t, store = self.temple, self.temple.store
        task = store.task(task_id)
        if not task:
            raise ValueError('unknown task')
        messages = store.messages(task_id=task_id, limit=20)
        # Use all explicit associations, including older delegations; never parse prose IDs.
        links = store.q("SELECT DISTINCT sender, receiver FROM messages WHERE task_id=? AND kind!='deploy_request'", (task_id,))
        ids = {m[k] for m in links for k in ('sender', 'receiver')}
        agents = [a for a in store.agents(include_stopped=False) if a['role'] in (Role.OLD_ONE, Role.GHROTH, Role.NYARLATHOTEP)
                  and (a['id'] in ids or a['spec'].get('task_id') == task_id)]
        if results is None:
            results = await asyncio.gather(*(t.latest_result(a['id']) for a in agents))
        task = store.task(task_id)  # Re-read after network awaits; a concurrent report may have completed it.
        by_id = {r['agent_id']: r for r in results}
        executors = [by_id.get(a['id']) or {'agent_id': a['id'], 'online': False} for a in agents]
        instruction = store.one("SELECT * FROM messages WHERE task_id=? AND sender IN "
                                "(SELECT id FROM agents WHERE role='azathoth') AND receiver IN "
                                "(SELECT id FROM agents WHERE role='shub') ORDER BY created_at DESC LIMIT 1", (task_id,))
        report = store.one("SELECT * FROM messages WHERE task_id=? AND sender IN "
                           "(SELECT id FROM agents WHERE role='shub') AND status IS NOT NULL "
                           "AND receiver IN (SELECT id FROM agents WHERE role='azathoth') "
                           "ORDER BY created_at DESC LIMIT 1", (task_id,))
        owner_blocked = bool(report and report['status'] == 'blocked' and
                             (not instruction or report['created_at'] > instruction['created_at']))
        def task_active(r):
            return bool(r.get('current_task') == task_id and
                        (r.get('state') == 'working' or r.get('active_runs') or r.get('background_busy')))
        active = any(task_active(r) for r in executors)
        pending = store.q("SELECT id FROM messages WHERE task_id=? AND state!='handled' AND receiver IN "
                          "(SELECT id FROM agents WHERE role IN ('shub','old_one','ghroth','nyarlathotep')) AND sender NOT IN "
                          "(SELECT id FROM agents WHERE role='yog')", (task_id,))
        # A missing/offline/unknown runtime is not proof that restarting its workload is safe.
        unavailable = any(not r.get('online') or not r.get('state') or r.get('draining') for r in executors)
        return {'task': task, 'instruction': instruction, 'messages': messages, 'executors': executors,
                'active': active, 'pending': bool(pending), 'unavailable': unavailable,
                'owner_blocked': owner_blocked, 'report': report,
                'reminders': self.records().get(task_id, {})}

    async def send(self, yog_id, p):
        """Validate and deduplicate a normal send at current state; no client revision handshake."""
        t, task_id, target = self.temple, p.get('task_id'), p['to']
        if not task_id or not p.get('text', '').strip():
            raise Denied('task_id and concrete reminder text are required')
        row = await self.snapshot(task_id)
        async with self.lock:
            # Fresh store facts after network awaits, without holding a global lock during remote reads.
            row = await self.snapshot(task_id, row['executors'])
            direct = t.role_of(target) in (Role.OLD_ONE, Role.NYARLATHOTEP)
            worker = next((r for r in row['executors'] if r['agent_id'] == target), None)
            if direct and not worker:
                raise Denied('worker is not associated with this task')
            records = self.records()
            rec = records.get(task_id, {}).get(target, {})
            instruction = (row['instruction'] or {}).get('id')
            suppressed = None
            if row['task']['state'] != 'open' or row['owner_blocked']:
                suppressed = 'done_or_owner_blocked'
            elif rec.get('phase') == 'blocked' and rec.get('instruction_id') == instruction:
                suppressed = 'owner_blocked'
            elif t.store.agent(target)['desired'] != 'running':
                suppressed = 'recipient_stopped'
            elif t.store.agent(target)['galaxy_id'] in t.maintenance or t.store.agent(yog_id)['galaxy_id'] in t.maintenance:
                suppressed = 'draining'
            elif direct and (not worker.get('online') or not worker.get('state') or worker.get('draining') or
                             worker.get('current_task') not in (None, task_id)):
                suppressed = 'worker_unavailable_or_other_task'
            elif direct and worker.get('activity_task') == task_id and time.time() - (worker.get('last_activity_at') or 0) < 300:
                suppressed = 'recent_activity'
            elif not direct and (row['active'] or row['pending'] or any(r.get('draining') for r in row['executors'])):
                suppressed = 'in_flight'
            elif any(r.get('phase') == 'sent' for r in records.get(task_id, {}).values()):
                suppressed = 'in_flight'
            elif rec.get('phase') == 'escalated' and rec.get('instruction_id') == instruction:
                suppressed = 'escalated'
            elif t.store.q("SELECT id FROM messages WHERE task_id=? AND receiver=? AND state!='handled'", (task_id,target)):
                suppressed = 'in_flight'
            if suppressed:
                return {'suppressed': suppressed, 'id': rec.get('message_id'), 'task_id': task_id}
            now, mid = time.time(), new_id('reminder')
            text = (('[Task-scoped worker nudge] ' if direct else '[Task-scoped reminder] ') + p['text'] +
                    '\nThis message does not create work or authorize workload restart. Verify real execution, not delivery. '
                    'Report genuine owner-decision blockers. Latest delegated instruction:\n' + (row['instruction'] or {}).get('text',''))
            records.setdefault(task_id, {})[target] = {'phase': 'sent', 'at': now, 'deadline': now+900,
                'instruction_id': instruction, 'message_id': mid, 'text': text}
            t.store.set_setting('continuations', records)
            return await t.route(yog_id, target, text, task_id=task_id, priority=p.get('priority',0),
                                 kind='message', message_id=mid)

    async def scan(self, yog_id):
        t = self.temple
        results = await t.patrol()
        rows = []
        for task in t.store.q("SELECT * FROM tasks WHERE state='open' ORDER BY updated_at"):
            row = await self.snapshot(task['id'], results)
            async with self.lock:
                row = await self.snapshot(task['id'], row['executors'])
                if row['task']['state'] != 'open':
                    continue
                records = self.records()
                for target, rec in records.get(task['id'], {}).items():
                    shub = t.role_of(target) == Role.SHUB
                    workers = row['executors'] if shub else [r for r in row['executors'] if r['agent_id'] == target]
                    if row['owner_blocked']:
                        rec['phase'] = 'blocked'
                        rec['instruction_id'] = (row['instruction'] or {}).get('id')
                        continue
                    if rec.get('phase') == 'blocked' and rec.get('instruction_id') != (row['instruction'] or {}).get('id'):
                        rec['phase'] = 'waiting' if shub else 'responded'
                    # Tool activity is not a receipt: a completed text result may
                    # be substantive, and a worker may already be on later work.
                    evidence_at = [r['finished_at'] for r in workers
                                   if r.get('last_task') == task['id'] and r.get('last_status') == 'completed'
                                   and (r.get('activity') or (r.get('last_reply') or '').strip())
                                   and (r.get('finished_at') or 0) > rec.get('at', 0)]
                    if shub:
                        evidence_at.extend(m['created_at'] for m in t.store.q(
                            "SELECT created_at FROM messages WHERE task_id=? AND receiver=? "
                            "AND sender IN (SELECT id FROM agents WHERE role IN ('old_one','ghroth','nyarlathotep')) "
                            "AND kind='message' AND state='handled' AND trim(text)!='' AND created_at>?",
                            (task['id'], target, rec.get('at', 0))))
                    # Reconcile this collection from its consumed evidence and
                    # subsequent report, regardless of unrelated current work.
                    completed = any(not shub or (row['report'] and row['report']['created_at'] >= at)
                                    for at in evidence_at)
                    active = any(r.get('activity_task') == task['id'] and (r.get('last_activity_at') or 0) > rec.get('at',0) for r in workers)
                    if rec.get('phase') in ('sent','escalated') and (completed or (not shub and active)):
                        rec['phase'] = 'waiting' if shub else 'responded'
                    if rec.get('phase') != 'sent':
                        continue
                    # Recover reservation-before-route crashes using the exact persisted message ID.
                    if not t.store.message(rec['message_id']):
                        await t.route(yog_id, target, rec['text'], task_id=task['id'], kind='message', message_id=rec['message_id'])
                    if time.time() >= rec['deadline']:
                        az = t.god(Role.AZATHOTH)
                        if az:
                            mid = 'reminder_escalation_' + rec['message_id']
                            if not t.store.message(mid):
                                await t.route(yog_id, az['id'],
                                    f"Reminder {rec['message_id']} has no verified " + ('completed collection' if shub else 'worker response') +
                                    ' after 15 minutes. Coordinate the specific task failure; preserve existing workloads.',
                                    task_id=task['id'], status='progress', message_id=mid)
                            rec['phase'] = 'escalated'
                t.store.set_setting('continuations', records)
                row['reminders'] = records.get(task['id'], {})
            rows.append(row)
        deployment = t.deployer.status()
        return {'tasks': rows, 'agents': results, 'deployment': {
            'temple_revision': deployment['temple'].get('revision'),
            'galaxies': {gid: info.get('revision') for gid, info in deployment['galaxies'].items()},
            'jobs': [{k: j.get(k) for k in ('id', 'state', 'revision', 'finished_at')} for j in deployment['jobs'][:3]],
            'pending_requests': [r['id'] for r in deployment['requests']]}}
