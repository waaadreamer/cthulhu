"""Convert explicitly reviewed Codex history for the existing Feishu importer.

No live discovery, model calls or automatic content-safety claim. The caller
supplies the effective Codex home and a trusted, disabled binding configuration.
"""
import argparse
import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime
from pathlib import Path

from . import feishu_import
from .roles import Denied


def read_bytes(path):
    with Path(path).open('rb') as f:
        data = f.read(feishu_import.MAX_BYTES + 1)
    if len(data) > feishu_import.MAX_BYTES:
        raise ValueError('input exceeds size bound')
    return data


def read_json(path):
    return json.loads(read_bytes(path))


def confined(path, roots):
    path = Path(path).resolve(strict=True)
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise ValueError('source path outside allowed roots')
    return path


def selected_sources(catalog, home, scope):
    """Inspect catalog metadata first; query only selected IDs, never all DB rows."""
    if not isinstance(catalog, list):
        raise ValueError('catalog must be an array')
    selected, keys = set(), set()
    for entry in catalog:
        if entry.get('scopeId') != scope or entry.get('agentId') != 'codex':
            continue
        key = '\x1f'.join(entry[k] for k in
                         ('scopeId', 'agentId', 'cwdRealpath', 'policyFingerprint'))
        if entry['key'] != key or key in keys or not entry.get('threadId'):
            raise ValueError('invalid or duplicate catalog identity')
        keys.add(key)
        selected.add(entry['threadId'])
    if not selected:
        raise ValueError('no selected source threads')
    scopes = {tid: sorted({e['scopeId'] for e in catalog if e.get('threadId') == tid})
              for tid in selected}
    if any(value != [scope] for value in scopes.values()):
        raise ValueError('cross-scope thread must be held')
    home = Path(home).resolve(strict=True)
    roots = [home / 'sessions', home / 'archived_sessions']
    # Do not resolve an escaping root symlink into a newly trusted root.
    index = confined(home / 'state_5.sqlite', [home])
    result = {}
    with closing(sqlite3.connect(index.as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        for tid in sorted(selected):
            rows = db.execute('SELECT rollout_path FROM threads WHERE id=?', (tid,)).fetchall()
            if len(rows) != 1 or not Path(rows[0][0]).is_absolute():
                raise ValueError('missing or ambiguous selected thread path')
            result[tid] = confined(rows[0][0], roots)
    if len(set(result.values())) != len(result):
        raise ValueError('source file reused by different threads')
    return result, scopes


def event(record, kind):
    return record.get('type') == 'event_msg' and record.get('payload', {}).get('type') == kind


def message(record, role):
    """Only supported text-message provenance; raw text is never copied out."""
    p = record.get('payload', {})
    if record.get('type') == 'event_msg':
        return (p.get('type') == {'user': 'user_message', 'assistant': 'agent_message'}[role]
                and isinstance(p.get('message'), str)
                and not any(p.get(k) for k in ('images', 'local_images', 'attachments')))
    if record.get('type') != 'response_item' or p.get('type') != 'message' or p.get('role') != role:
        return False
    content = p.get('content')
    return (isinstance(content, list) and bool(content)
            and all(isinstance(c, dict) and c.get('type') in ('input_text', 'output_text')
                    and isinstance(c.get('text'), str) for c in content))


def convert(config, catalog, home, review):
    if (set(review) != {'version', 'reviewed', 'threads', 'turns'}
            or review['version'] != 1 or review['reviewed'] is not True):
        raise ValueError('explicit reviewed text artifact required')
    cfg = config['feishu']
    owner = cfg['owner']
    scope = owner['legacy_scope_id']
    bound_event = {k: v for k, v in (('app_id', cfg['app_id']),
                                    ('sender_id', owner['sender_id']), ('chat_id', owner['chat_id']))}
    if owner.get('topic_id'):
        bound_event['topic_id'] = owner['topic_id']
    if not isinstance(review['turns'], list) or not 0 < len(review['turns']) <= feishu_import.MAX_TURNS:
        raise ValueError('reviewed turn count outside bounds')
    pins = {}
    for pin in review['threads']:
        if set(pin) != {'thread_id', 'sha256'} or pin['thread_id'] in pins:
            raise ValueError('duplicate or invalid reviewed thread')
        pins[pin['thread_id']] = pin['sha256']
    # Use the actual importer for binding, disabled flags and provenance schema
    # before opening any history file or SQLite index.
    provenance = [{'thread_id': tid, 'sha256': digest, 'scope_ids': [scope]}
                  for tid, digest in sorted(pins.items())]
    bundle = {'version': 1, 'event': bound_event,
              'source': {'scope_id': scope, 'threads': provenance}, 'turns': []}
    if not pins:
        raise ValueError('reviewed source hashes required')
    probe = dict(bundle, turns=[{'source_id': 'validation-only', 'source_thread': next(iter(pins)),
                                'input': '', 'reply': '', 'created_at': 0, 'status': 'completed'}])
    feishu_import.validate(config, probe)
    paths, scopes = selected_sources(catalog, home, scope)
    if set(paths) != set(pins):
        raise ValueError('reviewed threads differ from selected catalog threads')
    for thread in provenance:
        thread['scope_ids'] = scopes[thread['thread_id']]
    records = {}
    for tid, path in paths.items():
        raw = read_bytes(path)
        if hashlib.sha256(raw).hexdigest() != pins[tid]:
            raise ValueError('source changed after review')
        lines = [json.loads(line) for line in raw.splitlines()]
        if (not lines or lines[0].get('type') != 'session_meta'
                or lines[0].get('payload', {}).get('id') != tid):
            raise ValueError('source session identity mismatch')
        records[tid] = lines
    used = set()
    for turn in review['turns']:
        if set(turn) != {'source_thread', 'start_line', 'input_lines', 'reply_lines',
                         'finish_line', 'input', 'reply'}:
            raise ValueError('unexpected reviewed turn fields')
        tid = turn['source_thread']
        lines = records[tid]
        start, finish = turn['start_line'], turn['finish_line']
        if type(start) is not int or type(finish) is not int or not 1 < start < finish <= len(lines):
            raise ValueError('invalid turn boundary')
        first, last = lines[start - 1], lines[finish - 1]
        if not event(first, 'task_started'):
            raise ValueError('unsupported turn start')
        source_id = first['payload'].get('turn_id')
        status = next((s for name, s in (('task_complete', 'completed'), ('task_failed', 'failed'),
                                        ('turn_aborted', 'interrupted')) if event(last, name)), None)
        if (not isinstance(source_id, str) or not source_id or status is None
                or last['payload'].get('turn_id') != source_id or (tid, source_id) in used):
            raise ValueError('duplicate or unsupported finished turn')
        used.add((tid, source_id))
        if any(event(r, name) for r in lines[start:finish - 1]
               for name in ('task_started', 'task_complete', 'task_failed', 'turn_aborted')):
            raise ValueError('overlapping or ambiguous turn')
        for role, field in (('user', 'input_lines'), ('assistant', 'reply_lines')):
            refs = turn[field]
            if (not isinstance(refs, list) or (role == 'user' and not refs)
                    or any(type(n) is not int or not start < n < finish for n in refs)
                    or refs != sorted(set(refs))
                    or any(not message(lines[n - 1], role) for n in refs)):
                raise ValueError('unsupported message reference')
        if not turn['reply_lines'] and turn['reply'] != '':
            raise ValueError('reply text without assistant provenance')
        if status == 'completed' and not turn['reply_lines']:
            raise ValueError('completed turn requires reviewed assistant message')
        stamp = datetime.fromisoformat(last['timestamp'].replace('Z', '+00:00'))
        if stamp.tzinfo is None:
            raise ValueError('source timestamp requires timezone')
        bundle['turns'].append({'source_id': source_id, 'source_thread': tid,
                                'input': turn['input'], 'reply': turn['reply'],
                                'created_at': stamp.timestamp(), 'status': status})
    bundle['turns'].sort(key=lambda r: (r['created_at'], r['source_thread'], r['source_id']))
    feishu_import.validate(config, bundle)
    return bundle


def write_bundle(path, bundle):
    data = json.dumps(bundle, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    if len(data) > feishu_import.MAX_BYTES:
        raise ValueError('output exceeds size bound')
    path = Path(path)
    if path.parent.stat().st_mode & 0o077:
        raise ValueError('output directory must be private')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        path.unlink()
        raise
    return hashlib.sha256(data).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    for name in ('config', 'catalog', 'codex-home', 'reviewed', 'output'):
        p.add_argument('--' + name, type=Path, required=True)
    args = p.parse_args()
    try:
        bundle = convert(read_json(args.config), read_json(args.catalog), args.codex_home,
                         read_json(args.reviewed))
        digest = write_bundle(args.output, bundle)
        print(json.dumps({'written': True, 'turns': len(bundle['turns']), 'sha256': digest}))
    except (Denied, ValueError, KeyError, TypeError, OSError, sqlite3.Error, AttributeError):
        p.exit(1, 'History conversion rejected; check binding, source mapping and reviewed artifact.\n')


if __name__ == '__main__':
    main()
