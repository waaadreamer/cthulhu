# LLM Official 接入与发布

本次只增加 Galaxy API 路由，不改阿撒托斯、星系枢纽或总结 AI 的模型。发布由管理者执行。代码基线 fb48835320ef4e6095b41bf9e8446534e5bd792c；开发实际会话为 gpt-6-astra / medium。

## 路由与兼容

| 选择项 | provider | 请求公开 ID | runtime backend | provider backend | 出站 ID / 协议 |
|---|---|---|---|---|---|
| LLM Official | ali | claude-opus-5-5-official | galaxy_api | codex | claude-opus-5-5 / Responses |
| Claude Opus 5.5（中转站） | ali | claude-opus-5-5 | claude | claude | claude-opus-5-5 / Messages |

公开 ID 先经中心 allowlist、consumer 和协议校验，provider 再独立校验，最后仅替换出站 body.model。model_mapping 仅存在 provider 本地配置，不接受调用者覆盖。model_labels 为可选公开展示元数据；旧 provider 未提供标签时 UI 保留 modelLabel 回退。返回 SSE 不改写模型；api_result 同时保留 requested_model、upstream_model、returned_model。不存在前缀猜测、CLI 自动切换或默认 fallback。

新连接沿用阿里现有 codex_config_file、codex_provider=ss-gateway，原生读取 experimental_bearer_token；HTTPS /responses、固定 CA、trust_env=False、follow_redirects=False 保持现状。旧入口仍独立读取 claude_config_file，走 Messages，不共用 token。显示尾缀只是主人指定标签。

现有 Galaxy API 是 SSE 传输：输入 stream=false 仍规范为 stream=true，不新增 nonstream 响应接口。续接发送完整 input（含 function_call/function_call_output），store=false；previous_response_id 保持拒绝，避免把状态交给上游。新入口工具只用 auto；不能用 required/forced canary。

阿里首次真实完整 old_one 请求返回 HTTP400：`tool type 'web_search_20250305' is not supported for this model`。该模型必须声明 `model_capabilities: {"claude-opus-5-5-official": {"web_search": false}}`。catalog 将此能力传到现有 Codex 启动配置，设置 `web_search="disabled"`，使 CLI 在生成工具列表时关闭内置网页搜索。provider 不删除已经生成的工具或改写历史；Astra 和未声明此能力的模型保持原行为。运行记录 `runtime.api_result.model_capabilities.web_search=false`（`/api/directory` 对应 agent）明确呈现这一能力差异；模型 catalog 同时可查，缺省字段保持兼容。核心命令/文件/任务工具仍需真实验证，namespace 支持不能由首个 web_search 错误推断。scripts/verify_official_old_one.py 用真实 Codex 和完整 old_one 技能/工具集验证，未经验证不移除 web_search 等字段来掩盖兼容性错误。

## 验证与发布顺序

1. 在阿里隔离 clone 应用路由 commit；先运行完整 old_one 验证。它创建独立 loopback Temple、Galaxy hub、old_one 子进程、任务和临时 state；只有 provider 向真实上游请求。使用现有本地配置文件读取凭据，不连接生产 WS、不启动第二个 run.sh。

   ```sh
   cd /mnt/vepfs/base2/wangzeyuan/.cthulhu-v3/work/t_117b5a75b38dab56-isolated/repo
   /mnt/vepfs/base2/wangzeyuan/.cthulhu-v3/venv/bin/python scripts/verify_official_old_one.py \
     --galaxy-config /mnt/vepfs/base2/wangzeyuan/.cthulhu-v3/galaxy.json \
     --output ../official-isolated.json
   ```

   必须 status=passed，任务已提交审核、turn_status=completed、文件随机串匹配、auto 工具结果已续接，runtime 为新公开 ID 且 fallback_active=false。本地模拟上游测试不代替这项。脚本失败仍写脱敏证据，finally 终止其自己创建的进程并删除临时 state；不记录凭据或完整请求。验证等待上限约 8 分钟，加子进程终止时间。

2. 合并路由与并行 UI 源码。其他任务与本任务共同修改 temple.py、web/src/api.ts，但改动区块不同。不要挑选不同构建的 static 互相覆盖；最终合并后在 web 运行 npm run build，并提交 cthulhu/static。运行 `venv/bin/python -m unittest discover -s tests -v`。

