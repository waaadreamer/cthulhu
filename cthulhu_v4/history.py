"""Master 消息原文导出：按天写成可 grep 的文本，供 Master 回查“以前说过/做过什么”。

只导出真实消息原文（主人、Master、执行者、飞书之间），不写摘要、不调用模型：
原生会话文件里工具输出占绝大部分，直接 grep 会被淹没；这里是同一历史的干净索引，
细节仍以中心事件（#seq，可用 read_task_events 展开）和原生会话为准。
"""
import os
import re
from datetime import datetime
from pathlib import Path

# 系统模板消息每次内容相同、不含信息，留着只会淹没 grep 结果。
NOISE = tuple(re.compile(p) for p in (
    r'^十分钟增量审核', r'^等待到期，请检查', r'^任务尚未结束，但当前没有活动回合',
    r'^任务 t_\w+ 等待到期，已唤醒核验', r'^任务 t_\w+ 非预期空闲'))
QUERY = '''SELECT seq, at, kind, actor, target, task, text FROM events
    WHERE seq > ? AND seq <= ? AND (kind IN ('chat', 'feishu_out')
          OR (kind = 'message' AND (actor = 'master' OR target = 'master')))
    ORDER BY seq'''
WINDOW = 20000   # 每次最多扫描的事件序号跨度：首次补齐上百万条历史时不能长时间占住事件循环
HEAD = re.compile(r'^\[\d\d:\d\d:\d\d\] #(\d+) ', re.M)


def entry(seq, at, kind, actor, target, task, text):
    if kind == 'message' and actor == 'system' and any(p.match(text) for p in NOISE):
        return None
    if kind == 'chat':
        target = 'owner'          # chat 是 Master 写进主聊天的话（tell_owner 与最终回复）
    elif kind == 'feishu_out':
        target = 'feishu'
    t = datetime.fromtimestamp(at).astimezone()
    head = f"[{t:%H:%M:%S}] #{seq} {actor} → {target}" + (f" ({task})" if task else '')
    return t.strftime('%Y-%m-%d'), head + '\n' + text.strip() + '\n\n'


def rows(store, after, upto):
    """(seq, at, day, 文本) 序列，按 seq 升序；过滤规则与日文件一致，记忆提炼也用它取原文。"""
    for row in store.db.execute(QUERY, (after, upto)):
        e = entry(*row)
        if e:
            yield row[0], row[1], e[0], e[1]


class History:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root)
        self.cursor = None

    def load_cursor(self):
        """取游标文件与最新日文件末尾 #seq 的较大值。

        追加文件与写游标不是同一事务：若在两者之间崩溃，游标落后于文件，
        只看游标会把最后一批重复追加；文件里已有的 seq 才是真正写过的位置。
        """
        self.root.mkdir(parents=True, exist_ok=True)
        cur = self.root / '.cursor'
        seen = int(cur.read_text() or 0) if cur.exists() else 0
        days = sorted(self.root.glob('????-??-??.md'))
        if days:
            with open(days[-1], 'rb') as f:
                f.seek(max(0, f.seek(0, os.SEEK_END) - 65536))
                tail = f.read().decode('utf-8', 'ignore')
            seen = max([seen, *map(int, HEAD.findall(tail))])
        self.cursor = seen

    def sync(self, windows=5):
        """把游标之后的新消息追加到对应日期文件；返回追加条数。"""
        if self.cursor is None:
            self.load_cursor()
        top, added = self.store.highwater(), 0
        for _ in range(windows):
            if self.cursor >= top:
                break
            end = min(top, self.cursor + WINDOW)
            out = {}
            for _, _, day, text in rows(self.store, self.cursor, end):
                out.setdefault(day, []).append(text)
            for day, items in out.items():
                path = self.root / f'{day}.md'
                with open(path, 'a', encoding='utf-8') as f:
                    if f.tell() == 0:
                        f.write(f'# Master 消息 {day}\n\n')
                    f.write(''.join(items))
                added += len(items)
            # 文件已写完才推进游标；先写临时文件再改名，崩溃时不会留下半截游标。
            tmp = self.root / '.cursor.tmp'
            tmp.write_text(str(end)); tmp.replace(self.root / '.cursor')
            self.cursor = end
        return added
