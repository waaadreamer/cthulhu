"""小规模单机持久化：状态与对外可见事件在同一事务提交。"""
import json
import sqlite3
import time
from contextlib import contextmanager


# 界面会渲染的原生事件；其余原生事件是遥测。
UI_NATIVE = ('native:item/started', 'native:item/completed', 'native:item/agentMessage/delta',
             'native:item/reasoning/summaryTextDelta', 'native:item/reasoning/summaryPartAdded',
             'native:item/reasoning/textDelta', 'native:item/commandExecution/outputDelta',
             'native:turn/completed', 'native:turn/plan/updated', 'native:stderr', 'native:error',
             'native:declined_request', 'native:demo', 'native:stream_event', 'native:assistant',
             'native:user', 'native:result')
DELTAS = ('native:item/agentMessage/delta', 'native:item/reasoning/summaryTextDelta',
          'native:item/reasoning/summaryPartAdded', 'native:item/reasoning/textDelta',
          'native:item/commandExecution/outputDelta', 'native:item/fileChange/outputDelta', 'native:stream_event')


class Store:
    def __init__(self, path):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS objects (
                kind TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
                PRIMARY KEY(kind, id));
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                actor TEXT NOT NULL, target TEXT, task TEXT, kind TEXT NOT NULL,
                text TEXT NOT NULL, data TEXT NOT NULL, visible INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS events_task ON events(task, seq);
        ''')

    @contextmanager
    def atomic(self):
        # Core 中的事务只允许同步逻辑，绝不能跨 await 持有事务。
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def put(self, kind, obj):
        # 原地更新而不是 REPLACE：REPLACE 会删行重插、换掉 rowid，按 rowid 排序的列表会随心跳乱跳。
        self.db.execute('INSERT INTO objects VALUES (?, ?, ?) ON CONFLICT(kind, id) DO UPDATE SET data=excluded.data',
                        (kind, obj['id'], json.dumps(obj, ensure_ascii=False)))
        return obj

    def get(self, kind, ident):
        row = self.db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, ident)).fetchone()
        if not row:
            raise ValueError(f'{kind} 不存在: {ident}')
        return json.loads(row[0])

    def all(self, kind):
        return [json.loads(r[0]) for r in self.db.execute('SELECT data FROM objects WHERE kind=? ORDER BY rowid', (kind,))]

    def emit(self, kind, text='', *, actor='system', target=None, task=None, data=None, visible=False):
        cur = self.db.execute('INSERT INTO events(at,actor,target,task,kind,text,data,visible) VALUES(?,?,?,?,?,?,?,?)',
                             (time.time(), actor, target, task, kind, text,
                              json.dumps(data or {}, ensure_ascii=False), int(visible)))
        return cur.lastrowid

    def events(self, after=0, limit=200, task=None, agent=None, visible=False, conversation=False,
               before=None, ui=False, compact=False, harness=True):
        """按 seq 读取事件。

        before：向前翻页（返回 seq < before 的最新 limit 条，仍按升序）；before<=0 表示从最新开始。
        ui：只返回界面会展示的事件。遥测（token 用量、限速、握手、hook 生命周期）和包装后的路由输入
            体积大却从不展示，不必传给浏览器。工具 read_task_events 不用这个过滤，保持原始全集。
        compact：去掉流式增量。历史回合的完整文字/输出都在 item/completed 与 assistant 事件里。
        harness=False：去掉 harness_tool（工具调用的中心侧记录）。真实后端的工具调用已在原生事件里展示，
            网页只在 demo 后端需要它；结果正文往往很大。
        """
        clauses, args = [], []
        if before is None:
            clauses.append('seq>?'); args.append(int(after))
        elif int(before) > 0:
            clauses.append('seq<?'); args.append(int(before))
        if task:
            clauses.append('task=?'); args.append(task)
        if agent:
            clauses.append("(actor=? OR target=? OR task IN (SELECT id FROM objects WHERE kind='task' AND json_extract(data,'$.agent')=?))"); args.extend([agent, agent, agent])
        if visible:
            clauses.append('visible=1')
        if conversation:
            # 主聊天在同一序列中合并公开汇报与 Master 原生活动；不混入执行者原生流。
            clauses.append("(visible=1 OR actor='master' OR target='master')")
        if ui:
            clauses.append(f"(kind NOT LIKE 'native:%' OR kind IN ({','.join('?' * len(UI_NATIVE))}))"); args.extend(UI_NATIVE)
            clauses.append("NOT (kind IN ('native:item/started','native:item/completed') AND json_extract(data,'$.item.type')='userMessage')")
        if not harness:
            clauses.append("kind != 'harness_tool'")
        if compact:
            clauses.append(f"kind NOT IN ({','.join('?' * len(DELTAS))})"); args.extend(DELTAS)
        args.append(max(1, min(int(limit), 500)))
        where = ' AND '.join(clauses) or '1'
        order = 'DESC' if before is not None else 'ASC'
        rows = list(self.db.execute(f'SELECT * FROM events WHERE {where} ORDER BY seq {order} LIMIT ?', args))
        if before is not None:
            rows.reverse()
        return [dict(r) | {'data': json.loads(r['data'])} for r in rows]

    def find(self, kind, ident):
        row = self.db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, ident)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, kind, ident):
        self.db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, ident))

    def close(self):
        self.db.close()

    def highwater(self):
        return self.db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]

    def review_highwater(self, after=0):
        """只统计新信息；审核自身的输出和运行时遥测不能成为下一轮审核的原因。"""
        return self.db.execute('''
            SELECT COALESCE(MAX(seq), ?) FROM events WHERE seq > ? AND (
                actor = 'owner'
                OR kind IN ('task_created', 'control', 'deadline', 'completed',
                    'question', 'answer', 'ask_owner', 'question_timeout', 'question_delegated', 'overdue',
                    'review_requested', 'review_returned', 'task_failed', 'task_reopened',
                    'recovery', 'recovery_exhausted', 'node_attention', 'runtime_error',
                    'orphan_process', 'interrupted', 'instruction', 'turn_failed', 'agent_config')
                OR (actor NOT IN ('master', 'system', 'owner') AND (
                    kind IN ('message', 'reply', 'remote_result', 'native_request')
                    OR (kind = 'harness_tool' AND text IN ('wait_until', 'wait_for_reply'))
                    OR kind LIKE 'native:item/%'
                    OR kind LIKE 'native:hook/%'
                    OR kind IN ('native:turn/completed', 'native:stderr', 'native:runtime_config',
                        'native:assistant', 'native:user', 'native:stream_event', 'native:result',
                        'native:demo')
                ))
            )''', (int(after), int(after))).fetchone()[0]
