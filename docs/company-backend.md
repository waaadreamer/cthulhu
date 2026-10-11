# 公司 Codex 后端

新增后端标识为 `company_codex`，原 `codex` 保留个人认证、配置及线程。
两者复用 Codex app-server 协议，各有进程、日志和线程标签；公司进程使用单独的 CODEX_HOME。
同一 OS 用户仍能访问双方文件，这属于运行配置隔离，不是用户级安全边界。
若要求凭证互不可见，须另设执行用户/galaxy。

本机 galaxy JSON 新增以下传输配置；网页运行配置不接受这些字段，也不包含凭证：

```json
{
  "company_codex": {
    "codex_home": "/var/lib/cthulhu-company/codex",
    "base_url": "https://ai-gateway.prod.shu.team/v1",
    "api_key_file": "/etc/cthulhu/company-codex.key"
  }
}
```

由执行用户拥有专用目录（0700）和单行 key 文件（0600）。不复制个人 auth.json。
专用目录必须已存在，不得等于现有 CODEX_HOME（包括符号链接别名）。
公司进程通过命令行非敏感覆盖强制选择公司 provider、Responses 和 HTTPS；
key 只注入公司子进程环境，不写入命令参数、线程设置或网页。
公司进程去除继承的个人 OpenAI 环境配置；个人进程不继承 COMPANY_CODEX_API_KEY。
模型的 shell 与其运行进程使用相同执行身份，不能把环境过滤当作凭证访问控制。

角色运行配置（可由网页设置，也可作为 galaxy 的 runtimes 默认项）：

```json
{
  "backend": "company_codex",
  "model": "gpt-6-astra",
  "effort": "medium",
  "service_tier": "default",
  "fallback": {
    "backend": "codex",
    "model": "gpt-6-astra",
    "effort": "medium",
    "service_tier": "default"
  }
}
```

公司 Sol 可设置 `model=gpt-6.1-sol`，申请 fast 可设置 `service_tier=fast`。
模型列表是选择建议，不代表网关已支持；任意模型名仍需实测。
fast 经 Codex 映射为 priority 请求，不代表网关实际以该档位服务。
关闭 fast 请显式选择 default（省略字段会沿用 Codex 的配置/线程默认值）。
参见 [OpenAI 配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)。
本机 0.160.1 schema 已确认 thread/start 的 modelProvider 和 turn/start 的 serviceTier 字段。

主后端启动或回合失败时使用配置的后备，保持 30 分钟后在后续回合重试。
回退会重放原输入及最近交流，不保证失败回合中已经执行的工具无副作用；
这延续原有回退语义。未配置后备且新运行配置无法启动时，保留原运行配置。
不同后端不互相 resume 线程，个人旧线程继续保留。
更换公司端点/账号时使用新的专用 home，避免恢复旧 provider 会话。
同用户读取历史会话及跨后端交接的历史内容仍是已授权任务上下文。

## 部署顺序与影响

1. 备份代码、拟修改的本机 galaxy 配置及运行设置，保留原个人 CODEX_HOME。
2. 先完成中心 DNS/路由、TLS、凭证授权和有限协议测试；结果须包含实际模型、
   实际 service tier、SSE completed、函数调用及结果回传。HTTP 200 或传入 priority 不够。
3. 放置专用 home/key，向所需 galaxy 本机配置加入 company_codex 块。
4. 等目标 galaxy 所有 agent 空闲，再重启其服务加载代码。重启 galaxy 会终止当前回合，
   不能在活跃工作期间执行；独立 ghroth 服务也需要在维护回合结束后延迟重启。
5. 所有目标 agent 支持新后端后，重启 temple 加载验证器和模型列表。
   temple 重启会使 WebSocket 短暂重连，但不终止模型回合。
   新前端会在旧 temple 上隐藏公司和 fast 选择，避免混合版本误选。
6. 先用隔离 canary 验证公司后端与个人 Astra 回退，再切换正式角色。
   更新在回合边界生效；无需更改个人 provider。

回滚先在回合边界把运行设置改回 codex，确认个人线程可继续，再移除公司设置和恢复代码，
按相同空闲规则重启。保留公司目录和日志用于审计，不删除队列、任务、数据库或个人认证。
