# 历史方案已被替代

中心直连 `company_codex` 方案已退役，配置会明确拒绝，不会静默使用个人Codex。
不得在中心放置公司key、网关地址或Ali provider配置，也不得新增Ali入站代理。

当前实现、授权边界、协议、验证和滚动部署步骤见 [Galaxy API Provider](galaxy-api.md)。
原有 `codex`、`claude` 身份、配置与个人线程保持不变。