3. 准备中心 allowlist 和阿里 provider 配置副本；此阶段保留所有运行时默认/覆盖。以下命令只生成私有副本，管理者核对 diff 后才安装。不要提交生成文件。

   中心：
   ```sh
   python3 - <<'PY'
   import json, os
   from pathlib import Path
   source = Path('/etc/cthulhu-v3test/temple.json')
   c = json.loads(source.read_text())
   models = c['api_providers']['ali']['models']
   c['api_providers']['ali']['models'] = list(dict.fromkeys([*models, 'claude-opus-5-5-official']))
   target = Path('/var/lib/cthulhu-v3test/work/temple.official-stage-a.json')
   fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as f: json.dump(c, f, ensure_ascii=False, indent=2)
   PY
   ```

   阿里：
   ```sh
   python3 - <<'PY'
   import json, os
   from pathlib import Path
   root = Path('/mnt/vepfs/base2/wangzeyuan/.cthulhu-v3')
   c = json.loads((root / 'galaxy.json').read_text())
   p = c['api_provider']
   alias = 'claude-opus-5-5-official'
   p['models'] = list(dict.fromkeys([*p['models'], alias]))
   p['model_backends'] = p.get('model_backends', {}) | {alias: 'codex'}
   p['model_mapping'] = p.get('model_mapping', {}) | {alias: 'claude-opus-5-5'}
   p['model_capabilities'] = p.get('model_capabilities', {}) | {alias: {'web_search': False}}
   p['model_labels'] = p.get('model_labels', {}) | {alias: 'LLM Official', 'claude-opus-5-5': 'Claude Opus 5.5（中转站）'}
   fd = os.open(root / 'work/galaxy.official-stage-a.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
   with os.fdopen(fd, 'w') as f: json.dump(c, f, ensure_ascii=False, indent=2)
   PY
   ```

4. 审核备份并在安全窗口发布阶段 A。先协调所有 consumer（当前 test、az-test、ali、korea）回合结束与暂停新请求，再确认中心 active_streams=0。只排空 ali 自己的 agent 不足以保护其他星系和管理者正在使用的 Astra 请求。通过现有部署恢复流程发布中心 allowlist、provider 代码及阿里配置；ProviderWorker 仅启动读取配置，重连不热加载。阿里现有 run.sh 会在进程退出 5 秒后重启，不能再开第二份。相同 revision 可能跳过重启，必须核验真实 provider 启动和 catalog；部署启动保护只回滚代码，不会恢复 galaxy.json。生产重启/配置安装精确命令由阿里配套报告与管理者现有部署流程提供。

5. 从每个 consumer 查询 catalog，确认两 ID 同时可见、对应 backend 正确。运行隔离 canary 或经审核的生产独立 old_one 小任务，再检查 requested/upstream/returned、工具文件和无 fallback。全部通过后才执行下面默认迁移。

## 默认覆盖盘点和迁移

优先级为每 agent > galaxy:<id> > old_one > temple.json.old_one_runtime > 代码 OLD_ONE_RUNTIME。hub.db 中 runtimes 是中心 effective 配置的缓存；在线时中心 welcome 查询与 runtime 消息覆盖缓存，不应直接改所有 hub 数据库。中心离线时 hub 会用缓存，故恢复连接后必须检查有效值。

2026-10-15 14:39 中心只读快照在任务产物 migration-inventory.json（不提交运行状态到 Git）。当前有 12 个 roster 项，8 个 running、4 个 stopped。现有 settings 没有配置写入者/原因字段，不能仅凭值区分历史自动落盘与主人明确选择；以下按来源列出，具体清理须管理者审核。

