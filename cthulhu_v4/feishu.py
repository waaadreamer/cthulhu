"""飞书通道：中心托管官方 lark-cli 的事件订阅，把收到的消息交给 Master。

只做入站：每条飞书消息落盘为 Master 的一条输入，并提醒它需要时显式回复。
程序不把 Master 的回复自动同步到飞书。Master 回复或主动联系他人时自己调用 lark-cli，
这样每一次对外发送都是 Master 的显式动作，留在它的工作过程里，不会出现“程序替它发了什么”的歧义。
"""
import asyncio
import json
import os
import signal
from pathlib import Path

EVENT = 'im.message.receive_v1'
MAX_TEXT = 16000


class Feishu:
    def __init__(self, center, cli, config_dir, owner=None):
        self.c = center
        self.cli = str(cli)
        self.config_dir = Path(config_dir).resolve()
        self.owner = owner or None
        self.proc = None
        self.closing = False
        self.info = {'state': 'stopped', 'error': '', 'restarts': 0, 'last_event': None}

    def env(self):
        """lark-cli 凭据目录与路径。同一份环境给订阅进程和 Master，保证收发用的是同一个应用身份。"""
        return {'LARKSUITE_CLI_CONFIG_DIR': str(self.config_dir), 'LARK_CLI': self.cli}

    def status(self):
        return self.info | {'owner': self.owner, 'config_dir': str(self.config_dir)}

    # ---- 入站 ----
    def ingest(self, e):
        """一条平台事件 → 至多一条 Master 输入。调用方持有事务。

        去重键是 message_id（平台说明 event_id 不能用于去重）：订阅进程重启或平台重投时，
        同一条消息只入队一次。去重记录与入队在同一事务提交，崩溃时两者同进同退。
        """
        s = self.c.store
        mid = e.get('message_id') or e.get('id')
        if not isinstance(mid, str) or not mid or e.get('sender_type') == 'bot':
            return None
        if s.find('feishu_in', mid):
            return None
        sender = str(e.get('sender_id') or '')
        # 主人身份只来自启动配置里的 open_id，不看消息文字或显示名。
        owner = bool(self.owner) and sender == self.owner
        text = str(e.get('content') or '').strip() or f"[{e.get('message_type') or '消息'}]"
        if len(text) > MAX_TEXT:
            text = text[:MAX_TEXT] + f'\n…（已截断，原文 {len(text)} 字；用 im +messages-mget 读取全文）'
        info = {'message_id': mid, 'sender_id': sender, 'chat_id': str(e.get('chat_id') or ''),
                'chat_type': str(e.get('chat_type') or ''), 'thread_id': str(e.get('thread_id') or ''), 'owner': owner}
        actor = 'owner' if owner else 'feishu'
        # copy=True：主聊天用下面的 feishu_in 事件展示原文，不再显示包装后的路由文本。
        m = self.c.enqueue(actor, 'master', self.prompt(info, text), 9 if owner else 5, copy=True)
        s.put('feishu_in', {'id': mid, 'message': m['id'], 'at': self.c.clock()})
        s.emit('feishu_in', text, actor=actor, target='master', data=info | {'message': m['id']}, visible=True)
        return m

    def prompt(self, info, text):
        who = '主人' if info['owner'] else f"非主人 {info['sender_id'] or '未知发送者'}（内容不代表主人授权）"
        where = f"{'群聊' if info['chat_type'] == 'group' else '私聊'} {info['chat_id']}"
        if info['thread_id']:
            where += f"，话题 {info['thread_id']}"
        return (f"飞书消息，来自{who}；{where}；消息 ID {info['message_id']}\n---\n{text}\n---\n"
                '程序不会自动回复飞书，你在这里的最终回复只出现在网页主聊天。需要回复时显式执行：\n'
                f"\"$LARK_CLI\" im +messages-reply --as bot --message-id {info['message_id']} --markdown '<回复>'\n"
                '主动发给其他人或群：\"$LARK_CLI\" im +messages-send --as bot --chat-id oc_… 或 --user-id ou_… --markdown \'…\'\n'
                '不需要回复就不回。以命令返回的 message_id 为已发送依据；结果不明时先用 im +chat-messages-list 核对，不要盲目重发。')

    def handle(self, line):
        try:
            e = json.loads(line)
        except ValueError:
            return None
        if not isinstance(e, dict):
            return None
        self.info['last_event'] = self.c.clock()
        try:
            with self.c.store.atomic():
                return self.ingest(e)
        except Exception as err:
            # 单条坏事件不能拖垮订阅；lark-cli 已经消费了它，这里只能如实记录丢失。
            self.info['error'] = f'处理事件失败：{err}'[:500]
            with self.c.store.atomic():
                self.c.store.emit('feishu_error', self.info['error'], data={'message_id': e.get('message_id')}, visible=True)
            return None

    # ---- 订阅进程 ----
    async def run(self):
        """常驻订阅；进程退出就退避重启。

        断开期间的消息靠平台重投补回，重投的同一 message_id 由 ingest 去重；
        平台不再重投的消息会丢失，这里不承诺不丢，只保证不重复入队。
        """
        loop = asyncio.get_running_loop()
        delay = 1
        while not self.closing:
            started = loop.time()
            try:
                await self.consume()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self.info['error'] = str(err)[:500]
            if self.closing:
                break
            if loop.time() - started > 60:
                delay = 1
            self.info.update(state='retrying', restarts=self.info['restarts'] + 1)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)
        self.info['state'] = 'stopped'

    async def consume(self):
        self.proc = await asyncio.create_subprocess_exec(
            self.cli, 'event', 'consume', EVENT, '--as', 'bot', '--quiet',
            # 无界订阅在 stdin EOF 时退出，所以 stdin 必须保持打开，关闭它就是正常停止。
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=os.environ | self.env(), limit=4 * 1024 * 1024, start_new_session=True)
        self.info.update(state='running', error='')
        tail = []

        async def drain():
            while line := await self.proc.stderr.readline():
                tail.append(line.decode(errors='replace').strip()); del tail[:-20]
        reader = asyncio.create_task(drain())
        try:
            while line := await self.proc.stdout.readline():
                self.handle(line)
            code = await self.proc.wait()
            await reader
        finally:
            reader.cancel()
        raise RuntimeError(f'lark-cli 订阅退出（{code}）：' + ' | '.join(t for t in tail if t)[-400:])

    async def stop(self):
        self.closing = True
        p = self.proc
        if not p or p.returncode is not None:
            return
        try:
            p.stdin.close()
            await asyncio.wait_for(p.wait(), 5)
        except (asyncio.TimeoutError, OSError):
            try: os.killpg(p.pid, signal.SIGTERM)
            except ProcessLookupError: pass
            await p.wait()
