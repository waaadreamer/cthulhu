# 操作与验证边界

官方 lark-cli 是飞书 API 工具，不是事件监听器或任务调度器；复用现有 CLI，无需第二套实现。具体命令与身份参数查固定版本的公开 README/help/schema。业务能力存在不等于当前授权具备相应 scope 或文档 ACL。

在目标服务账户下建立独立 bridge home、CLI config/data 目录；目录700、机密文件600。只迁获准飞书配置字段，不迁模型网关配置、代理环境或整个代理home。模型接入复用既有 provider 机制，模型凭据留在原 provider 执行位置。

AppSecret 重建：原 bridge 密文由 hostname、username 和 salt 派生密钥，不能当作便携配置复制。已有 `secrets set --app-id <id> --profile <name>` 通过无回显输入本机加密，源码路径不联网。`profile create` 可用已有应用，但会调用 tenant token/bot-info 校验；不是离线检查。`profile export` 默认脱敏，显式含secret导出不包含OAuth或历史；没有通用profile import，migrate只处理旧布局。不要把secret放命令参数、普通日志或Git。

Linux CLI token 使用加密文件和master.key，默认数据目录与profile配置目录不是同一隔离边界。显式设置CLI配置/数据目录，优先目标profile重新进行device flow，由预期用户在可信浏览器确认身份与scope。它使用出站授权请求和轮询，不需要入站OAuth callback。两台机器不能同时刷新同一授权。

事件采用出站WebSocket；公开SDK先从 `https://open.feishu.cn/callback/ws/endpoint` 获取动态WSS地址。根域DNS/TLS可达不证明动态WSS、OAuth、业务或媒体请求可用。原默认消息去重在内存，不具备重启后的持久幂等性。

源码/依赖与离线契约验证通过后，才进入获准部署窗口。目标准备就绪前保留旧服务；先排空并停止旧消费者及自动重启，再启用目标唯一消费者和唯一刷新端。真实验证只使用已授权测试会话、可丢弃测试文档：入站、原线程回复、显式app/user身份、文档读改回读、重连/重放、取消、回执和跨scope记忆隔离。

回滚先停止目标消费者，再恢复原单一入口；保留来源event/request ID与实际回执，核对不明结果。重新OAuth授权可能影响原授权，不能声称恢复旧文件必然恢复用户能力。包传输或语法检查通过不代表迁移验收完成。

源码事实：/status忽略额外参数并回复状态，但任何已处理slash命令在回复后会取消当前scope的pending批次。普通DM scope为chatId，话题scope为chatId:threadId；不是全局取消，也不停止active run。debounce为600ms，服务没有现成只读pending快照。使用现有私聊命令作人工身份关联前必须确认当前会话无待处理输入；不得把它宣称为机器证明零副作用的内建配对协议。任何实际挑战、身份和取证材料均不属于本目录。
