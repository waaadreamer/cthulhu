# Galaxy API Provider v1

公司配置、公司key和公司HTTPS请求仅在Ali。Ali通过原有主动发起、已认证并校验证书的
galaxy WebSocket注册模型能力并处理请求。中心只持授权表和短期流队列，不新增Ali监听端口。
GPT 使用 `galaxy_api` 后端，Claude 模型使用原生 `claude` 后端加 `provider`；未知后端明确拒绝。
既有codex/claude配置与个人认证不变。

## 配置与选择

只有Ali本机galaxy.json加入：

```json
{
  "api_provider": {
    "codex_config_file": "/absolute/path/to/existing/private/.codex/config.toml",
    "codex_provider": "ss-gateway",
    "ca_file": "/etc/ssl/certs/ca-certificates.crt",
    "models": ["gpt-6-astra", "gpt-6.1-sol"]
  }
}
```

私有TOML须限制group/other访问；本机读取experimental_bearer_token或env_key。
也可用Ali本机base_url和api_key_file；这两个字段绝不上传中心。可选proxy也仅在Ali读取。
HTTP客户端显式验证CA和原域名，不跟随重定向，不继承不确定的全局代理环境。
注册前检查配置与CA非空；注册模型是宣告能力，实际可用性仍须canary。
没有模型请求会因为注册而自动发出。

中心temple.json只加入策略：

```json
{
  "api_providers": {
    "ali": {
      "models": ["gpt-6-astra", "gpt-6.1-sol"],
      "consumers": ["temple", "temple-ghroth", "ali", "korea", "work_notebook"]
    }
  }
}
```

实际模型目录是在线注册与中心allowlist的交集。角色运行配置：

```json
{
  "backend": "galaxy_api",
  "provider": "ali",
  "model": "gpt-6-astra",
  "effort": "medium",
  "service_tier": "default",
  "fallback": {
    "backend": "codex", "model": "gpt-6-astra",
    "effort": "medium", "service_tier": "default"
  }
}
```

目标agent须在线且声明galaxy_api_v1，否则中心拒绝下发新后端。
模型须由目标星系获授权的在线provider提供。provider断连立即撤下目录。
网页展示请求档位和实际终端档位；fast仅是请求，Ali此前completed返回default，
因此不能称fast已经兑现。缺失实际model/tier保留unknown。

## 公司 Claude 原生模型

Ali 的既有 `api_provider` 增加公开模型与本机配置路径，保留原 GPT 模型：

```json
{
  "models": ["gpt-6-astra", "gpt-6.1-sol", "claude-opus-5-5"],
  "model_backends": {"claude-opus-5-5": "claude"},
  "claude_config_file": "/absolute/path/to/private/.claude/settings.json"
}
```

中心仅在 Ali 的模型授权表加入 `claude-opus-5-5`。Ali 本机 settings.json 必须禁止
组和其他用户访问；读取其中 env.ANTHROPIC_BASE_URL 与 ANTHROPIC_AUTH_TOKEN
（或 ANTHROPIC_API_KEY）。地址、令牌与文件内容均不登记到中心。
Claude-only provider 无需提供 Codex 配置。

在线目录的 `model_backends` 指定原生执行后端。现有配置选择入口据此保存：
`{"backend":"claude","provider":"ali","model":"claude-opus-5-5"}`。
不改变现有模型映射、用户默认或显式 fallback。provider 与消费者均须声明
`galaxy_api_claude_v1`；旧消费者不能接收此配置，以免忽略 provider 而使用个人认证。

沿用 ClaudeRuntime、Claude Agent SDK 与原生 CLI；CLI 可使用 SDK 自带可执行文件。
复用同一个 GalaxyAPITransport 和 Broker/Worker 的请求、背压、取消及恢复机制，
原样转发 Anthropic Messages/SSE，无 Responses 转换。Claude 的工具仍在消费者本地执行。
CLI 使用独立 CLAUDE_CONFIG_DIR 和 loopback 随机令牌，清除继承的上游凭据与
其他云厂商路由开关。公司凭据仅由 Ali 注入。仅透传经过字符校验的 anthropic-beta
协议头，其余调用方 HTTP 头不进入公司请求。

Claude CLI 的请求重试及非流式补发均关闭，初次加五次重试仍由原共享恢复策略负责。
配置了后备才会使用后备，不创建隐式 Codex fallback。

