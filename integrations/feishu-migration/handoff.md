# 中心运行契约

保留已有 Feishu channel 的事件标准化、附件、访问过滤与出站回复。将旧 CodexAdapter 推理路径接入现有 harness，避免同时运行 bridge Codex 与中心代理。配置和工厂目前仅支持 claude/codex，必须明确实现新模式；不能通过改名字或伪装 Codex JSONL 可执行文件声称完成。

可复用执行边界：bridge CodexAdapter.run 接受 runId、cwd、prompt、threadId、images、model、sandbox、stopGraceMs，返回异步 events、stop 和 waitForExit。事件包括 system/threadId、text/delta、tool_use/tool_result、error、done；结束原因 normal/failed/interrupted。中心复用 `Agent.run(thread,text)` 与 `Runtime.start_thread/resume_thread/run_turn/interrupt`，向 `ensure_thread/run_main` 传入明确 scope 选择的 thread。旧进程 SIGTERM/SIGKILL 不等于中心任务取消成功。

中心生命周期管理本机 transport driver，用一条已有 agent Link。没有现成 channel IPC 插件，需要新增窄接口；不使用主人 cookie、owner 全局事件流、同 agent ID 的第二条连接，或另造跨主机消息总线。角色注册、权限和恢复沿用 roles/gods/现有工具机制。

身份与历史：

1. 旧 allowlist、OAuth subject、app creator 或 catalog 记录都不自动证明当前主人或委托。默认只激活已验证主人和明确授权路由；其余记录保留但休眠。来自其他人的内容仍是外部输入。
2. 复用 catalog 的 scopeId+agentId+cwdRealpath+policyFingerprint 映射。不能让单例的一个 backend thread 汇集所有人的聊天。旧路径迁移与实际 thread 关联需要明确转换，不打包整个工作区或代理 home。
3. turn_log、handoff_context、instructions、memory_delta、after_turn、fallback 和线程轮转均按同一授权范围过滤。主人永久记忆及 OAuth/工具不能因为共用进程、profile 或 defaultAs=auto 而对其他发送者开放。

消息与回执：

- 输入包括来源应用命名空间、原始事件 ID、scope、sender、回复定位、正文或媒体引用。由中心核验来源与委托，不能接受调用者自报 owner=true。
- 在既有 messages 存储中使用确定的来源唯一键，重复投递返回原记录；保留 task_id/reply_to，沿用现有生命周期。不要建立平行任务/会话状态机。
- Link 的待完成调用不跨断连保留；handled 不等于模型或业务成功。最终 done/failed/interrupted 必须关联实际出站 request_id 与飞书回执。
- 结果未知的发送/编辑不能盲重试。先查已记录回执和对应操作结果；不能假设所有飞书接口天然幂等。取消不撤销已成功的外部操作。
- 只回传该 scope 的显式答复。旧 /cd、Codex resume、模型切换及会议运行分支不能继续驱动旧代理；/new、/status、/stop 映射到实际中心状态。验收后删除被替代的执行分支。

补丁中的 app-owner recovery bypass 只是旧 bridge 的行为，不能取代中心可信主人绑定；新的来源授权门必须覆盖每个输入与工具操作入口。
