"""Task-scoped supervision; only Shub decides how work is resumed."""
import asyncio
import hashlib
import json
import time

from .roles import Role


class Continuations:
    def __init__(self, temple):
        self.temple = temple
        self.lock = asyncio.Lock()

    @staticmethod
    def collection_source(row):
        """Identify actual completed collection, not a new paraphrase of an old report."""
        results = [r for r in row['executors'] if r.get('last_task') == row['task']['id'] and
                   r.get('finished_at') and r.get('activity') and r.get('last_status') == 'completed']
        if not results:
            return None
        result = max(results, key=lambda r: r['finished_at'])
        return {'agent_id': result['agent_id'], 'finished_at': result['finished_at']}

    async def remind(self, yog_id, row, evidence, purpose='resume'):
        t = self.temple
        shub = t.god(Role.SHUB)
        if not shub or shub['desired'] != 'running' or shub['galaxy_id'] in t.maintenance:
            return None
        from .store import new_id
        now, mid = time.time(), new_id('continuation')
        if purpose == 'inspect':
            text = ('[Automatic waiting-state inspection] This patrol checks the waiting condition of this unfinished task. '
                    'This is NOT evidence that its background workload stopped and NOT authorization to restart it. '
                    'Read temple_task_state/latest instructions, reuse any collection already in flight, and ask the '
                    'existing executor for one read-only check of the current process/job, progress, last update and '
                    'completion/observer result. If legacy executor associations are empty, use your roster/history '
                    'to locate the existing executor and include task_id in the request; do not create a duplicate workload. '
                    'Collect/finalize only if the existing work actually finished and the latest instructions authorize it. ')
        else:
            text = ('[Automatic continuation review] This task is unfinished and assessed ready with no active executor. '
                    'Read temple_task_state and the latest owner instructions; inspect actual workload state. '
                    'Resume, retry, or reassign only authorized work; preserve active downloads/evaluations/policies. ')
        text += ('Verify executor activity, not delivery; report genuine owner-decision blockers. '
                 f"Evidence: {evidence}\nLatest delegated instruction:\n{(row['instruction'] or {}).get('text', '')}")
        rec = {'phase': 'reminded', 'purpose': purpose, 'revision': row['revision'],
               'instruction_id': (row['instruction'] or {}).get('id'), 'at': now, 'deadline': now + 900,
               'message_id': mid, 'evidence': evidence, 'text': text}
        records = t.store.setting('continuations') or {}
        records[row['task']['id']] = rec
        t.store.set_setting('continuations', records)
        await t.route(yog_id, shub['id'], text, task_id=row['task']['id'],
                      kind='continuation', message_id=mid)
        return rec

    async def snapshot(self, task_id, results=None):
        t, store = self.temple, self.temple.store
        task = store.task(task_id)
        if not task:
            raise ValueError('unknown task')
        messages = store.messages(task_id=task_id, limit=20)
        # Use all explicit associations, including older delegations; never parse prose IDs.
        links = store.q("SELECT DISTINCT sender, receiver FROM messages WHERE task_id=? AND kind!='deploy_request'", (task_id,))
        ids = {m[k] for m in links for k in ('sender', 'receiver')}
        agents = [a for a in store.agents(include_stopped=False) if a['role'] in (Role.OLD_ONE, Role.GHROTH)
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
                           "ORDER BY created_at DESC LIMIT 1", (task_id,))
        owner_blocked = bool(report and report['status'] == 'blocked' and
                             (not instruction or report['created_at'] > instruction['created_at']))
        def task_active(r):
            return bool((r.get('current_task') or r.get('last_task')) == task_id and
                        (r.get('state') == 'working' or r.get('active_runs') or r.get('background_busy')))
        active = any(task_active(r) for r in executors)
        pending = store.q("SELECT id FROM messages WHERE task_id=? AND state!='handled' AND receiver IN "
                          "(SELECT id FROM agents WHERE role IN ('shub','old_one','ghroth')) AND sender NOT IN "
                          "(SELECT id FROM agents WHERE role='yog')", (task_id,))
        # A missing/offline/unknown runtime is not proof that restarting its workload is safe.
        unavailable = any(not r.get('online') or not r.get('state') or r.get('draining') for r in executors)
        revision = hashlib.sha256(json.dumps([task['state'], instruction and instruction['id'],
            [(m['id'], m['state']) for m in messages if t.role_of(m['sender']) != Role.YOG],
            [{'agent_id': r['agent_id'], 'online': r.get('online'), 'draining': r.get('draining'),
              'active': task_active(r), 'known_state': bool(r.get('state')),
              'finished_at': r.get('finished_at') if r.get('last_task') == task_id else None}
             for r in executors]],
            sort_keys=True).encode()).hexdigest()
        return {'task': task, 'instruction': instruction, 'messages': messages, 'executors': executors,
                'active': active, 'pending': bool(pending), 'unavailable': unavailable,
                'owner_blocked': owner_blocked, 'revision': revision,
                'worker_nudges': {aid: rec for aid, rec in (store.setting('worker_nudges') or {}).get(task_id, {}).items()},
                'continuation': (store.setting('continuations') or {}).get(task_id, {})}

    async def worker_review(self, yog_id, p):
        """A scoped observation/nudge, never a restart or a new assignment."""
        async with self.lock:
            t = self.temple
            row = await self.snapshot(p['task_id'])
            if row['revision'] != p['revision']:
                return {'state': 'changed'}
            aid = p['agent_id']
            worker = next((r for r in row['executors'] if r['agent_id'] == aid), None)
            if not worker or t.role_of(aid) != Role.OLD_ONE:
                raise ValueError('an associated old_one is required')
            if p['state'] not in ('healthy', 'stalled') or not p.get('evidence', '').strip():
                raise ValueError('healthy/stalled assessment with concrete evidence is required')
            records = t.store.setting('worker_nudges') or {}
            by_agent = records.setdefault(p['task_id'], {})
            rec = by_agent.get(aid, {})
            if p['state'] == 'healthy':
                # Healthy background progress can be established independently of agent tool activity.
                if rec.get('phase') == 'healthy' and rec.get('revision') == row['revision']:
                    return {'state': 'healthy'}
                by_agent[aid] = {'phase': 'healthy', 'evidence': p['evidence'], 'at': time.time(),
                                 'revision': row['revision']}
                t.store.set_setting('worker_nudges', records)
                return {'state': 'healthy'}
            if (row['task']['state'] != 'open' or row['owner_blocked'] or
                    row['continuation'].get('phase') in ('waiting', 'owner_blocked') or
                    not worker.get('online') or not worker.get('state') or worker.get('draining') or
                    t.store.agent(aid)['galaxy_id'] in t.maintenance or
                    worker.get('current_task') not in (None, p['task_id'])):
                return {'state': 'guarded'}
            now = time.time()
            if worker.get('activity_task') == p['task_id'] and now - (worker.get('last_activity_at') or 0) < 300:
                return {'state': 'recent_activity'}
            instruction_id = (row['instruction'] or {}).get('id')
            if rec.get('phase') in ('nudged', 'escalated') and rec.get('instruction_id') == instruction_id:
                return {'state': 'duplicate'}
            if rec.get('phase') == 'healthy' and rec.get('revision') == row['revision']:
                return {'state': 'healthy', 'reason': 'no observed change since healthy assessment'}
            from .store import new_id
            text = ('[Task-scoped worker nudge] Yog observed possible stalled progress in your existing assignment. '
                    'Check the specific issue and report concrete current activity/results or the blocker to Shub with task_id. '
                    'Preserve healthy background evaluation/download/training/policies. This message does not create work, '
                    'authorize workload restart, or override owner instructions. If already progressing, report fresh evidence; '
                    'otherwise proceed only within your current authorized assignment. '
                    f"Evidence: {p['evidence']}\nLatest delegated instruction:\n{(row['instruction'] or {}).get('text', '')}")
            rec = {'phase': 'nudged', 'at': now, 'deadline': now + 900, 'instruction_id': instruction_id,
                   'message_id': new_id('worker_nudge'), 'text': text, 'evidence': p['evidence']}
            by_agent[aid] = rec
            t.store.set_setting('worker_nudges', records)
            await t.route(yog_id, aid, text, kind='worker_nudge', task_id=p['task_id'], message_id=rec['message_id'])
            return {'state': 'nudged', 'message_id': rec['message_id']}

    async def check_worker_nudges(self, yog_id, row):
        t = self.temple
        records = t.store.setting('worker_nudges') or {}
        for aid, rec in records.get(row['task']['id'], {}).items():
            if rec['phase'] != 'nudged':
                continue
            if row['owner_blocked'] or row['continuation'].get('phase') in ('waiting', 'owner_blocked'):
                rec['phase'] = 'waiting'
                continue
            if rec['instruction_id'] != (row['instruction'] or {}).get('id'):
                rec['phase'] = 'changed'
                continue
            # Recover a crash between reservation and message persistence without duplication.
            if not t.store.message(rec['message_id']):
                a = t.store.agent(aid)
                if a and a['desired'] == 'running':
                    await t.route(yog_id, aid, rec['text'], kind='worker_nudge', task_id=row['task']['id'], message_id=rec['message_id'])
            worker = next((r for r in row['executors'] if r['agent_id'] == aid), {})
            if worker.get('activity_task') == row['task']['id'] and (worker.get('last_activity_at') or 0) > rec['at']:
                rec['phase'] = 'responding'
            elif time.time() >= rec['deadline']:
                # A still-working flag or handled receipt alone is not evidence of a response.
                az = t.god(Role.AZATHOTH)
                if az:
                    mid = 'worker_escalation_' + rec['message_id']
                    if not t.store.message(mid):
                        await t.route(yog_id, az['id'],
                            f"Worker {aid} has no verified new activity after task-scoped nudge {rec['message_id']}. "
                            'Inspect its current evidence and involve Shub for coordination if needed; do not restart a healthy workload.',
                            task_id=row['task']['id'], status='progress', message_id=mid)
                    rec['phase'] = 'escalated'
        t.store.set_setting('worker_nudges', records)

    async def scan(self, yog_id):
        t = self.temple
        results = await t.patrol()
        rows = []
        for task in t.store.q("SELECT * FROM tasks WHERE state='open' ORDER BY updated_at"):
            row = await self.snapshot(task['id'], results)
            async with self.lock:
                if t.store.task(task['id'])['state'] != 'open':
                    continue
                await self.check_worker_nudges(yog_id, row)
                row['worker_nudges'] = (t.store.setting('worker_nudges') or {}).get(task['id'], {})
                records = t.store.setting('continuations') or {}
                rec = records.get(task['id'], {})
                source = self.collection_source(row)
                if (rec.get('purpose') == 'inspect' and rec.get('phase') in ('reminded', 'executing', 'escalated') and
                        source and source['finished_at'] > rec['at'] and not row['owner_blocked']):
                    # Re-arm independently of a successful model review. This is another inspection,
                    # not a claim that the workload is healthy or permission to rerun it.
                    rec = {'phase': 'waiting', 'revision': row['revision'], 'source': source,
                           'at': source['finished_at'],
                           'instruction_id': (row['instruction'] or {}).get('id'),
                           'evidence': 'A new task-scoped collection finished; interpret its latest result and report it. '
                                       'Task remains open; inspect on each patrol without restarting workload.'}
                    records[task['id']] = rec
                    t.store.set_setting('continuations', records)
                if (rec.get('phase') == 'waiting' and
                        not row['owner_blocked'] and not row['active'] and not row['pending'] and
                        not any(r.get('draining') for r in row['executors'])):
                    updated = await self.remind(yog_id, row, rec.get('evidence', ''), purpose='inspect')
                    if updated:
                        rec = updated
                        records[task['id']] = rec
                if rec.get('phase') == 'reminded' and rec.get('instruction_id') != (row['instruction'] or {}).get('id'):
                    rec = rec | {'phase': 'changed'}
                    records[task['id']] = rec
                    t.store.set_setting('continuations', records)
                if rec.get('phase') == 'reminded':
                    # Recover the tiny reserve-before-route crash window using the same message ID.
                    if not t.store.message(rec['message_id']) and rec.get('text'):
                        shub = t.god(Role.SHUB)
                        if shub and shub['desired'] == 'running':
                            await t.route(yog_id, shub['id'], rec['text'], task_id=task['id'],
                                          kind='continuation', message_id=rec['message_id'])
                    # Delivery/handled receipts and Shub's own activity are not executor progress.
                    progressed = any((r.get('current_task') == task['id'] and
                                      (r.get('working_since') or 0) > rec['at'] and r.get('live')) or
                                     (r.get('last_task') == task['id'] and
                                      (r.get('finished_at') or 0) > rec['at'] and r.get('activity'))
                                     for r in row['executors'])
                    if progressed:
                        rec = rec | {'phase': 'executing', 'progress_at': time.time()}
                    elif row['owner_blocked']:
                        rec = rec | {'phase': 'owner_blocked'}
                    elif time.time() >= rec['deadline'] and not row['active']:
                        az = t.god(Role.AZATHOTH)
                        if az:
                            mid = f"continuation_escalation_{rec['message_id']}"
                            if not t.store.message(mid):
                                await t.route(yog_id, az['id'],
                                    'Automatic continuation stalled: no executor activity was verified after the reminder. '
                                    'Shub must inspect actual execution or report the concrete blocker; delivery is not progress. '
                                    f"Reminder: {rec['message_id']}", task_id=task['id'], status='progress', message_id=mid)
                            rec = rec | {'phase': 'escalated'}
                    records[task['id']] = rec
                    t.store.set_setting('continuations', records)
                if (rec.get('purpose') == 'inspect' and rec.get('phase') == 'executing' and
                        time.time() >= rec['deadline']):
                    # Starting a tool is not a completed collection. Keep a bounded failure exit
                    # even if the agent stays working or finishes with an error.
                    az = t.god(Role.AZATHOTH)
                    if az:
                        mid = f"continuation_escalation_{rec['message_id']}"
                        if not t.store.message(mid):
                            await t.route(yog_id, az['id'],
                                'Waiting-state collection started but has no verified completed result within 15 minutes. '
                                'Shub must inspect the collection failure or delay; preserve the existing workload. '
                                f"Reminder: {rec['message_id']}", task_id=task['id'], status='progress', message_id=mid)
                        rec = rec | {'phase': 'escalated'}
                        records[task['id']] = rec
                        t.store.set_setting('continuations', records)
                row['continuation'] = rec
            rows.append(row)
        deployment = t.deployer.status()
        return {'tasks': rows, 'agents': results, 'deployment': {
            'temple_revision': deployment['temple'].get('revision'),
            'galaxies': {gid: info.get('revision') for gid, info in deployment['galaxies'].items()},
            'jobs': [{k: j.get(k) for k in ('id', 'state', 'revision', 'finished_at')} for j in deployment['jobs'][:3]],
            'pending_requests': [r['id'] for r in deployment['requests']]}}

    async def review(self, yog_id, p):
        async with self.lock:
            t = self.temple
            row = await self.snapshot(p['task_id'])
            if p['revision'] != row['revision']:
                return {'state': 'changed', 'reason': 'refresh task state before deciding'}
            state = p['state']
            if state not in ('ready', 'waiting', 'owner_blocked') or not p.get('evidence', '').strip():
                raise ValueError('state and concrete evidence are required')
            if row['task']['state'] != 'open':
                return {'state': 'done'}
            records = t.store.setting('continuations') or {}
            rec = records.get(p['task_id'], {})
            if rec.get('phase') == 'owner_blocked' and rec.get('instruction_id') == (row['instruction'] or {}).get('id'):
                return {'state': 'owner_blocked', 'reason': 'a new authorized instruction is required'}
            if state != 'ready':
                if (state == 'waiting' and rec.get('purpose') == 'inspect' and
                        rec.get('phase') in ('reminded', 'executing', 'escalated')):
                    return {'state': rec['phase'], 'reason': 'collection already requested; scan tracks its completion'}
                if rec.get('revision') == row['revision'] and rec.get('phase') == state:
                    return {'state': state}
                # Rewording an unchanged assessment does not rearm a reminder.
                if rec.get('revision') == row['revision'] and rec.get('phase') in ('reminded', 'escalated'):
                    return {'state': 'duplicate'}
                rec = {'phase': state, 'revision': row['revision'], 'evidence': p['evidence'], 'at': time.time(),
                       'instruction_id': (row['instruction'] or {}).get('id')}
            else:
                if row['owner_blocked'] or row['active'] or row['pending'] or row['unavailable']:
                    return {'state': 'guarded', 'active': row['active'], 'pending': row['pending'],
                            'unavailable': row['unavailable'], 'owner_blocked': row['owner_blocked']}
                instruction_id = (row['instruction'] or {}).get('id')
                if rec.get('phase') in ('reminded', 'escalated') and rec.get('instruction_id') == instruction_id:
                    return {'state': 'duplicate'}
                if rec.get('phase') == 'waiting' and rec.get('revision') == row['revision'] and rec.get('evidence') == p['evidence']:
                    return {'state': 'waiting', 'reason': 'no observed readiness change'}
                rec = await self.remind(yog_id, row, p['evidence'])
                if rec is None:
                    return {'state': 'unavailable'}
            records[p['task_id']] = rec
            t.store.set_setting('continuations', records)
            return {'state': rec['phase'], 'task_id': p['task_id'], 'message_id': rec.get('message_id')}
