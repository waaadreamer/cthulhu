"""Local, pre-activation single-scope history import into the existing turn log.

No model threads, catalog entries, channel events or queued messages are forged.
The first admitted event hands off this history and updates the normal catalog.
"""
import argparse
import hashlib
import json
import math
import re
from pathlib import Path

from .feishu import owner_scope
from .roles import Denied
from .store import Store

MAX_BYTES = 32 * 1024 * 1024
MAX_TURNS = 10000


def validate(config, bundle):
    cfg = config.get('feishu') or {}
    if cfg.get('enabled') or (cfg.get('worker') or {}).get('enabled'):
        raise ValueError('import requires disabled Feishu configuration')
    if bundle.get('version') != 1:
        raise ValueError('unsupported history version')
    if set(bundle) != {'version', 'event', 'source', 'turns'}:
        raise ValueError('unexpected bundle fields')
    event = bundle['event']
    scope = owner_scope(cfg, event)
    if (event.get('topic_id') or '') != ((cfg.get('owner') or {}).get('topic_id') or ''):
        raise ValueError('history topic differs from trusted binding')
    source = bundle['source']
    if set(source) != {'scope_id', 'threads'}:
        raise ValueError('unexpected source fields')
    old_scope = (cfg.get('owner') or {}).get('legacy_scope_id')
    if not old_scope or source['scope_id'] != old_scope:
        raise ValueError('legacy scope requires trusted binding mapping')
    threads = source['threads']
    if not isinstance(threads, list) or not threads:
        raise ValueError('selected thread provenance required')
    thread_ids = set()
    for thread in threads:
        if set(thread) != {'thread_id', 'sha256', 'scope_ids'}:
            raise ValueError('unexpected thread provenance fields')
        if not isinstance(thread['thread_id'], str) or not thread['thread_id'] or thread['thread_id'] in thread_ids:
            raise ValueError('unique source thread IDs required')
        thread_ids.add(thread['thread_id'])
        if thread['scope_ids'] != [old_scope]:
            raise ValueError('shared or unverified source threads must be held')
        if not isinstance(thread['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', thread['sha256']):
            raise ValueError('selected source hash required')
    turns = bundle['turns']
    if not isinstance(turns, list) or not 0 < len(turns) <= MAX_TURNS:
        raise ValueError('history turn count outside import bounds')
    seen, previous = set(), -1
    for row in turns:
        if set(row) != {'source_id', 'source_thread', 'input', 'reply', 'created_at', 'status'}:
            raise ValueError('unexpected history fields')
        if any(not isinstance(row[k], str) for k in ('source_id', 'input', 'reply')):
            raise ValueError('history text fields required')
        if row['source_thread'] not in thread_ids:
            raise ValueError('unknown source thread')
        identity = (row['source_thread'], row['source_id'])
        if not row['source_id'] or identity in seen:
            raise ValueError('unique source IDs required')
        seen.add(identity)
        if row['status'] not in ('completed', 'failed', 'interrupted'):
            raise ValueError('only finished historical turns may be imported')
        stamp = row['created_at']
        if type(stamp) not in (int, float) or not math.isfinite(stamp) or stamp < 0 or stamp < previous:
            raise ValueError('history must be in timestamp order')
        previous = stamp
    encoded = json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > MAX_BYTES:
        raise ValueError('history bundle exceeds import size bound')
    digest = hashlib.sha256(encoded).hexdigest()
    return scope, turns, digest


def import_history(store, config, bundle):
    scope, turns, digest = validate(config, bundle)
    key = 'feishu_history:' + hashlib.sha256(scope.encode()).hexdigest()
    with store.lock:
        db = store.db
        db.execute('BEGIN IMMEDIATE')
        try:
            prior = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            if prior:
                if json.loads(prior['value'])['sha256'] != digest:
                    raise ValueError('different history already imported for this scope')
                db.execute('COMMIT')
                return {'imported': 0, 'duplicate': True}
            if db.execute('SELECT 1 FROM turn_log WHERE agent_id=? AND scope=? LIMIT 1',
                          ('nyarlathotep', scope)).fetchone() or db.execute(
                    'SELECT 1 FROM channel_events WHERE agent_id=? AND scope=? LIMIT 1',
                    ('nyarlathotep', scope)).fetchone():
                raise ValueError('scope already in use; import before activation')
            mapping = []
            for r in turns:
                cur = db.execute('INSERT INTO turn_log '
                           '(agent_id,backend,model,thread,input,reply,status,created_at,scope) '
                           'VALUES (?,?,?,?,?,?,?,?,?)',
                    ('nyarlathotep', 'imported', None, 'imported:' + r['source_thread'],
                     r['input'], r['reply'], r['status'], r['created_at'], scope))
                mapping.append({'source_thread': r['source_thread'], 'source_id': r['source_id'],
                                'turn_log_id': cur.lastrowid})
            receipt = {'sha256': digest, 'source': bundle['source'], 'mapping': mapping}
            db.execute('INSERT INTO settings(key,value) VALUES (?,?)', (key, json.dumps(receipt)))
            db.execute('COMMIT')
            return {'imported': len(turns), 'duplicate': False}
        except BaseException:
            db.execute('ROLLBACK')
            raise


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True, help='trusted disabled local config')
    p.add_argument('--bundle', type=Path, required=True)
    p.add_argument('--sha256', required=True, help='expected private transfer artifact SHA256')
    p.add_argument('--db', type=Path, help='existing temple DB; required only with --apply')
    p.add_argument('--apply', action='store_true', help='default only validates; no DB access')
    args = p.parse_args()
    try:
        if args.bundle.stat().st_size > MAX_BYTES:
            raise ValueError('history bundle exceeds import size bound')
        data = args.bundle.read_bytes()
        if hashlib.sha256(data).hexdigest() != args.sha256:
            raise ValueError('private artifact hash mismatch')
        config, bundle = json.loads(args.config.read_text()), json.loads(data)
        scope, turns, digest = validate(config, bundle)
        if not args.apply:
            print(json.dumps({'valid': True, 'turns': len(turns), 'sha256': digest}))
            return
        if not args.db or not args.db.is_file():
            raise ValueError('existing temple database required')
        store = Store(args.db)
        try:
            print(json.dumps(import_history(store, config, bundle)))
        finally:
            store.db.close()
    except (Denied, ValueError, KeyError, TypeError, OSError):
        # Do not echo malformed private input or exception payloads.
        p.exit(1, 'History import rejected; verify binding, disabled configuration and bundle schema.\n')


if __name__ == '__main__':
    main()
