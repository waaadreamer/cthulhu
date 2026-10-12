# 飞书集成迁移源码交接

这里提供已有 bridge 定制行为的最小运行时差异和中心适配契约，不提供部署配置或可用的授权。迁移应延续现有机器人的身份和获准历史，中心角色承载同一代理；不要创建无关机器人，也不要更改其他代理或外部显示名。

基线是公开 npm 包 `lark-channel-bridge@0.7.0`，上游为 `https://github.com/zarazhangrui/feishu-claude-code-bridge`，MIT 授权文本见 `LICENSE.bridge`。`bridge-runtime.patch` 保留已存在的 blockedUsers 配置、访问过滤、管理命令和相关接口；这是现有定制行为的搬运，不是中心 harness adapter 的完整实现。

`manifest.json` 固定公开 tarball、原始及打补丁后的 `dist/cli.js` 哈希。生成的独立 bridge Web UI、开发生命周期/package-manager 元数据和 README 展示改动不在补丁内，因此打补丁结果不声称与原定制分发包逐字节相同。中心使用自己的 UI 和运行生命周期，不要启动独立 bridge Web UI 来配置未实现的 harness 模式。

中心准备 Node >=20.12、相应平台的官方 `@larksuite/cli@1.0.82` 及公开依赖；不需要转移运行时二进制或执行上游开发用 prepare/prepublish 脚本。保留依赖目录布局。校验 manifest 中的基线哈希后，在解包的 package 根目录应用补丁：

```bash
git apply --check /path/to/bridge-runtime.patch
git apply /path/to/bridge-runtime.patch
node --check dist/cli.js
```

再次核对输出哈希后，由中心实现方接入现有 runtime。不要把 `bridge run/start`、OAuth 或真实消息发送作为源码校验命令。

文件范围：

- `handoff.md`：真实执行接口、消息持久化、身份与会话隔离契约。
- `operations.md`：授权重建、切换与验收边界。
- `deployment-input.example.json`：未激活的实施输入示例，不是可直接启动的 bridge 配置。

本目录没有私有应用/用户/群/线程标识、配对短语、实际配置、密钥、OAuth、会话正文、机器路径或内部网络信息。历史和身份绑定只通过独立私密途径迁移。没有发现需要复刻的已安装飞书 skill；操作参考直接复用公开 CLI/bridge README，不在 skill 中建立新的授权规则。