## 协议、额度与故障

既有hello/welcome保留兼容性；新增capabilities、version和api_version。
旧temple不宣告api_version时，新galaxy不注册provider；旧消费者不能收到新后端选择。
公司信息不会进入hello/登记、错误文本或中心配置。

- api_register：认证galaxy提交version=1、protocol=responses、模型列表和代码版本。
  神殿按连接身份核对策略，忽略任何客户端宣称的调用方身份。
- api_catalog：返回调用方星系获准使用的在线provider及模型。
- api_open：provider ID、原生 protocol（responses/messages）与完整 JSON；神殿生成随机请求ID并绑定当前caller连接和provider连接。
  未知模型、未授权/离线provider、缺少客户端能力均拒绝。
- api_request事件：经原galaxy WS送达Ali；请求仅包含模型协议数据，不含URL/header或key。
- api_push：provider逐块上传base64 SSE，携带递增seq；每块等待确认。
- api_next：caller单读者拉取下一块；相关请求独立，不混合并发流。
- api_cancel / api_cancel_ack：取消唯一请求，Ali取消并等待HTTP上下文退出后确认；
  返回upstream_closed仅在收到匹配provider确认时为true，超时不会伪称已关闭。
- 断连：所有绑定该旧连接的流终止；重新认证后不得接收旧请求或自动重发。
  失效agent epoch导致连接撤销，因此同样不能把迟到响应注入新线程。

单请求4MiB，块64KiB，单SSE事件4MiB，单流32MiB；队列4块；
全局32流、每provider4流、每caller2流；空闲90秒、总时长600秒。
消费者不拉取时阻塞provider并有超时；总时长后释放遗留槽位。
一条连接最多128个处理中调用。错误只传固定类别，不传上游错误正文或HTTP头。
Ali还会防御性去除上游事件中已知key的回显。

客户端Codex使用仅绑定127.0.0.1的桥接服务、随机本地令牌和独立CODEX_HOME；
其本地令牌不是公司key。目录为agent状态目录下galaxy-api/provider哈希，
线程标签包含backend/provider/model。原个人线程标签不变。
Codex继续执行本机原有工具；Ali只推理，不获得跨星系工具执行权。
工具定义、function_call、function_call_output、完整历史与加密reasoning项由协议透传，
不依赖previous_response_id。Codex的provider HTTP/stream重试设为0。

失败恢复复用共享策略：可重试错误先在当前源执行初次尝试加5次重试，再使用已配置后备。
再次推理前必须等待原执行及工具结果落定，读取持久化结果和已检查上下文；不重放消息入站或工具调用。
认证、权限等不可重试错误直接失败；取消立即停止恢复。原执行状态无法确认时，不发起新的推理。
后备使用独立线程和backend标签。
回退保持30分钟后，后续任务尝试主后端并重新验证在线能力。
这不提供任意shell工具的业务exactly-once保证，也不承诺取消后零计费。

## 验证入口

`venv/bin/python -m unittest discover -s tests -v` 包含：
真实认证WS+本地模拟HTTP上游链路、真实打包 Codex/Claude CLI 动态工具和结果续轮、
Claude 六次实际失败请求后切换显式原生后备、认证错误不重试、
多请求相关性、终端实际tier、错误脱敏、取消确认、断连重连、排空、授权与限额。
这些测试不是Ali公司模型的现场验收。

只有本机维护身份持temple私有配置时可运行：

```sh
venv/bin/python -m cthulhu.ops --config /etc/cthulhu/temple.json status
venv/bin/python -m cthulhu.ops --config /etc/cthulhu/temple.json drain --galaxy temple
venv/bin/python -m cthulhu.ops --config /etc/cthulhu/temple.json check --galaxy temple
venv/bin/python -m cthulhu.ops --config /etc/cthulhu/temple.json resume --galaxy temple
venv/bin/python -m cthulhu.api_canary --config /etc/cthulhu/temple.json --provider ali --mode full --output /private/path/canary.json
```

