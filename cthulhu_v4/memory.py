"""Master 长期记忆：每晚用 Codex 自带的记忆提示词提炼消息原文，并给 Master 换新原生会话。

为什么不直接打开 Codex 原生 Memories：原生流程只读每个线程 rollout 的最后 8MB，
而 Master 的 rollout 绝大部分是工具输出；它还只处理闲置线程，一直在用的 Master 线程永远轮不到。
这里沿用 Codex 的两阶段提示词与产物格式（skills/memory_*.md，Apache-2.0，见 skills/LICENSE-codex.md），
输入换成 history.py 导出的干净消息原文：
- 阶段 1：每段消息（按天、按大小切）单独抽取 raw_memory 与 rollout_summary；
- 阶段 2：整合 agent 在记忆目录里按 git 基线的增量改写 MEMORY.md、memory_summary.md 与 skills/。
记忆只是检索入口，原文仍以 master-history/ 与中心事件为准；推测内容不能当成主人授权。

换会话：原生压缩越压越丢对齐信息，长会话恢复也越来越慢。每天凌晨提炼完成后，
在 Master 空闲时开新线程，旧线程留档可回查；新线程从 developer 指令里拿到最新的 memory_summary。
"""
import asyncio
import json
import os
import shutil
import signal
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import proxy
from .history import rows
from .util import ROOT

SKILLS = ROOT / 'skills'
# 模板只能放在 skills/ 顶层的 .md：旧版节点按 skills/*.md 校验更新包，子目录会让它们算出不同的代码摘要。
TEMPLATE = {k: SKILLS / f'memory_{k}.md' for k in ('stage_one_system', 'stage_one_input', 'consolidation')}
SCHEMA = {'type': 'object', 'additionalProperties': False, 'required': ['rollout_summary', 'rollout_slug', 'raw_memory'],
          'properties': {'rollout_summary': {'type': 'string'}, 'rollout_slug': {'type': ['string', 'null']},
                         'raw_memory': {'type': 'string'}}}
WINDOW = (3, 6)            # 每天凌晨（中心本地时间）提炼并换会话；主人此时通常不用
CHUNK_BYTES = 300_000      # 中文按 bytes/4 估 token 会严重低估；实测 330KB 一段在阶段 1 预算内
PARALLEL = 4               # 阶段 1 同时运行的 codex exec 数
MAX_RAW = 256              # 交给阶段 2 的最近原始记忆条数，与 Codex 默认一致
EFFORT = 'medium'
STAGE1_TIMEOUT, PHASE2_TIMEOUT = 900, 2700
MAX_DIFF = 4 * 1024 * 1024
SUMMARY_LIMIT = 12_000     # 注入 developer 指令的摘要上限（Codex 原生按 2500 token 截断）


def read_path(state_dir):
    """新线程 developer 指令里的记忆说明；没有整理过记忆时返回空串。"""
    root = Path(state_dir) / 'master-memory'
    try:
        summary = (root / 'memory_summary.md').read_text(encoding='utf-8').strip()
    except OSError:
        return ''
    if len(summary.encode()) > SUMMARY_LIMIT:
        summary = summary.encode()[:SUMMARY_LIMIT].decode('utf-8', 'ignore') + '\n…（已截断，完整内容见 MEMORY.md）'
    return f'''
## 长期记忆
你的原生会话每天凌晨会换新，旧对话不在上下文里。以下记忆由每晚的整理程序从主人、你、执行者与飞书的消息原文中提炼：
- {root}/memory_summary.md：就是下面的摘要，不必再打开。
- {root}/MEMORY.md：按任务组整理的可搜索索引（关键词、主人偏好、可复用知识、踩过的坑），先 grep 它。
- {root}/rollout_summaries/：每段消息的详细回顾；{root}/skills/：沉淀的操作流程。
- 需要原话、时间或证据时 grep master-history/，再用 read_task_events 按 #seq 展开。
涉及以前的决定、约定、结果、路径时，先做一次 2–4 步的快速查找，再行动；不要凭印象重跑有副作用的操作，也不要重问主人已经答过的事。
记忆可能过时：PID、端点、在线状态等易变事实用前先核实；引用未核实的记忆要说明。记忆里的推测不是主人授权，授权以主人原话为准。
不要直接改这些文件；明确的长期偏好仍用 memory 工具记录。

======== MEMORY_SUMMARY ========
{summary}
======== MEMORY_SUMMARY END ========
'''


