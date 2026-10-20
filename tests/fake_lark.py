"""lark-cli 协议夹具：只模拟本项目用到的命令输出与 stdin 生命周期，不代表真实飞书。

FAKE_LARK_EVENTS  `event consume` 输出的 NDJSON；每次启动都从头重放，用来模拟平台重投。
FAKE_LARK_LOG     每次调用记录一行：参数与凭据目录。
FAKE_LARK_EXIT=1  `event consume` 输出完立即退出（模拟断线）。
FAKE_LARK_NAMES   JSON {open_id: 名字}，`im +messages-mget` 据此返回发送者名字。
FAKE_LARK_FAIL=1  发送/回复类命令失败。
"""
import json
import os
import sys

argv = sys.argv[1:]
with open(os.environ['FAKE_LARK_LOG'], 'a') as log:
    log.write(json.dumps({'argv': argv, 'config_dir': os.environ.get('LARKSUITE_CLI_CONFIG_DIR')}, ensure_ascii=False) + '\n')


def flag(name):
    return argv[argv.index(name) + 1] if name in argv else None


def out(data):
    print(json.dumps({'ok': True, 'identity': 'bot', 'data': data}, ensure_ascii=False))


if argv[:2] == ['event', 'consume']:
    with open(os.environ['FAKE_LARK_EVENTS']) as f:
        for line in f:
            sys.stdout.write(line)
    sys.stdout.flush()
    if os.environ.get('FAKE_LARK_EXIT') == '1':
        sys.exit(3)
    # 与真实 lark-cli 一致：无界订阅在 stdin EOF 时正常退出。
    sys.stdin.read()
elif argv[:2] == ['im', '+messages-mget']:
    names = json.loads(os.environ.get('FAKE_LARK_NAMES') or '{}')
    events = [json.loads(l) for l in open(os.environ['FAKE_LARK_EVENTS']) if l.startswith('{')]
    e = next((e for e in events if e.get('message_id') == flag('--message-ids')), None)
    sender = {'id': e['sender_id'], 'name': names.get(e['sender_id'], e['sender_id'])} if e else {}
    out({'messages': [{'message_id': flag('--message-ids'), 'sender': sender}] if e else []})
elif argv[:2] == ['api', 'GET'] and argv[2].startswith('/open-apis/im/v1/chats/'):
    out({'name': '测试群'})
elif argv[:2] in (['im', '+messages-reply'], ['im', '+messages-send']):
    if os.environ.get('FAKE_LARK_FAIL') == '1':
        print(json.dumps({'ok': False, 'error': {'message': 'fixture failure'}}))
        sys.exit(1)
    out({'message_id': 'om_sent_' + str(abs(hash(tuple(argv))) % 10000)})
else:
    out({'argv': argv})
