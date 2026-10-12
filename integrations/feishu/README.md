# 现有飞书助理迁入奈亚

此目录只适配原有 bridge 0.7.0，不创建外部机器人，不修改飞书显示名。运行时身份是奈亚拉托提普（nyarlathotep）。

## 软件与生命周期

使用 Node >=20.12、官方 @larksuite/cli 1.0.82 和已核验的现有 bridge 源码。package-lock 固定公开依赖；`upstream/manifest.json`固定公开基线、经审查定制补丁及结果hash，构建时离线应用补丁。未知源码拒绝构建。

`python3 integrations/feishu/build.py SOURCE/dist/cli.js TARGET/bridge.mjs` 保留上游 transport、catalog、publishers，将 CodexAdapter 的执行边界改为本地Unix IPC；生成物移除本地Codex/Claude model adapters和旧argv/stream翻译，不伪造CLI。源布局不匹配即拒绝构建。生成路径必须能解析原包依赖（例如依赖安装前缀下的nyarl目录）。

经审查的定制差异保留blockedUsers配置与过滤；旧team/admin/app-creator策略不授予中央权限，所有输入仍须通过已核验的主人绑定。旧管理命令不恢复本地模型控制权。生成的独立bridge Web UI、开发生命周期元数据及展示文案不迁入运行路径；上游MIT声明见`upstream/LICENSE.bridge`。公开CLI内置skill继续复用，不复刻未安装的私有skill。

奈亚agent拥有Node子进程、0600 Unix socket及对应Runtime。worker使用foreground `run`，不运行`start`/daemon或独立systemd服务。scope→thread只存上游catalog；scope日志复用Store.turn_log，未知/跨scope旧thread不能直接恢复。变更后重启agent时重新从固定source构建。没有worker配置时角色可测试主人网页对话，绝不自行订阅飞书。

## 中央配置约定（真实标识/凭据不进仓库）

中心配置的`feishu`含：`galaxy`（中央星系）、`app_id`（原应用）、`enabled`、`owner`、`runtime`、`worker`。

- `owner`: `sender_id`、`chat_id`和`verified_by`，来自主人可信配对证据。旧allowlist、OAuth subject、catalog不是主人证明。当前只启用已核验主人的显式会话，文档评论/会议自动入口及其他历史scope不激活。
- `runtime`: 复用现有模型配置，例如galaxy_api/provider=ali；公司网关密钥、配置和HTTP请求不离开Ali。
- `worker`: `enabled`、`root_dir`、`profile`、`source`、`command`（数组：Node绝对路径、生成的bridge.mjs、run、--profile及原profile名称、--config及中央配置路径）、`bin_dir`、`cli_config_dir`。仅中央服务账号能写配置和授权目录。
- 原bridge `LARK_CHANNEL_HOME`需指向中央专用root。CLI数据目录必须与该root下`profiles/<profile>/lark-cli`一致，程序用`LARKSUITE_CLI_CONFIG_DIR`设置边界；不能用共享token库上的不同`--profile`冒充隔离。未核验身份不得借用主人的CLI授权。

worker的`bin_dir`需要真实Node和官方lark-cli的可执行链接或实际安装目录；不是自造命令shim。worker命令和source都必须在受控安装位置，配置不来自模型/飞书消息。

## 身份、授权、历史与切换

旧机器人停止后，可用同一安装中的 `pair.mjs` 进行一次性前台配对。它复用官方 `@larksuite/channel` 和 bridge `secrets get`，不启动模型、回复或正常入口；详见 [PAIRING.md](PAIRING.md)。先核实旧listener/refresh停机回执与原应用秘密，握手成功后才向当前控制台发布全新的60分钟短语，不能让主人向已停机接收端发送或复用过期短语。

复用旧应用ID。官方bridge `secrets set --app-id <原app_id> --profile <profile>`支持不回显输入；在目标服务账号、中央专用root下执行，不能复制旧主机加密密文假装可用。官方CLI `config init --app-id ... --app-secret-stdin`也支持安全标准输入；不要把秘密放命令行、环境日志或普通聊天。所需AppSecret输入或受控转移须走明确的私有交接。

官方内置`lark-cli skills read lark-shared`及业务skill是当前帮助来源，奈亚只增加短角色说明。首次OAuth用官方`auth login --domain docs --domain drive --domain im --no-wait --json`，具体域应对齐原授权范围/本次验证需要；输出中的授权URL原样展示并用官方`auth qrcode`生成二维码。device_code不公开，完成后由维护者调用官方poll。OAuth subject再核对可信主人绑定，不用OAuth结果自封主人。没有AppSecret/绑定时不执行授权或启动另一个监听。

