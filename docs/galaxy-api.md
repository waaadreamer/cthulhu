# Galaxy API Provider v1

公司配置、公司key和公司HTTPS请求仅在Ali。Ali通过原有主动发起、已认证并校验证书的
galaxy WebSocket注册模型能力并处理请求。中心只持授权表和短期流队列，不新增Ali监听端口。
客户端使用独立后端 `galaxy_api`；未知后端（包括退役的company_codex）明确拒绝。
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

## 协议、额度与故障

既有hello/welcome保留兼容性；新增capabilities、version和api_version。
旧temple不宣告api_version时，新galaxy不注册provider；旧消费者不能收到新后端选择。
公司信息不会进入hello/登记、错误文本或中心配置。

- api_register：认证galaxy提交version=1、protocol=responses、模型列表和代码版本。
  神殿按连接身份核对策略，忽略任何客户端宣称的调用方身份。
- api_catalog：返回调用方星系获准使用的在线provider及模型。
- api_open：provider ID与完整Responses JSON；神殿生成随机请求ID并绑定当前caller连接和provider连接。
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

失败回退采用保守边界：仅在请求尚未可能发送时允许自动重试个人后备。
api_open开始后即视为可能已发送，即使响应尚未出现也不自动重放；工具执行后更不重放。
失败保留明确状态，人工可在了解执行情况后发起新任务。后备使用独立线程和backend标签。
回退保持30分钟后，后续任务尝试主后端并重新验证在线能力。
这不提供任意shell工具的业务exactly-once保证，也不承诺取消后零计费。

## 验证入口

`venv/bin/python -m unittest discover -s tests -v` 包含：
真实认证WS+本地模拟HTTPS客户端链路、真实打包Codex动态工具和第二轮对话、
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
canary缺少provider时不生成；full最多5个公司请求（两模型短文本、工具及续轮、单请求取消），
echo仅在内存执行一次。生产provider的WS不做故障注入；断开provider的回归在隔离测试完成。
跨星系text canary可经维护连接maintenance_canary指定已升级galaxy，固定最多2个短请求。
不接受远程shell或任意脚本。

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
   最后才选galaxy_api。中心systemd和Ali/韩国nohup监督必须分别处理。
5. 离线work_notebook标pending，重新上线后补部署/验证，不能宣称全星系完成。

中心服务重启galaxy会终止其当前agent回合，因此只在排空且确认业务进程安全后执行。
ghroth服务最后用延迟任务重启，避免中途杀维护回合。远端run.sh循环会重启galaxy，
不能全局pkill；现场先备份并核实PID/父子关系、启动器和代码根，再针对该实例维护。
不能通过旧read_file RPC执行代码，也不能冒用现有galaxy凭证开第二条连接作为部署捷径。

回滚先停止新API选择并排空，把角色显式恢复codex，再恢复配置/代码；
保留新线程和审计资料，不删除数据库或队列。已经可能发出的请求不得自动复制到个人。
temple单独回滚前先停止新消费者/provider，避免新能力依赖丢失。
