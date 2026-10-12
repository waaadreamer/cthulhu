"""Feishu provenance and durable correlation, using the temple's existing Store.

Only the lifecycle-owned Nyarl worker can call this API. Platform identity comes
from the authenticated bridge event, never from text or the model's tool args.
"""
import json

from .roles import Denied, Role


def binding(temple, agent_id, event):
    agent = temple.store.agent(agent_id)
    cfg = temple.config.get('feishu') or {}
    if not agent or agent['role'] != Role.NYARLATHOTEP or not cfg.get('enabled'):
        raise Denied('Feishu ingress is not enabled for this identity')
    return owner_scope(cfg, event)


def owner_scope(cfg, event):
    """Resolve provenance from trusted local binding, also before activation."""
    # Provisioning must record trusted owner verification, not an allowlist hit.
    owner = cfg.get('owner') or {}
    if not cfg.get('app_id') or not owner.get('verified_by') or not owner.get('sender_id') or not owner.get('chat_id'):
        raise Denied('verified owner binding required')
    if (event.get('app_id') != cfg.get('app_id') or
            event.get('sender_id') != owner['sender_id'] or
            event.get('chat_id') != owner['chat_id']):
        raise Denied('external sender or chat is not delegated')
    chat = event['chat_id']
    scope_id = chat + (':' + event['topic_id'] if event.get('topic_id') else '')
    if event.get('scope_id', scope_id) != scope_id:
        raise Denied('scope does not match authenticated event')
    return f"feishu:{cfg['app_id']}:{owner['sender_id']}:{scope_id}"


def check_scope(temple, agent_id, scope):
    if not scope:
        return ''
    if not temple.store.one('SELECT message_id FROM channel_events WHERE agent_id=? AND scope=?',
                            (agent_id, scope)):
        raise Denied('unknown channel scope')
    return scope


async def channel_call(temple, conn, method, p):
    if temple.role_of(conn.id) != Role.NYARLATHOTEP:
        raise Denied('channel operation requires Nyarl identity')
    if method == 'channel_authorize':
        return {'scope': binding(temple, conn.id, p)}
    if method == 'channel_ingress':
        scope = binding(temple, conn.id, p)
        if not all(isinstance(p.get(k), str) and p[k] for k in ('event_id', 'prompt', 'text')):
            raise ValueError('event_id, prompt and text required')
        row, created = temple.store.channel_accept(conn.id, p['app_id'], p['event_id'], scope, p)
        turn = temple.store.turn(row['message_id'])
        if created:
            await temple.broadcast({'type': 'chat', 'turn': turn})
        # The worker executes via explicit Agent.run; it does not create another
        # agent or route a second copy through the model's main-thread queue.
        return {'message_id': row['message_id'], 'scope': scope, 'created': created,
                'turn': turn, 'receipt': json.loads(row['receipt'])}
    row = temple.store.one('SELECT * FROM channel_events WHERE agent_id=? AND message_id=?',
                           (conn.id, p.get('message_id')))
    if not row:
        raise Denied('channel event not owned by this agent')
    if method == 'channel_receipt':
        receipt = p.get('receipt') or {}
        if receipt.get('state') not in ('sending', 'sent', 'ambiguous', 'failed'):
            raise ValueError('invalid receipt state')
        if receipt['state'] == 'sent' and not (isinstance(receipt.get('message_id'), str) and receipt['message_id'].strip()):
            raise ValueError('sent requires a platform message receipt')
        # Persist only platform IDs/status, never CLI credentials or raw errors.
        receipt = {k: receipt[k] for k in ('state', 'message_id', 'request_id') if k in receipt}
        old = json.loads(row['receipt'])
        if old.get('state') == 'sent' and receipt != old:
            raise Denied('confirmed receipt cannot be overwritten')
        temple.store.x('UPDATE channel_events SET receipt=? WHERE message_id=?',
                       (json.dumps(receipt), row['message_id']))
        await temple.broadcast({'type': 'chat', 'turn': temple.store.turn(row['message_id'])})
        return receipt
    raise Denied('unknown channel operation')