| 来源 | 当前配置 | 处理 |
|---|---|---|
| temple.json.old_one_runtime | 旧 Claude / ali / high | 阶段 B 改新入口，无 effort/tier/fallback |
| settings.old_one | 不存在 | 阶段 B 设置新入口作为全局运行默认 |
| galaxy:test、galaxy:ali、galaxy:korea | Astra / ali / medium | 新入口验证通过后清除，让其继承全局 |
| old_f3aa1000bf30d363 | 个人 Codex gpt-6.1-sol，idle | 审核后清除，观察切换成功 |
| old_e7e30dcf43e48cb1 | 个人 Codex gpt-6.1-sol / high，idle | 同上 |
| old_a0f486a457d3aa12 | 原生 Claude / xhigh，stopped | 清理覆盖但保持 stopped；恢复时再验 |
| old_4960a164ab8bea76 | Astra / ali / medium，在途开发 | 保留到本任务完成，随后清除 |
| old_5d20049bed990f0a | Astra / ali / medium，在途核验 | 保留到当前任务完成；快照实际 config=xhigh，待边界变更不能当成已完成 |
| old_a65a95efe779c00a、old_b95906f5af1271f0、old_315237565dbe211c | 继承星系 Astra，在途开发/接入验证 | 先以各自实际 config 临时固定 Astra，完成任务后清除 |
| old_14bf53f2fb5d0476 | 继承 galaxy:ali，idle | 全局迁移后继承新入口 |
| old_9b12505e461badb5、old_7d4d048afd3f607c、old_b0d54a58cac1783e | stopped，继承星系 | 保持 stopped，恢复时继承新入口 |
| azathoth | Astra / ali / medium | 保留原值；其他管理者/枢纽/总结 AI 均不改 |

迁移不删除任何 agent、task、thread、turn log。代码使用原有 pending_runtime：运行中收到新配置仅保存 pending，在当前批次/回合完成后 apply_runtime；backend/provider/model 独立线程通过 handoff_context 的后端无关 turn log 接续。验证 pending 清空、actual runtime 和随后一次 api_result，而不是仅看 POST 成功。现有 apply_runtime 在新实例启动失败时保留原实例并写错误日志，故 pending 清空本身也不能作为切换成功证据。开发任务继续用 Astra，不能把这些临时钉住的条目漏成永久覆盖。

阶段 B 通过已有主人 HTTP /api/runtimes 接口操作，保留其能力/catalog校验与运行时通知，不直接 UPDATE settings。先保存 GET /api/runtimes 的完整 overrides 为回滚文件。以下文件是 POST body 示例，须使用管理者已登录会话；cookie 只放本地私有文件，不打印：

```json
{"key":"old_one","config":{"backend":"galaxy_api","provider":"ali","model":"claude-opus-5-5-official"}}
```

清除经审核的覆盖：`{"key":"galaxy:test","config":null}`（其他 galaxy 和 agent 同格式）。临时固定在途开发者：`{"key":"old_b95906f5af1271f0","config":{"backend":"galaxy_api","provider":"ali","model":"gpt-6-astra","effort":"medium"}}`。所有 POST 均由管理者逐项执行，不写含 manager key 的批量清除脚本。

```sh
curl --fail-with-body --silent --show-error --cookie /path/to/private-owner-cookie \
  -H 'x-cthulhu: 1' -H 'Content-Type: application/json' \
  --data-binary @/path/to/reviewed-runtime-change.json \
  http://127.0.0.1:PORT/api/runtimes
```

同时将阶段 A 中心配置副本的 old_one_runtime 更新成同一个三字段配置，安装到中心时不改其余键。部署阶段 B 代码默认与持久配置后，即使清空数据库覆盖仍是新入口。离线星系须等待重新上线并确认 catalog/协议能力；不绕过 set_runtime 对离线目标的拒绝。

## 回滚

- 隔离测试失败：不发布、不迁移默认，保留原配置和 Astra 开发；按实际错误修复再验。
- 阶段 A 失败：保持旧默认；停止新请求、等待 active_streams=0，恢复阿里代码和 galaxy.json 成对备份，再用原 run.sh 管理进程；中心恢复 allowlist/代码。不要单独回滚代码却遗留新配置。
- 阶段 B 失败：先保留新路由，按迁移前 overrides 快照通过 /api/runtimes 逐项恢复被改的 old_one/global/galaxy 键；删除迁移新增的临时覆盖，恢复 old_one_runtime 原值。等回合安全边界、pending 清空且旧实际 runtime 已确认，再考虑删除新路由。不得恢复整库来覆盖并行任务消息。
- UI 回滚与并行任务一起评估；从最终合并源码重建 static，不能拿本任务旧 bundle 覆盖同事新 UI。