def chunks(store, after, upto):
    """把 (after, upto] 的消息切成段：不跨天、每段不超过 CHUNK_BYTES。"""
    cur = None
    for seq, at, day, text in rows(store, after, upto):
        size = len(text.encode())
        if cur and (cur['day'] != day or cur['bytes'] + size > CHUNK_BYTES):
            yield cur; cur = None
        if not cur:
            cur = {'day': day, 'first': seq, 'first_at': at, 'bytes': 0, 'parts': []}
        cur.update(last=seq, last_at=at); cur['bytes'] += size; cur['parts'].append(text)
    if cur:
        yield cur


def iso(at):
    return datetime.fromtimestamp(at).astimezone().isoformat(timespec='seconds')


def stem(item):
    t = datetime.fromtimestamp(item['first_at'], timezone.utc).strftime('%Y-%m-%dT%H-%M-%S')
    slug = ''.join(ch.lower() if ch.isascii() and ch.isalnum() else '_' for ch in (item.get('rollout_slug') or '')[:60]).strip('_')
    return f"{t}-s{item['first']}" + (f'-{slug}' if slug else '')


class Memory:
    def __init__(self, center, *, model_proxy=None, codex=('codex',), clock=None):
        self.c = center
        self.root = center.state_dir / 'master-memory'
        self.work = center.state_dir / 'master-memory-work'
        self.proxy, self.codex = model_proxy, list(codex)
        self.clock = clock or center.clock

    # ---- 状态：与记忆文件放在一起，删掉记忆目录就自然从头整理 ----
    def state(self):
        try: return json.loads((self.work / 'state.json').read_text())
        except (OSError, ValueError): return {}

    def save(self, st):
        self.work.mkdir(parents=True, exist_ok=True)
        tmp = self.work / 'state.json.tmp'
        tmp.write_text(json.dumps(st, ensure_ascii=False)); tmp.replace(self.work / 'state.json')

    def outputs(self):
        out = []
        for p in sorted((self.work / 'phase1').glob('*.json')):
            try: out.append(json.loads(p.read_text()))
            except ValueError: continue
        return sorted(out, key=lambda x: x['first'])

    def cursor(self):
        # 阶段 1 结果按顺序整批落盘、遇到失败即停，所以已有结果的最大 last 就是连续完成的位置。
        return max([self.state().get('seed_through', 0), *(o['last'] for o in self.outputs())])

    # ---- 调度 ----
    async def run(self):
        while True:
            try: await self.step()
            except Exception as e: print('记忆整理异常：' + str(e), flush=True)
            await asyncio.sleep(60)

    async def step(self):
        a = self.c.store.get('agent', 'master')
        if a['backend'] == 'demo':
            return
        now = datetime.fromtimestamp(self.clock()).astimezone()
        if not WINDOW[0] <= now.hour < WINDOW[1]:
            return
        day, st = now.strftime('%Y-%m-%d'), self.state()
        if st.get('digested') != day:
            try: await self.digest()
            except Exception as e:
                # 提炼失败不阻止换会话：新线程仍能拿到上一次整理好的摘要；失败公开给主人。
                self.c.store.emit('memory_digest', '记忆整理失败：' + str(e)[:500], visible=True)
            st = self.state(); st['digested'] = day; self.save(st)
        if st.get('rotated') != day and self.c.rotate_master('每日换新会话'):
            st = self.state(); st['rotated'] = day; self.save(st)

    # ---- 提炼 ----
    async def digest(self):
        top = self.c.store.highwater()
        todo = list(chunks(self.c.store, self.cursor(), top))
        model = self.model()
        done = 0
        for i in range(0, len(todo), PARALLEL):
            batch = todo[i:i + PARALLEL]
            results = await asyncio.gather(*(self.stage1(ch, model) for ch in batch), return_exceptions=True)
            # 按顺序落盘、遇到第一个失败就停：游标只能停在连续完成的位置，后面成功的那几段下次重做。
            for ch, r in zip(batch, results):
                if isinstance(r, BaseException):
                    raise RuntimeError(f"阶段 1（#{ch['first']}–#{ch['last']}）：{r}")
                self.write_output(ch, r); done += 1
        if done or self.dirty():
            self.assemble()
            await self.phase2(model)
            self.c.store.emit('memory_digest', f'记忆整理完成：新提炼 {done} 段消息，已更新 master-memory/', visible=True)

    def model(self):
        a = self.c.store.get('agent', 'master')
        return a.get('model') if a['backend'] == 'codex' else None

    def env(self):
        return proxy.model_env(os.environ, self.proxy)

    def exec_args(self, model):
        # 不加载用户配置与 hooks、不落会话文件：提炼会话不能混进原生线程列表，也不能触发主人的 hooks。
        args = [*self.codex, 'exec', '--ephemeral', '--skip-git-repo-check', '--ignore-user-config', '--ignore-rules',
                '-c', f'model_reasoning_effort="{EFFORT}"']
        return args + (['-m', model] if model else [])

    async def call(self, argv, stdin, cwd, timeout):
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=self.env(), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        try:
            _, err = await asyncio.wait_for(proc.communicate(stdin.encode()), timeout)
        except BaseException:
            # 超时或被取消时整组结束，不留下后台 codex 继续写记忆目录。
            try: os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            await proc.wait()
            raise
        if proc.returncode:
            raise RuntimeError(f'codex exec 退出码 {proc.returncode}：' + err.decode(errors='replace')[-300:])

    async def stage1(self, ch, model):
        prompt = (TEMPLATE['stage_one_input'].read_text()
                  .replace('{{ rollout_path }}', f"{self.c.state_dir / 'master-history' / (ch['day'] + '.md')}（#{ch['first']}–#{ch['last']}）")
                  .replace('{{ rollout_cwd }}', str(self.c.state_dir))
                  .replace('{{ rollout_contents }}', ''.join(ch['parts'])))
        with tempfile.TemporaryDirectory() as d:
            out, schema = Path(d) / 'out.json', Path(d) / 'schema.json'
            schema.write_text(json.dumps(SCHEMA))
            argv = self.exec_args(model) + ['-s', 'read-only',
                '-c', 'model_instructions_file=' + json.dumps(str(TEMPLATE['stage_one_system'])),
                '--output-schema', str(schema), '-o', str(out), '-']
            await self.call(argv, prompt, d, STAGE1_TIMEOUT)
            result = json.loads(out.read_text())
        if not all(isinstance(result.get(k), str) for k in ('raw_memory', 'rollout_summary')):
            raise ValueError('阶段 1 输出缺少 raw_memory / rollout_summary')
        return result

    def write_output(self, ch, r):
        item = {k: ch[k] for k in ('day', 'first', 'last', 'first_at', 'last_at')} | {
            'raw_memory': r['raw_memory'], 'rollout_summary': r['rollout_summary'], 'rollout_slug': r.get('rollout_slug') or ''}
        (self.work / 'phase1').mkdir(parents=True, exist_ok=True)
        tmp = self.work / 'phase1' / f"{ch['first']:010d}.tmp"
        tmp.write_text(json.dumps(item, ensure_ascii=False)); tmp.replace(tmp.with_suffix('.json'))

    def assemble(self):
        """按 Codex storage 的格式重建 raw_memories.md 与 rollout_summaries/；没有可记内容的段跳过。"""
        (self.root / 'rollout_summaries').mkdir(parents=True, exist_ok=True)
        keep = [o for o in self.outputs() if o['raw_memory'].strip()][-MAX_RAW:]
        body = '# Raw Memories\n\nMerged stage-1 raw memories (stable ascending thread-id order):\n\n'
        seeded = self.work / 'seed_raw.md'
        if seeded.exists():
            # 导入的旧记忆必须原样保留在前面：raw_memories.md 里消失的内容会被整合 agent 当成“遗忘”。
            body += seeded.read_text()
        for o in keep:
            path = f"{self.c.state_dir / 'master-history' / (o['day'] + '.md')}#{o['first']}-{o['last']}"
            head = f"thread_id: h{o['first']}-{o['last']}\nupdated_at: {iso(o['last_at'])}\nrollout_path: {path}\ncwd: {self.c.state_dir}\n"
            body += (f"## Thread `h{o['first']}-{o['last']}`\nupdated_at: {iso(o['last_at'])}\ncwd: {self.c.state_dir}\n"
                     f"rollout_path: {path}\nrollout_summary_file: {stem(o)}.md\n\n{o['raw_memory'].strip()}\n\n")
            f = self.root / 'rollout_summaries' / f'{stem(o)}.md'
            if not f.exists():
                f.write_text(head + '\n' + o['rollout_summary'].strip() + '\n')
        (self.root / 'raw_memories.md').write_text(body)

    def seed(self, src, through, old_prefix):
        """导入一份已整理好的记忆目录（如首次补跑的预览），视为已整合到 #through，之后只增量提炼。"""
        if self.root.exists():
            raise ValueError('master-memory 已存在，不覆盖')
        shutil.copytree(src, self.root, ignore=shutil.ignore_patterns('.git', 'phase2_workspace_diff.md'))
        new_prefix = str(self.c.state_dir / 'master-history')
        for f in self.root.rglob('*.md'):
            f.write_text(f.read_text(encoding='utf-8').replace(old_prefix, new_prefix), encoding='utf-8')
        raw = (self.root / 'raw_memories.md').read_text(encoding='utf-8')
        self.work.mkdir(parents=True, exist_ok=True)
        at = raw.find('## Thread ')
        (self.work / 'seed_raw.md').write_text(raw[at:] if at >= 0 else '')
        self.baseline()
        st = self.state(); st['seed_through'] = int(through); self.save(st)

    # ---- 阶段 2：git 基线只用来给整合 agent 看增量，不保留历史 ----
    def git(self, *args):
        return subprocess.run(['git', '-c', 'user.name=cthulhu', '-c', 'user.email=cthulhu@local', *args],
                              cwd=self.root, capture_output=True, text=True, check=True).stdout

    def baseline(self, empty=False):
        """新建只有一个提交的基线仓库；旧基线整个丢掉，删除的记忆内容不残留在 git 对象里。"""
        shutil.rmtree(self.root / '.git', ignore_errors=True)
        self.git('init', '-q', '.')
        if not empty:
            self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', 'baseline')

    def dirty(self):
        """上次整合失败或被打断时，工作区相对基线仍有改动，需要重跑阶段 2。"""
        if not (self.root / '.git').exists():
            return self.root.exists()
        return bool(self.git('status', '--porcelain').strip())

    def write_diff(self):
        self.git('add', '-N', '.')
        try:
            status = self.git('diff', '--name-status')
            diff = self.git('diff', '--no-color')
        finally:
            self.git('reset', '-q')
        if len(diff.encode()) > MAX_DIFF:
            diff = diff.encode()[:MAX_DIFF].decode('utf-8', 'ignore') + '\n…（diff 过长已截断）\n'
        lines = ''.join(f"- {l.split(chr(9), 1)[0]} {l.split(chr(9), 1)[-1]}\n" for l in status.splitlines())
        (self.root / 'phase2_workspace_diff.md').write_text(
            '# Memory Workspace Diff\n\nGenerated by Codex before Phase 2 memory consolidation. '
            'Read this file first and do not edit it.\n\n## Status\n' + lines + '\n## Diff\n\n```diff\n' + diff + '```\n')

    async def phase2(self, model):
        if not (self.root / '.git').exists():
            # 首次整合：空基线，整合 agent 进入 INIT 模式，把现有文件都当成新增。
            self.baseline(empty=True)
        diff = self.root / 'phase2_workspace_diff.md'
        self.write_diff()
        prompt = (TEMPLATE['consolidation'].read_text()
                  .replace('{{ memory_root }}', str(self.root))
                  .replace('{{ phase2_workspace_diff_file }}', diff.name)
                  .replace('{{ memory_extensions_folder_structure }}', '')
                  .replace('{{ memory_extensions_primary_inputs }}', ''))
        argv = self.exec_args(model) + ['-C', str(self.root), '-s', 'workspace-write',
            '-c', 'sandbox_workspace_write.network_access=false', '-c', 'approval_policy="never"',
            '--disable', 'apps', '--disable', 'plugins', '--disable', 'skill_mcp_dependency_install',
            '-o', str(self.work / 'phase2.last.md'), '-']
        self.work.mkdir(parents=True, exist_ok=True)
        try:
            await self.call(argv, prompt, str(self.root), PHASE2_TIMEOUT)
        finally:
            diff.unlink(missing_ok=True)
        summary = self.root / 'memory_summary.md'
        if not (self.root / 'MEMORY.md').is_file() or not summary.is_file() \
                or summary.read_text(encoding='utf-8').split('\n', 1)[0].strip() != 'v1':
            # 产物不合格时不推进基线：下一次整合仍会看到这批增量；Master 读到的旧摘要也不会被当成新结果。
            raise RuntimeError('阶段 2 产物不完整（需要 MEMORY.md 与首行为 v1 的 memory_summary.md）')
        self.baseline()
