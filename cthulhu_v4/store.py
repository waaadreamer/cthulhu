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
        self.path = str(path)
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
            -- 租约与消息只增不删，热路径只关心少数 running/queued 行；按状态建表达式索引，
            -- 否则每次节点同步、网页轮询都要反序列化全部历史，事件循环被拖住、节点租约过期。
            CREATE INDEX IF NOT EXISTS objects_state ON objects(kind, json_extract(data, '$.state'));
            -- 按执行者读事件（网页点开某个执行者的历史）要匹配 actor、target 或其任务；没有这两个索引时
            -- 只能倒序扫整张 events 表（线上 14GB、280 万行，冷门执行者实测 3~6 秒）。带 seq 的复合索引
            -- 还让“跟随最新”(seq>?) 的每秒轮询只读新增的行，而不是这个执行者的全部历史。
            -- 线上大库须停中心后离线建好同名索引：这里的 IF NOT EXISTS 此时是空操作；
            -- 若在大库上靠启动时建，会在启动阶段长时间占住写锁，节点租约随之到期。
            CREATE INDEX IF NOT EXISTS events_actor ON events(actor, seq);
            CREATE INDEX IF NOT EXISTS events_target ON events(target, seq);
            -- read_messages 只要与某人有关的最近几十条；没有索引时要反序列化全部消息再在 Python 里过滤。
            -- 索引项隐含 rowid，按 rowid 倒序取前几条可以直接沿索引读。消息约 2 万条，启动时建索引不到 1 秒。
            CREATE INDEX IF NOT EXISTS objects_message_sender ON objects(json_extract(data, '$.sender')) WHERE kind = 'message';
            CREATE INDEX IF NOT EXISTS objects_message_target ON objects(json_extract(data, '$.target')) WHERE kind = 'message';
            -- “某个执行者的任务”在事件入库、消息投递、派发里每次都要查；没有索引时是反序列化全部任务
            -- （线上 324 个、约 2MB）再在 Python 里比对，实测占事件循环主线程的 93%，节点心跳与文件读取因此超时。
            CREATE INDEX IF NOT EXISTS objects_task_agent ON objects(json_extract(data, '$.agent')) WHERE kind = 'task';
            -- wait_for_reply 要找“回复了某条消息的消息”；消息只增不删，全表扫描会随历史线性变慢。
            CREATE INDEX IF NOT EXISTS objects_message_reply ON objects(json_extract(data, '$.reply_to')) WHERE kind = 'message';
            -- 任务结束时要关闭它名下的问题；问题表小，但同样不该靠遍历找。
            CREATE INDEX IF NOT EXISTS objects_question_task ON objects(json_extract(data, '$.task')) WHERE kind = 'question';
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

    # 允许按字段查找的 (kind, 字段)：每一项都必须有同名表达式索引。白名单的目的是让“没有索引的查找”
    # 无法被写出来——否则它会悄悄退化成全表扫描，在热路径上拖住事件循环（见 objects_task_agent 的说明）。
    INDEXED = {('task', 'agent'), ('message', 'reply_to'), ('question', 'task')}

    def lookup(self, kind, field, value, limit=None):
        """按索引字段取对象，按写入顺序；只反序列化命中的行。"""
        if (kind, field) not in self.INDEXED:
            raise ValueError(f'{kind}.{field} 没有索引，不能直接查找；先建索引并登记到 Store.INDEXED')
        sql = f"SELECT data FROM objects WHERE kind=? AND json_extract(data, '$.{field}') = ? ORDER BY rowid"
        args = [kind, value]
        if limit:
            sql += ' LIMIT ?'; args.append(int(limit))
        return [json.loads(r[0]) for r in self.db.execute(sql, args)]

    def queued_for(self, target):
        """发给 target 的排队中消息。走 objects_state 只取 queued 行（队列通常只有几条），再在 SQL 里按收件人过滤，
        不把别人的队列反序列化到 Python。没有为此单独建索引：规划器本来就选 objects_state，多一个索引只增加写入成本。"""
        return [json.loads(r[0]) for r in self.db.execute(
            "SELECT data FROM objects WHERE kind='message' AND json_extract(data, '$.state') = 'queued' "
            "AND json_extract(data, '$.target') = ? ORDER BY rowid", (target,))]

    def queued_targets(self):
        """有排队消息的收件人，按其 agent 的创建顺序排列。"""
        return [r[0] for r in self.db.execute(
            "SELECT DISTINCT json_extract(data, '$.target') FROM objects WHERE kind='message' "
            "AND json_extract(data, '$.state') = 'queued'")]

    def get_many(self, kind, ids):
        """按 id 批量取，保持写入顺序（rowid）；不存在的 id 被忽略。"""
        ids = list(ids)
        if not ids:
            return []
        marks = ','.join('?' * len(ids))
        return [json.loads(r[0]) for r in self.db.execute(
            f'SELECT data FROM objects WHERE kind=? AND id IN ({marks}) ORDER BY rowid', (kind, *ids))]

    def messages_of(self, who, limit):
        """与 who 有关（发出或收到）的最近 limit 条消息，按写入顺序。
        两路各沿部分索引倒序取 limit 条再合并，只解析这几十行；不能取全部消息到 Python 里过滤。
        表达式须与 objects_message_* 索引定义完全一致才会命中。"""
        rows = {}
        for col in ('sender', 'target'):
            for rowid, data in self.db.execute(
                    f"SELECT rowid, data FROM objects WHERE kind = 'message' AND json_extract(data, '$.{col}') = ? "
                    'ORDER BY rowid DESC LIMIT ?', (who, limit)):
                rows[rowid] = data
        return [json.loads(rows[r]) for r in sorted(rows)[-limit:]]

    def where(self, kind, *states):
        """按 state 取对象（走 objects_state 索引）；表达式须与索引定义完全一致才会命中。"""
        marks = ','.join('?' * len(states))
        return [json.loads(r[0]) for r in self.db.execute(
            f"SELECT data FROM objects WHERE kind=? AND json_extract(data, '$.state') IN ({marks}) ORDER BY rowid",
            (kind, *states))]

    def project(self, kind, fields, *states):
        """只取对象的指定字段（可按 state 过滤），在 SQL 里提取，不把长字段读成 Python 对象。

        任务的 history/claim/evidence 占体积的九成；网页每秒轮询，只要列表需要的那几个字段。
        缺失的字段不会出现在结果里（与 JSON 里值为 null 区分开）：前端对 undefined 与 null 的比较结果不同。
        每个字段单独一列再按 json_type 还原：不能用 json_object 一次拼出，SQLite 会把小数截成 15 位有效数字，
        时间戳 1792412193.5345566 会变成 1792412193.53456，列表排序与比较随之改变。
        fields 与 states 都来自代码常量，不接受外部输入，所以直接拼进 SQL。
        """
        cols = ','.join(f"json_type(data, '$.{f}'), json_extract(data, '$.{f}')" for f in fields)
        sql = f"SELECT {cols} FROM objects WHERE kind=?"
        args = [kind]
        if states:
            sql += f" AND json_extract(data, '$.state') IN ({','.join('?' * len(states))})"
            args += states
        out = []
        for row in self.db.execute(sql + ' ORDER BY rowid', args):
            item = {}
            for i, f in enumerate(fields):
                kind_, value = row[2 * i], row[2 * i + 1]
                if kind_ is None:
                    continue                      # 字段缺失
                if kind_ in ('true', 'false'):
                    value = kind_ == 'true'       # json_extract 把布尔变成 0/1，须还原
                elif kind_ in ('object', 'array'):
                    value = json.loads(value)     # 嵌套值以 JSON 文本返回
                elif kind_ == 'null':
                    value = None
                item[f] = value
            out.append(item)
        return out

    def where_not(self, kind, *states):
        """state 不在给定集合里的对象。过滤在 SQL 里完成：只有命中的行才会被反序列化。"""
        marks = ','.join('?' * len(states))
        return [json.loads(r[0]) for r in self.db.execute(
            f"SELECT data FROM objects WHERE kind=? AND json_extract(data, '$.state') NOT IN ({marks}) ORDER BY rowid",
            (kind, *states))]

    def emit(self, kind, text='', *, actor='system', target=None, task=None, data=None, visible=False):
        cur = self.db.execute('INSERT INTO events(at,actor,target,task,kind,text,data,visible) VALUES(?,?,?,?,?,?,?,?)',
                             (time.time(), actor, target, task, kind, text,
                              json.dumps(data or {}, ensure_ascii=False), int(visible)))
        return cur.lastrowid

    def events_snapshot(self, **kw):
        """在只读连接上读事件，并在同一个读快照里取 highwater；调用方必须放到线程里执行。

        为什么单开只读连接：按 agent 过滤的查询要倒序扫 events 表（线上 14GB、280 万行），
        在事件循环线程里跑会拖住节点心跳与租约续期，实测的现象就是点开某个执行者的历史后网页卡死、
        中心不断提示租约到期。WAL 下只读连接不阻塞写入，主连接也不能跨线程用。
        highwater 与查询必须在同一个读事务里：先于查询的游标最多让客户端重复拉一次，
        晚于查询的游标会让两次读之间新写入的事件被永久跳过。
        """
        db = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute('BEGIN')
            top = db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]
            return self.events(db=db, **kw), top
        finally:
            db.close()

    def events(self, after=0, limit=200, task=None, agent=None, visible=False, conversation=False,
               before=None, ui=False, compact=False, harness=True, db=None):
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
        limit = max(1, min(int(limit), 500))
        where = ' AND '.join(clauses) or '1'
        order = 'DESC' if before is not None else 'ASC'
        conn = db or self.db
        if not agent:
            rows = list(conn.execute(f'SELECT * FROM events WHERE {where} ORDER BY seq {order} LIMIT ?', [*args, limit]))
        else:
            # 为什么按分支分别取再合并，而不写成一条 actor=? OR target=? OR task IN (...)：
            # 单条 OR 会让 SQLite 走 MULTI-INDEX OR，先收齐三路命中的全部行再排序取 LIMIT，
            # 事件最多的执行者（Master）要处理上百万行，线上实测超过 30 秒；
            # 每一路各自沿 (actor|target|task, seq) 索引按 seq 方向读，凑够 limit 条就停（线上 master 约 12ms）。
            # 过滤条件在每一路里都相同，所以合并后取前 limit 条与单条 OR 的结果一致。
            tasks = [r[0] for r in conn.execute(
                "SELECT id FROM objects WHERE kind='task' AND json_extract(data,'$.agent')=?", (agent,))]
            branches = [('actor=?', agent), ('target=?', agent), *(('task=?', t) for t in tasks)]
            found = {}
            for cond, arg in branches:
                for r in conn.execute(f'SELECT * FROM events WHERE {cond} AND {where} ORDER BY seq {order} LIMIT ?',
                                      [arg, *args, limit]):
                    found[r['seq']] = r
            rows = sorted(found.values(), key=lambda r: r['seq'], reverse=before is not None)[:limit]
        if before is not None:
            rows.reverse()
        return [dict(r) | {'data': json.loads(r['data'])} for r in rows]

    def find(self, kind, ident):
        row = self.db.execute('SELECT data FROM objects WHERE kind=? AND id=?', (kind, ident)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, kind, ident):
        self.db.execute('DELETE FROM objects WHERE kind=? AND id=?', (kind, ident))

    def history(self, query='', before=None, limit=50, aside=True):
        """主聊天的对话记录（主人、Master、飞书往来、定期审核），新的在前，可按关键词筛选。

        为什么单开只读连接：带关键词时要扫全部事件（实测 190 万条约 2 秒），由调用方放到线程里执行；
        主连接只能在事件循环线程用，在这里扫会卡住节点同步、让租约过期。WAL 下只读连接不阻塞写入。
        """
        clauses = ["((kind IN ('chat', 'review', 'feishu_in', 'feishu_out') AND visible=1)"
                   + (" OR (kind = 'reply' AND actor = 'master')" if aside else '') +
                   " OR (kind = 'message' AND actor = 'owner' AND target = 'master' AND json_extract(data, '$.copy') = 0))"]
        args = []
        if before:
            clauses.append('seq < ?'); args.append(int(before))
        for word in str(query or '').split()[:5]:
            clauses.append("text LIKE ? ESCAPE '\\'")
            args.append('%' + word.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        args.append(max(1, min(int(limit), 200)))
        db = sqlite3.connect(f'file:{self.path}?mode=ro', uri=True)
        try:
            db.row_factory = sqlite3.Row
            rows = db.execute(f"SELECT seq, at, actor, target, task, kind, text, data FROM events WHERE {' AND '.join(clauses)} "
                              'ORDER BY seq DESC LIMIT ?', args).fetchall()
        finally:
            db.close()
        return [dict(r) | {'data': json.loads(r['data'])} for r in rows]

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
