"""Validate/apply a manager-authored {task_id: short_name} JSON mapping offline.

Run against a backed-up/stopped temple DB during an approved release. Without
--apply it opens SQLite read-only and reports missing names; no model/API calls.
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cthulhu.store import Store
from cthulhu.task_display import short_name


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', required=True)
    parser.add_argument('--mapping', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    mapping = json.loads(Path(args.mapping).read_text())
    if not isinstance(mapping, dict):
        raise ValueError('mapping must be an object')
    for name in mapping.values():
        if not isinstance(name, str) or not name.strip():
            raise ValueError('empty or invalid name')
        short_name('', name)
    # Read-only preflight also works on pre-migration schemas.
    db = sqlite3.connect(Path(args.db).resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    rows = [dict(row) for row in db.execute('SELECT * FROM task_index')]
    db.close()
    known = {row['id'] for row in rows}
    if unknown := set(mapping) - known:
        raise ValueError('unknown tasks: ' + ', '.join(sorted(unknown)))
    for row in rows:
        if not row.get('short_name_pending', 1) and row['id'] in mapping and row['short_name'] != mapping[row['id']]:
            raise ValueError('stable task name cannot be changed: ' + row['id'])
    missing = [row['id'] for row in rows if row.get('short_name_pending', 1) and row['id'] not in mapping]
    print(json.dumps({'tasks': len(rows), 'mapped': len(mapping), 'missing': missing}, ensure_ascii=False))
    if args.apply:
        if missing:
            raise ValueError('mapping must cover all pending task names before apply')
        store = Store(Path(args.db))
        try:
            print(json.dumps({'named': store.name_tasks(mapping)}, ensure_ascii=False))
        finally:
            store.db.close()

if __name__ == '__main__':
    main()
