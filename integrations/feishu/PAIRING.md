# 旧桥停止后的单次主人配对

配对复用现有官方channel WebSocket接收与bridge本机加密秘密协议；没有第二模型/消息引擎、HTTP接收服务或daemon。它是前台、单次、有限时长的维护命令。普通Feishu路由和worker仍关闭。

启动条件：已在服务账号专用root内录入原应用AppSecret、配置；实际协调确认旧listener和refresh均停止，原机器人不会被悄悄重启。`--stop-receipt`指向维护者根据真实停机回执落盘的私有JSON，字段为`app_id`、`listener_stopped:true`、`refresh_stopped:true`、`verified_by`、`task_id`、`stopped_at`（Unix毫秒）。这只是既有协调的记录，不是要求主人再次批准。

```sh
NODE integrations/feishu/pair.mjs --root PRIVATE_ROOT --install-root INSTALLED_DEPENDENCY_PREFIX --stop-receipt PRIVATE_STOP_RECEIPT --profile nyarl
```

root必须由当前服务账号拥有且0700；`config.json`是原bridge的profile配置，`temple-feishu.disabled.json`保持`feishu.enabled=false`及`worker.enabled=false`。程序拒绝已有`pairing-evidence.json`覆盖，维护者须先核验旧记录，不能盲删重配。通过既有proper-lockfile防止本机重复配对接收。此锁不代替跨主机停机协调。

只有官方channel的`connect()`确认WebSocket握手成功后才生成24字节随机值，以“奈亚配对 …”完整短语输出当前控制台并记录到0600的`pairing-ready.json`。有效期从接收就绪起60分钟。此时主人在**原应用的私聊**发送这条当前短语；接收来自中央配对进程，不是已停止的旧模型桥。准备阶段不生成短语、不让主人提前发送。

只接受窗口内、原始事件与规范化事件一致的纯文本p2p用户消息；要求open_id，拒绝bot/group/topic/转化后才匹配的消息、过期或未来时间。nonce必须原样相同。重复平台消息去重；首个匹配后留5秒收集冲突，出现不同sender/chat则拒绝。到期、中断、连接错误或成功均断开接收；没有发送方法调用，不开启正常路由、不运行模型。

成功后只写0600的`pairing-evidence.json`：app/sender/chat/message/event时间与ready/expiry/verified_by；不写秘密或配对短语。p2p旧scope按当前bridge原生规则等于chatId。随后维护者将该可信映射合入已有禁用配置的owner和legacy_scope_id，保持开关关闭，执行官方OAuth与授权历史迁移。关闭时ready文件改为closed，不把遗留ready文件当作活跃接收证明。

实际准备路径和账号见部署任务私有交接，不进入公开仓库。身份不能从旧allowlist、应用creator或OAuth subject自行推断。OAuth在绑定和AppSecret就绪后通过官方 `auth login --domain docs --domain drive --domain im --no-wait --json` 发起；device_code私有保存，向主人仅展示原样官方验证URL/二维码，后续使用 `auth login --device-code … --json` 完成。CLI没有独立的`auth poll`子命令。OAuth主体还须核对已建立的绑定。之后保留唯一消费者并按原任务进行真实收发、测试文档、历史和回滚验收。

测试仅使用虚构事件、官方normalizer及替代通道；覆盖就绪前不发短语、原始事件校验、窗口/歧义/去重、超时/断开、0600输出、无发送和缺先决条件不启动。实际平台握手及主人绑定要在真实条件齐备后另行确认。