维护连接限定loopback，用独立HMAC域认证，仅允许状态、排空和API canary，不允许消息、
spawn、任意RPC或provider登记。没有冒用现存agent/galaxy连接。
canary缺少provider时不生成；默认full最多5个公司请求（两模型短文本、工具及续轮、单请求取消），
echo仅在内存执行一次。生产provider的WS不做故障注入；断开provider的回归在隔离测试完成。
默认检查两个GPT模型；可重复传入 `--model <已发布模型名>` 指定检查范围。
指定单模型时，full共4个请求，文本、工具调用、携带工具结果的续轮和取消均使用该模型。
任何指定模型未在目录发布时，整次检查生成0个请求；报告保留请求模型与上游返回模型，不能据别名推断实际模型。
跨星系text canary可经维护连接maintenance_canary指定已升级galaxy，固定最多2个短请求。
不接受远程shell或任意脚本。

### Claude 原生验收入口

```sh
venv/bin/python -m cthulhu.api_canary --config /etc/cthulhu/temple.json --provider ali --native-claude --model claude-opus-5-5 --mode full --output /private/path/native-claude.json
```

此入口实际构建既有 ClaudeRuntime，启动 SDK 原生 CLI，并执行一次无副作用 echo 工具。
工具返回随机值，提示词不包含该答案；必须观察到原生工具调用、流式文本和准确返回值，
以及上游返回模型符合预期，才算通过。请求别名与实际型号分开记录；已独立确认映射时，
用 `--expected-returned-model Claude-Opus-5.5-P017` 显式指定实际型号，否则默认要求与请求名相同。
任意一次推理返回非预期型号均失败，不改写上游返回值。推理限制为最多两次上游请求；full 另外发起一次
Messages 请求并核验定向取消回执。text 模式仍包含原生工具检查，省去最后取消请求。
报告保留 backend、runtime、provider、请求 ID、requested/returned model 和工具执行次数。
取消是传输层验收；断连故障与重试使用隔离测试，不能中断生产 provider 来制造错误。
普通 Responses canary 遇到 Claude 模型在发送前拒绝，避免误报为 Claude-native 运行。

## 发布与回滚

部署授权不等于可以终止活跃任务。维护排空暂停新消息投递和新巡检；新agent会停止取下一批，
已在执行的主回合、记忆整理和巡检继续完成。安全检查同时要求无前台/后台模型调用。
队列保留。旧agent没有drain_v1时明确返回不安全，首次升级须由现场执行者检查任务边界，
不能把旧idle字段当作完整排空证明。

1. 备份代码与各host本机配置；配置备份0600、目录0700，不进git。
2. 先运行完整测试和web构建。先更新temple的向后兼容路由/能力门及策略：
   重启temple只让控制WS重连，不杀模型回合。无provider时不显示新后端。
3. Ali现场确认评测进程归属与安全任务边界，再更新galaxy/agent和Ali本机配置。
   不能杀生产WS来测试断连；不能复制个人OAuth或公司key。
4. 注册后先跑隔离canary，再逐个升级消费者并读取运行进程报告的版本/能力，
   最后才选galaxy_api。分别处理中心systemd、Ali用户systemd和韩国nohup监督。
5. 离线work_notebook标pending，重新上线后补部署/验证，不能宣称全星系完成。

中心服务重启galaxy会终止其当前agent回合，因此只在排空且确认业务进程安全后执行。
ghroth服务最后用延迟任务重启，避免中途杀维护回合。
Ali现场确认由用户systemd单元cthulhu-galaxy.service启动~/.cthulhu/run.sh，
KillMode=control-group会终止其受管agent；必须等执行者自身回合及后台工作结束，
并核验整个单元cgroup后，才可由独立于该单元的现场执行上下文重启用户单元。
韩国现场为nohup/run.sh循环；保留经核验的wrapper，仅在排空放行后定向重启galaxy子进程，
执行前重验PID/starttime/PPID/源码及配置，不匹配即停止。
不能全局pkill；现场先备份并核实启动器和代码根，再针对该实例维护。
保留既有未跟踪文件（如cthulhu.egg-info），不使用git clean。
不能通过旧read_file RPC执行代码，也不能冒用现有galaxy凭证开第二条连接作为部署捷径。

回滚先停止新API选择并排空，把角色显式恢复codex，再恢复配置/代码；
保留新线程和审计资料，不删除数据库或队列。已经可能发出的请求不得自动复制到个人。
temple单独回滚前先停止新消费者/provider，避免新能力依赖丢失。
