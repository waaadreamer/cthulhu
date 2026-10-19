"""lark-cli 协议夹具：只模拟 `event consume` 的 NDJSON 输出与 stdin 生命周期，不代表真实飞书。

FAKE_LARK_EVENTS 每行一个事件；每次启动都从头重放，用来模拟平台重投。
FAKE_LARK_LOG 记录每次启动的参数与凭据目录。FAKE_LARK_EXIT=1 时输出完立即退出（模拟断线）。
"""
import json
import os
import sys

with open(os.environ['FAKE_LARK_LOG'], 'a') as log:
    log.write(json.dumps({'argv': sys.argv[1:], 'config_dir': os.environ.get('LARKSUITE_CLI_CONFIG_DIR')}) + '\n')
with open(os.environ['FAKE_LARK_EVENTS']) as f:
    for line in f:
        sys.stdout.write(line)
sys.stdout.flush()
if os.environ.get('FAKE_LARK_EXIT') == '1':
    sys.exit(3)
# 与真实 lark-cli 一致：无界订阅在 stdin EOF 时正常退出。
sys.stdin.read()
