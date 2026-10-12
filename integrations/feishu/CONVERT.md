# 将已审阅的单会话历史转换为导入包

`cthulhu.feishu_convert` 生成现有 `cthulhu.feishu_import` 的 version 1 输入，直接复用其 `validate`。依赖包含该导入器的代码版本；转换器不建立数据库、授权入口或新会话，不启动模型、不恢复旧线程、不发送消息。绑定配置来自中心已核实映射，不能由导入包或 OAuth 用户自证。

源码交接分支的 `integrations/feishu-migration/continuity/` 只存放三个经审阅文件，不含运行状态。接收方在包含实际导入器的独立工作树中，将 `feishu_convert.py` 放入 `cthulhu/`，`test_feishu_convert.py` 放入 `tests/`，本说明放入 `integrations/feishu/CONVERT.md`，再运行下述测试。不要用旧交接分支整体替换中心当前代码；本改动不替换或复制导入器。

调用者必须显式提供已核实的实际 Codex home，程序不猜测 `~/.codex`，也不读取进程环境。运行路径如下：

```bash
venv/bin/python -m cthulhu.feishu_convert \
  --config TRUSTED_DISABLED_CONFIG \
  --catalog PRIVATE_SOURCE_CATALOG \
  --codex-home VERIFIED_EFFECTIVE_CODEX_HOME \
  --reviewed PRIVATE_REVIEWED_TEXT_JSON \
  --output PRIVATE_DIRECTORY/continuity.json
```

输出目录须为当前操作账号的私密目录（通常 `0700`）。文件以排他创建写入 `0600`，不覆盖已有文件。标准输出只有成功状态、回合数量与实际文件 SHA256；错误不回显私密输入。真实材料不进入 Git。迁移已有授权；审阅标记是文本处理记录，不是新增授权机制。

源选择和约束：

- 从 catalog 数组精确选择可信 `owner.legacy_scope_id` 的 `agentId=codex` 记录，校验原 scope/agent/cwd/policy 组合键，不使用 `lastSummary`。重复组合键拒绝。收集选定线程在**整个 catalog 元数据**中的全部 scope；出现其他 scope 即暂缓该线程，且在读取索引和正文前拒绝。
- 对显式 home 内 `state_5.sqlite` 使用只读连接、只读查询和参数化 `SELECT rollout_path FROM threads WHERE id=?`。只查询选中线程；路径必须为绝对路径，并在解析符号链接后仍位于该 home 的 `sessions` 或 `archived_sessions` 内。缺失、歧义、逃逸、不同线程共用文件均拒绝。不复制数据库、目录树、索引或工作区。
- 审阅文件为每个选中线程固定完整源 JSONL 的 SHA256；哈希不符拒绝。首行必须是匹配 thread ID 的 `session_meta`。源哈希与真实 catalog scope 列表进入导入包。
- 支持有 `turn_id` 的 `event_msg/task_started`，以及相同 ID 的 `task_complete`、`task_failed`、`turn_aborted` 终结记录。未知形式、缺少终结记录或相互交叠的边界拒绝，不猜测状态。这里验证的是下方虚构 fixture 所代表的格式，尚未读取真实 rollout 验证全部历史兼容性。
- 文字来源引用支持 `event_msg/user_message`、`event_msg/agent_message`，以及 `response_item/message` 的 user/assistant 文本块。system/developer、工具、环境、provider 字段及多媒体块不能作为文字来源；未引用的记录不进入输出。尚未支持的历史格式须先补充经审查的解析测试，不能通过伪造边界绕过。
- 导入回合 ID 来自源 `turn_id`，在线程内必须唯一；时间取终结记录的带时区 ISO 时间并转为 Unix 秒。跨线程按时间、线程 ID、回合 ID 稳定排序。失败或中断若没有 assistant 消息，可用空 `reply_lines` 和空 `reply`，不伪造回答。
- 超过 10000 回合或 32 MiB 输出直接拒绝，不截断；每个输入文件也有 32 MiB 保守上限。全部源线程都需要哈希，审阅文本可只包含该 scope 内实际选定的历史回合，不会自动导出未审阅回合。

## 显式审阅文件

以下仅为虚构格式。行号从 1 起，指向固定哈希的原 JSONL，不是搜索全文或匹配文本：

```json
{
  "version": 1,
  "reviewed": true,
  "threads": [{"thread_id": "thread-a", "sha256": "实际源文件的64位小写哈希"}],
  "turns": [{
    "source_thread": "thread-a",
    "start_line": 2,
    "input_lines": [4],
    "reply_lines": [6],
    "finish_line": 7,
    "input": "经人工审阅的历史问题或摘要",
    "reply": "经人工审阅的历史答复或摘要"
  }]
}
```

**转换器只输出审阅文件中显式提供的 `input`/`reply`，从不自动复制原始正文。** 审阅人员要去除凭据、公司网关资料、其他 scope 的内容和不适合迁入的敏感细节。角色正确并不证明自由文本安全；`reviewed: true` 也不能让程序证明审阅者确实完成了工作。文本可为忠实摘要，程序只验证引用、边界、哈希、格式及导入约束，不评价摘要正确性。原系统/工具指令不能借摘要迁为新指令。

## 虚构验证及中心校验

专项测试只创建临时虚构 SQLite 索引、catalog、JSONL 和绑定配置。它们运行真实转换 CLI 与导入器 dry-run，不传 `--db` 或 `--apply`：

```bash
venv/bin/python -m unittest discover -s tests -p 'test_feishu_convert.py' -v
```

可在新建的私密目录重建相同虚构输入（路径不应指向真实 Codex home）：

```bash
venv/bin/python - <<'PY'
from pathlib import Path
import runpy
root = Path('fictional-conversion-check').resolve()
root.mkdir(mode=0o700)
runpy.run_path('tests/test_feishu_convert.py')['fixture'](root)
PY
venv/bin/python -m cthulhu.feishu_convert \
  --config fictional-conversion-check/config.json \
  --catalog fictional-conversion-check/catalog.json \
  --codex-home fictional-conversion-check/fictional-home \
  --reviewed fictional-conversion-check/reviewed.json \
  --output fictional-conversion-check/continuity.json
venv/bin/python -m cthulhu.feishu_import \
  --config fictional-conversion-check/config.json \
  --bundle fictional-conversion-check/continuity.json \
  --sha256 CONVERTER_PRINTED_SHA256
```

中心真实接收后同样先用其可信禁用配置、实际传输哈希和 `PRIVATE_CENTRAL_INCOMING/continuity.json` dry-run。真实 `--db TEMPLE_DB --apply` 属于中心既有导入步骤，本转换器不执行。真实身份、源内容兼容性、审阅和私密传输尚未通过虚构测试证明。