旧catalog的保存不授予激活权。私有catalog和符合范围的历史需单独受控交接；当前软件包不带私人历史。中央只能导入明确授权scope的历史到同scope日志/对应catalog；不能把所有旧记录混进一个新thread。实际历史未交接前不得宣称连续性验收完成。

公开Git仅交接经审查的通用上游源码差异和脱敏契约/示例。原始内部交接归档、内部路径与运行清单、身份、授权、历史、配对随机值、网关信息及运行时二进制不得进入公开分支。私有配置与证据另走明确可用的私有机制或主人本地操作。

目标端源码、依赖、身份、OAuth、入口隔离验证完成后，经阿撒托斯/Shub协调旧桥排空并停止监听和refresh。确认旧端退出后才开启中央worker；训练、评测和Ali模型provider不动。先在已核验主人会话收发，用一次性测试文档验证读写，核对平台messageId回执。禁止向无关联系人发送或编辑任意文档。

`channel_events`原子绑定平台事件与已有message/chat；无第二队列。重复event不再执行模型或重发final。sending和sent/ambiguous区分模型结果与平台回执，模型已执行后断线不盲重放。取消只中断对应Runtime turn。排空等待正在执行/最终回执处理结束。没有exactly-once承诺：ACK丢失须先按平台回执核验。

回滚先停中央worker并保留correlation记录，再确认单消费者条件和旧端授权实际有效性，之后才恢复旧桥。新OAuth可能影响旧授权，不能假定复制密文或旧refresh token即可回切。回滚沿托管部署/授权cutover，只作用本集成，不重启其他工作负载。

## 验证

### 单会话历史导入

源端 `cthulhu.feishu_convert` 将明确审阅的单scope历史转换为下述输入，复用本导入器校验；命令、审阅格式、源文件边界和已验证范围见 [CONVERT.md](CONVERT.md)。`reviewed: true` 仅记录人工审阅，不构成自动脱敏或身份授权。

`venv/bin/python -m cthulhu.feishu_import --config TRUSTED_DISABLED_CONFIG --bundle REVIEWED_HISTORY_JSON --sha256 EXPECTED_FILE_SHA256` 默认仅校验，不访问数据库。维护者在切换前备份已有数据库，再追加 `--db TEMPLE_DB --apply` 原子导入；不新增公开RPC或传输入口。配置是 `{"feishu":{"enabled":false,"app_id":"...","owner":{"sender_id":"...","chat_id":"...","verified_by":"可信绑定证据","legacy_scope_id":"对应旧scope"}}}`，必要时 `owner.topic_id` 指定该主题。标识全部来自可信绑定及旧catalog映射，不能从待导入文本自证。

输入格式如下（仅虚构示例；sha256填写所选源文件真实哈希，另用CLI参数核对传输文件哈希）：

```json
{
  "version": 1,
  "event": {"app_id": "fixture-app", "sender_id": "fixture-owner", "chat_id": "fixture-chat"},
  "source": {"scope_id": "fixture-old-scope", "threads": [
    {"thread_id": "fixture-thread", "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "scope_ids": ["fixture-old-scope"]}
  ]},
  "turns": [
    {"source_id": "turn-1", "source_thread": "fixture-thread", "input": "已处理问题", "reply": "已完成结果", "status": "completed", "created_at": 1700000000}
  ]
}
```

源转换器只选绑定旧scope的codex catalog条目，通过实际CODEX_HOME内受限的选定thread查找导出经审阅的user/assistant正文或摘要。`scope_ids`记录该thread在源catalog中关联的全部scope；跨scope复用的thread必须暂缓，导入器拒绝。不能仅按角色过滤就宣称文本无秘密；不得导出全库、全部rollout、私有lastSummary或网关/密钥配置。多线程可合并按时间升序排列，source_id在各源线程内唯一；时间为Unix秒，status只接受completed/failed/interrupted。

上限32MiB、10000回合，超限拒绝而不截断。源scope/thread/hash、源回合到目标turn_log ID映射与内容摘要保存在已有settings的一次性完成回执；完全重复导入不重复写，不同包不能覆盖已有导入，已开始使用的scope拒绝后补。历史只写该scope的turn_log，backend=imported，不伪造channel_events、消息、身份、catalog或可恢复的旧线程。真正授权的首条平台事件到达后，现有check_scope才允许读取历史，通过现有有界handoff给新Runtime线程，原catalog按真实system事件更新。未激活scope不会因导入而获得权限，也不执行历史工具或发送回复。

`venv/bin/python -m unittest discover -s tests -v`；前端改动后`cd web && npm run build`；浏览器`venv/bin/python tests/ui_workflow.py`。test_feishu使用真实Node/Unix IPC/认证WebSocket/Runtime进程，模型与平台回执为fixture；不冒充真实飞书验收。现场源码收件、OAuth、主人会话与测试文档回执须另记证据。
