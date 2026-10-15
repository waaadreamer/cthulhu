# 已验收 v3 三任务统一候选

本文件是审核后的执行清单，不代表操作已经执行。首次正式 v3 → main 已单独完成：
main 从 2b1959987f4eece35ea66e3b7a01d4aacb51697e 快进到 fb48835320ef4e6095b41bf9e8446534e5bd792c，
archive/main-before-v3-20261015 保留旧 main，远端 v3 没有改动。

## 合并清单

| 功能 | 已验收 tip | 完整历史 |
| --- | --- | --- |
| 部署状态与资格 | 625ed8a6a35ffc258e29ed50297bcd605a1ef393 | fb48835 → 734c56b → 625ed8a |
| Official 路由、能力、默认 | 068716509c3d636a5eaacfe0721a0da8a85e7f8b | fb48835 → 425c7ac → 95088ad → 69c4f81 → 8591e4c → 0687165 |
| 任务短名与每期过程汇报 | ebf63576361cc036f5d81a5cd2516525a3bdb13a | fb48835 → f1baa00 → 9277ec2 → 0e641e2 → ebf6357 |

独立仓库 `/var/lib/cthulhu-v3test/work/v3-release-integration`，分支 `integrate/approved-v3`。
两次 no-ff merge 保留所有提交。源码无文本冲突；生成 static 的 rename/content 冲突统一使用部署分支作为临时基线，
在三条源码合完后只运行一次 `npm run build`。中间两次 merge 不是可部署候选，最终构建提交才是审核产物。
`temple.py` 同时保留摘要/管理操作、Official 默认与 deploy_latest；`web/src/api.ts` 同时保留任务字段、模型标签和部署类型。
既有回滚、排空、权限与任务十分钟汇报机制均保留；不扩大本次测试 timeout。

## 上线前顺序

1. 审核最终干净 HEAD、构建与交叉验证结果。普通权限打包最终 HEAD；由已有发布身份 fetch 最新 GitHub main，
   必须先确认它是最终候选祖先。若有并发提交，重新合并和针对变化验证，禁止 force/squash 丢失现有运行版本的祖先关系。
   统一候选的推送与首次 fb48835 推送是独立步骤，目前尚未推送统一候选。
2. 协调中心及 test/az-test/ali/korea 的 API consumer 安全边界；通过现有 maintenance_drain 等待回合、整理和工具自然结束。
   provider 重启还必须确认全 consumer 暂停新请求、active_streams=0。不能仅因为 ali 自身排空就重启共享 provider。
   本机执行者必须先移出即将重启的服务，发布由管理者在批准窗口执行。
3. 路由阶段 A：阿里执行者负责实际 provider 代码/catalog/配置生效。本机只按审核增量更新中心 allowlist，保留 old_one_runtime
   和所有 overrides 原值。当前集成代码虽然包含新代码默认，生产配置和星系/个人覆盖继续保护阶段 A；不得提前清理。
4. 切换本机三个 unit 到同一个真实 Git checkout，配置 repo 与 checkout.origin 都指向真实 GitHub main。
   先验证中心/本机星系版本与功能；路由实际可用后再执行阶段 B 默认迁移。推 main 不等于服务/默认已上线。
5. 历史短名映射在停止中心写入的窗口再次 dry-run，13 项映射须覆盖执行时全部待命名任务；缺项则停下请管理者补齐。
   幂等应用后再启动中心。新任务 ID 及原始描述/消息均保留。

## 本机目录和服务

| unit | 用户 | 当前代码目录 | Python |
| --- | --- | --- | --- |
| cthulhu-v3test-temple.service | cthulhu | /opt/cthulhu-v3test | /opt/cthulhu/venv/bin/python |
| cthulhu-v3test-galaxy.service | cthulhu | /opt/cthulhu-v3test | /opt/cthulhu/venv/bin/python |
| cthulhu-v3test-azgalaxy.service | azgalaxy | /opt/cthulhu-v3test | /opt/cthulhu/venv/bin/python |

现有 ExecStart 分别为 `-m cthulhu temple --config /etc/cthulhu-v3test/temple.json`、
`-m cthulhu galaxy --config /etc/cthulhu-v3test/galaxy.json`、
`-m cthulhu galaxy --config /etc/cthulhu-v3test/azgalaxy.json`。
本次无 pyproject.toml 变化，不修改 v2 的共享 venv。保留各自 state、token 与 DB。

