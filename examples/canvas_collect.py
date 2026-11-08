#!/usr/bin/env python3
"""只读取清单明确列出的 JSON 状态文件；不扫描实验目录，不推测训练进度。

用法：python3 canvas_collect.py /absolute/path/manifest.json
清单：{"experiments": [{"name": "测试 A", "path": "/path/to/status.json"}, ...]}
状态文件：{"status": "running", "metrics": {"completed": 3, "total": 10}}
"""
import json
import sys
import time
from pathlib import Path


def collect(manifest):
    if manifest.stat().st_size > 256 * 1024:
        raise ValueError('清单超过 256KiB')
    specs = json.loads(manifest.read_text())['experiments']
    if not isinstance(specs, list) or len(specs) > 100:
        raise ValueError('清单必须是至多 100 项的 experiments 列表')
    results = []
    for spec in specs:
        path = Path(spec['path'])
        if not path.is_absolute():
            path = manifest.parent / path
        result = {'name': spec.get('name', path.name), 'path': str(path)}
        try:
            if path.stat().st_size > 256 * 1024:
                raise ValueError('单个状态文件超过 256KiB')
            record = json.loads(path.read_text())
            if not isinstance(record, dict):
                raise ValueError('状态文件必须是 JSON 对象')
            result.update(status=record.get('status', '未提供状态'), metrics=record.get('metrics', {}),
                          file_modified_at=path.stat().st_mtime)
        except (OSError, ValueError) as exc:
            result['error'] = str(exc)
        results.append(result)
    return {'demo': False, 'source': str(manifest), 'observed_at': time.time(), 'experiments': results}


if __name__ == '__main__':
    print(json.dumps(collect(Path(sys.argv[1]).resolve()), ensure_ascii=False))
