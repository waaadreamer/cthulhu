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

    async def remind(self, yog_id, row, evidence, purpose='resume'):
        t = self.temple
        shub = t.god(Role.SHUB)
        if not shub or shub['desired'] != 'running' or shub['galaxy_id'] in t.maintenance:
            return None
        from .store import new_id
        now, mid = time.time(), new_id('continuation')
        if purpose == 'inspect':
            text = ('[Automatic waiting-state inspection] The evidence for this unfinished task is due for refresh. '
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
        links = store.q('SELECT DISTINCT sender, receiver FROM messages WHERE task_id=?', (task_id,))
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
        active = any((not r.get('current_task') or r['current_task'] == task_id) and
                     (r.get('state') == 'working' or r.get('active_runs') or r.get('background_busy')) for r in executors)
        pending = store.q("SELECT id FROM messages WHERE task_id=? AND state!='handled' AND receiver IN "
                          "(SELECT id FROM agents WHERE role IN ('shub','old_one','ghroth')) AND sender NOT IN "
                          "(SELECT id FROM agents WHERE role='yog')", (task_id,))
        # A missing/offline/unknown runtime is not proof that restarting its workload is safe.
        unavailable = any(not r.get('online') or not r.get('state') or r.get('draining') for r in executors)
        revision = hashlib.sha256(json.dumps([task['state'], instruction and instruction['id'],
            [(m['id'], m['state']) for m in messages if t.role_of(m['sender']) != Role.YOG],
            [{k: r.get(k) for k in ('agent_id', 'online', 'state', 'current_task', 'last_task',
                                  'finished_at', 'active_runs', 'background_busy', 'draining')} for r in executors]],
            sort_keys=True).encode()).hexdigest()
        return {'task': task, 'instruction': instruction, 'messages': messages, 'executors': executors,
                'active': active, 'pending': bool(pending), 'unavailable': unavailable,
                'owner_blocked': owner_blocked, 'revision': revision,
                'continuation': (store.setting('continuations') or {}).get(task_id, {})}

    async def scan(self, yog_id):
        t = self.temple
        results = await t.patrol()
        rows = []
        for task in t.store.q("SELECT * FROM tasks WHERE state='open' ORDER BY updated_at"):
            row = await self.snapshot(task['id'], results)
            async with self.lock:
                if t.store.task(task['id'])['state'] != 'open':
                    continue
                records = t.store.setting('continuations') or {}
                rec = records.get(task['id'], {})
                if (rec.get('phase') == 'waiting' and
                        time.time() >= rec.get('check_at', rec.get('at', time.time()) + 900) and
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
                row['continuation'] = rec
            rows.append(row)
        return {'tasks': rows, 'agents': results}

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
                if rec.get('revision') == row['revision'] and rec.get('phase') == state:
                    return {'state': state}
                # Rewording an unchanged assessment does not rearm a reminder.
                if rec.get('revision') == row['revision'] and rec.get('phase') in ('reminded', 'escalated'):
                    return {'state': 'duplicate'}
                rec = {'phase': state, 'revision': row['revision'], 'evidence': p['evidence'], 'at': time.time(),
                       'instruction_id': (row['instruction'] or {}).get('id')}
                if state == 'waiting':
                    delay = p.get('check_after_seconds', 900)
                    if not isinstance(delay, (int, float)) or not 300 <= delay <= 3600:
                        raise ValueError('check_after_seconds must be between 300 and 3600')
                    rec['check_at'] = rec['at'] + delay
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