目录准备、具名 systemd drop-in、配置备份、健康验收与移除 drop-in 的精确命令复用
[deploy-status-recovery.md](deploy-status-recovery.md) 的阶段 1/2；执行时 source 必须改成此次统一仓库，
reviewed_rev 必须是此次统一最终 SHA，不能继续使用部署单分支。
候选目录 `/opt/cthulhu-v3test-reviewed`，由 cthulhu 拥有并可写 Git metadata；azgalaxy 只读即可，中心作为共享组更新者。
三个 unit 同时使用该 WorkingDirectory 和 PYTHONPATH，不能只改 deploy.code_root。
配置文件改动应用于执行时最新内容，不覆盖阶段 A 之后的并发配置更新；保留备份所有权和 0600 权限。

准备审核包（普通用户，可重复创建新文件但不能覆盖已有审核包）：

```bash
set -eu
release_repo=/var/lib/cthulhu-v3test/work/v3-release-integration
test -z "$(git -C "$release_repo" status --porcelain)"
git -C "$release_repo" rev-parse HEAD
git -C "$release_repo" bundle create /var/lib/cthulhu-v3test/work/v3-integrated-reviewed.bundle HEAD
```

## 短名迁移的精确步骤

先只读预检（当前已验证13/13、missing=[]；发布时必须重做）：

```bash
release_repo=/var/lib/cthulhu-v3test/work/v3-release-integration
/opt/cthulhu/venv/bin/python "$release_repo/scripts/task_short_names.py" \
  --db /var/lib/cthulhu-v3test/temple/temple.db \
  --mapping "$release_repo/docs/task-progress/task-names-20261015.json"
```

以下由已审批 root 会话执行，且所有排空/active_streams 门槛已满足。先停止中心写入，SQLite backup API包含WAL的一致状态；
不直接复制正在写入的 .db。此停止用于迁移窗口，后续按目录切换方案启动三个本地服务：

```bash
set -eu
systemctl stop cthulhu-v3test-temple.service
sudo -u cthulhu python3 - <<'BACKUP'
import sqlite3, os
from pathlib import Path
src=Path('/var/lib/cthulhu-v3test/temple/temple.db')
dst=Path('/var/lib/cthulhu-v3test/temple/temple.before-integrated-v3.db')
fd=os.open(dst,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.close(fd)
with sqlite3.connect(src.as_uri()+'?mode=ro',uri=True) as a, sqlite3.connect(dst) as b:
    a.backup(b)
BACKUP
release_repo=/opt/cthulhu-v3test-reviewed
sudo -u cthulhu /opt/cthulhu/venv/bin/python "$release_repo/scripts/task_short_names.py" \
  --db /var/lib/cthulhu-v3test/temple/temple.db \
  --mapping "$release_repo/docs/task-progress/task-names-20261015.json"
# 人工确认missing=[]；脚本apply也会重新检查并拒绝遗漏。
sudo -u cthulhu /opt/cthulhu/venv/bin/python "$release_repo/scripts/task_short_names.py" \
  --db /var/lib/cthulhu-v3test/temple/temple.db \
  --mapping "$release_repo/docs/task-progress/task-names-20261015.json" --apply
```

13项映射为管理者已批准内容，不凭任务prompt自动补名。发布时若有新增缺项，保持窗口并请管理者补齐；
若放弃窗口则恢复原中心服务，不继续apply。应用/重放幂等性已有专项覆盖，不在生产额外写入来证明幂等。

## 默认迁移及执行时绑定刷新

复用 [official-opus-release.md](official-opus-release.md) 的阶段 A/B；精确配置/回滚命令和私有快照在
`/var/lib/cthulhu-v3test/work/opus-official/deployment-commands.md`、`runtime-migration-plan.json`。
这些文件中的“5个在途pin”是旧快照，不能整份直接执行。
`old_a65a95efe779c00a` 与 `old_b95906f5af1271f0` 的任务均已结束；执行时仍空闲就继承新默认，不新增 Astra pin。
执行前重新读取 bindings 与 runtime 实际配置，只对仍在途且被明确要求 Astra 的执行者保留/新增必要覆盖。
本部署任务的 `old_4960a164ab8bea76` 继续 Astra 至本任务安全交接。每项按既有 runtime API校验/通知，不能写库绕过。
不改管理者、枢纽和总结模型；stopped 执行者保持 stopped。确认 actual runtime及下一次api_result，不以仅收到POST成功为准。

## 验收与失败回滚

- `/health` HTTP200；temple/test/az-test 的 root 一致为候选目录、revision一致为最终 SHA，真实 origin/main包含最终候选。
  cthulhu writable=true，中心共享组可更新。当前候选领先尚未发布main时显示真实不兼容原因，不能伪造按钮资格。
- 目录在线与原任务绑定保持，检查每任务独立过程行、两期汇报、更正/依据展开、历史短名、断线重连；
  部署状态能显示真实版本/来源与禁用原因，不调用全部部署作为验收。
- 路由：两公开模型ID/backend/catalog正确，使用阿里已验收真实闭环证据并在实际发布后检查该进程版本/catalog。
  Web Search不支持是主人接受的能力边界，不额外探测、不阻挡发布。
- 阶段A失败保持旧默认；恢复代码与对应中心/阿里配置成对备份，维持排空到旧版本、连接与请求恢复。
  本执行者不操作远端；阿里 provider恢复仍由其授权执行者及原监督器负责。
- 本机失败按deploy-status-recovery的具名drop-in回滚，恢复配置和原非Git目录，版本应重新显示未知。
- 若短名迁移后中心还未重新接收任何写入，可在服务停止时用上述一致DB备份恢复。
  已恢复运行并产生新消息后禁止直接整库回滚覆盖新消息；保留新增兼容列/索引，先回滚代码/配置，
  若必须改名称则由管理者审核定向修正，不自动丢弃期间任务记录。
- 阶段B失败先保留新路由，通过原runtime API逐项恢复本阶段实际改动前的overrides/配置，确认安全边界及实际旧runtime，
  再决定是否撤回路由；不覆盖整库、不遗留临时pin。

本文件不授权执行任何尚未审核的服务重启或配置安装。实际操作与回执另附任务交付证据。

## 统一候选验证（2026-10-15）

源码合并点e790566：交叉回归87项/98.192s，OK(skipped=1)；覆盖部署状态/回滚/实际更新与排空、
provider/runtime、任务摘要/检查点、完整probe→review→close→reuse生命周期。
唯一跳过为隔离仓库未捆绑原生Codex，不能宣称该条实际执行；阿里独立真实Official闭环已有单独验收。
日志末尾有测试fixture procs.log未关闭的ResourceWarning，测试已正常exit0；未因此修改生产逻辑。

统一前端build49.62s成功（仅既有chunk体积提示）。真实合并组件部署6场景、模型设置2场景均PASS；
构建页面真实HTTP/WS浏览器PASS，覆盖任务隔离/两期/迟到纠正/动作追溯/重复事件/重连/刷新/侧栏。
本机日志：v3-release-cross-tests.log、v3-release-build.log；浏览器证据：v3-release-browser/。
逐函数AST核验保留17个UI变更方法、deploy_latest原逻辑、Official默认；TS编译确认共享类型合并有效。

另核实维护窗口限制：当前ProviderBroker.open没有全局maintenance门禁，Hub的检查点GalaxyAI未被agent drain暂停；
普通drain+active_streams=0只是当时快照。管理者15:07已决定首次切换采用短暂中心离线窗口/阿里独立有界程序，明确接受最后快照至停服间的后台检查点小窗口失败，由现有恢复处理；不能把agent drain描述成原子API暂停。训练进程保持不动。
本机一次性环境脚本位于工作目录v3-release-prepare-root.sh、v3-release-cutover-root.sh、v3-release-rollback-root.sh，
由管理者经sudo_request和独立systemd transient oneshot执行；不引入产品内第二套部署状态机。
准备脚本校验bundle SHA与最终Git SHA、不改服务；切换脚本停中心前验证映射完整性，失败trap恢复原配置/drop-in并启动旧服务。
脚本SHA、固定调用参数、日志及实际回执随审核提交；仅bash -n通过不等于已执行生产迁移。
