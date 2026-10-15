# 部署状态与非 Git 发布目录恢复

此修复只准备代码和方案，不执行生产部署。发布由维护者审核后通过已有维护窗口执行。

## 展示与资格

| 条件 | 版本与按钮 |
| --- | --- |
| 正常 Git 检出、main 可查询 | 显示 Git HEAD 与仓库 main；显示查询来源 origin 或 deploy.repo。部署继续经过原有排空、main 归属、快进、工作区、导入及回滚检查。 |
| 非 Git 发布目录、未配置仓库 | 中心版本未知；main 查询解释缺少 origin；中心与共享目录星系不可部署。不会借用另一个源码仓库的 HEAD。 |
| 非 Git 发布目录、配置了仓库 | 可以显示该仓库真实的 main，中心仍不可部署。配置 repo 只恢复查询，不会赋予目录 Git 更新能力。 |
| 远端查询失败、超时或没有 main | 显示失败原因，清除上次查询结果，禁用部署；刷新可重试。错误不包含仓库凭据。 |
| 不可写、已有部署、待确认回滚 | 显示具体原因并禁用对应操作。历史回滚仍阻止新部署，继续用原恢复入口。 |
| Git 历史已证明 main 无法从当前 v3 快进 | 仍显示真实 main 与当前版本，解释禁止回退/覆盖，禁用整个共享组；服务端直接部署请求也在排空前拒绝。 |

`directory.galaxies[].deployable` 是旧协议的“上报了部署信息”，不代表完整发布资格。
新的 `deploy.blocked` 由部署器现有的共享目录分组计算；单组操作与中心组不具备资格时，
服务端在排空之前拒绝。`all` 继续跳过不能参与的非中心组，结果记录为 skipped；
不能把全部作业结束理解为每个离线或不可写星系都已升级。
状态是预检查，目录内文件权限、dirty tree、目标可快进等仍以实际执行检查为准。
目标兼容性复用 Git ancestry：本地已知历史不兼容时拒绝；正常新 main 的对象尚未存在本地时，
继续走原 update_code 的 fetch/main/快进检查，不会仅因新版本未缓存而禁止常规发布。
中心没有 Git 历史时也不提供远端兼容性的假保证，先恢复中心的 Git 目录。

## 本机已核实的边界（2026-10-15）

- v3test 从 `/opt/cthulhu-v3test` 的非 Git 副本运行，配置没有 deploy.repo/code_root。
  v2 `/opt/cthulhu` 是 Git checkout。两者原 Deploy.tsx/deploy.py 内容相同。
- v3test 运行副本只补入 fb48835 的每任务汇报改动，不是完整 fb48835。
  正式 v3 的 cli.py、temple.py、Chat.tsx 与运行副本存在差异：分别涉及防重复星系进程、
  磁盘/替换连接告警与 deploy_latest 错误返回、v3 任务结束状态展示。
  发布完整候选会同时带入这些已存在的 v3 差异，必须一并审核，不能称为仅复制本修复文件。
- 只读访问真实 GitHub 成功：`refs/heads/main` 为
  `2b1959987f4eece35ea66e3b7a01d4aacb51697e`，`refs/heads/v3` 为
  `9dba44b27f91a640a260b5c63e396ba59e05a53d`。
  本机正式 v3 是 `fb48835320ef4e6095b41bf9e8446534e5bd792c`，比所查询 main 多 12 个提交。
  这些是初次核验快照，操作前必须重查；主人随后于 14:42 授权将正式 v3 合入 main。
- main 仍是 v2 代码线。v3 当前 HEAD 不是 main 的祖先，原有快进检查会拒绝回退到该 main。
  main 升级到包含当前版本之后，此祖先关系会改变，提示自动消失，不硬编码 v2/v3 状态。
  不能把 `deploy.repo` 改成 v3 分支、把源码目录塞进 code_root，或给发布副本复制 `.git`
  来伪造版本/启用按钮。
- temple 与 test 的服务用户是 cthulhu；az-test 的服务用户是 azgalaxy。
  三个本地服务都需要从同一个真实代码目录启动，才会作为一组排空和更新。

## 分阶段恢复方案

以下命令是待审核操作，**本任务没有执行**。所有 root 操作先走 sudo_request。
不包含 ali/korea 的远端执行或全量部署。不要在活跃回合中运行切换/重启命令。

### 1. 审核源码与产物，准备独立候选（不触碰运行目录）

审核本修复提交；如还要合并任务命名/按 task 汇报改动，应先合并源码，再在最终源码执行
`venv/bin/python -m unittest discover -s tests -v` 和 `cd web && npm run build`，
提交生成的 `cthulhu/static`。不要用两个任务的静态目录相互覆盖。

下面使用已审核的本任务分支 HEAD 演示；如已合并其他改动，必须将 source 改成最终审核仓库。
先检查 HEAD、工作区与测试报告一致，再由该仓库的拥有者执行 bundle 命令。

```bash
source=/var/lib/cthulhu-v3test/work/deploy-ui-repo
git -C "$source" status --short --untracked-files=no
git -C "$source" rev-parse HEAD
git -C "$source" bundle create /var/lib/cthulhu-v3test/work/deploy-ui-reviewed.bundle HEAD
```

审核后的 root 会话中准备全新路径；路径已存在时停止，不能覆盖已有候选：

```bash
set -eu
source=/var/lib/cthulhu-v3test/work/deploy-ui-repo
reviewed_rev=$(git -c safe.directory="$source" -C "$source" rev-parse HEAD)
candidate=/opt/cthulhu-v3test-reviewed
test ! -e "$candidate"
install -d -o cthulhu -g cthulhu -m 0755 "$candidate"
sudo -u cthulhu git clone --no-checkout /var/lib/cthulhu-v3test/work/deploy-ui-reviewed.bundle "$candidate"
sudo -u cthulhu git -C "$candidate" switch -c v3-reviewed "$reviewed_rev"
sudo -u cthulhu git -C "$candidate" remote set-url origin https://github.com/waaadreamer/cthulhu.git
sudo -u cthulhu ln -s /opt/cthulhu/venv "$candidate/venv"
sudo -u cthulhu git -C "$candidate" diff --exit-code HEAD
sudo -u cthulhu env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$candidate" /opt/cthulhu/venv/bin/python -c 'import cthulhu.cli, cthulhu.galaxy, cthulhu.temple; from cthulhu.deploy import local_info; print(local_info())'
sudo -u azgalaxy env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$candidate" /opt/cthulhu/venv/bin/python -c 'import cthulhu.cli, cthulhu.galaxy; from cthulhu.deploy import local_info; print(local_info())'
```

预期 cthulhu 能读取正确 reviewed_rev、writable=true；azgalaxy 能读取相同版本，允许 writable=false，
由中心进程更新共享目录。不要扩大 azgalaxy 对 .git 的权限。
本任务没有改 pyproject.toml，不需修改共享 `/opt/cthulhu/venv`；若最终合并版本改依赖，应另建专用 venv 并审核 unit ExecStart，不能直接改 v2 的共享环境。

### 2. 本地维护窗口切换到可追溯 checkout

由中心现有 maintenance_drain 对 `test`、`az-test` 分别 `enabled=true`，
读取 maintenance 状态直到两者 `safe_to_restart=true`。必须等所有已投递回合/巡检/整理结束；
本任务完成、发布执行者处于被重启单元之外后才能操作。不能把旧 agent 的 safe=false 忽略掉。
记录 owner 的 status、两个本地 maintenance 返回及服务当前配置作为回滚依据。

下面 root 命令仅在上述条件满足并获审核后执行。它保持所有 token、state、DB、现有 ExecStart 不变，
仅给三个本地 unit 增加 WorkingDirectory/PYTHONPATH 覆盖。保存 deploy 配置前会完整备份原文件，
但不把其内容输出到终端或提交仓库。

```bash
set -eu
backup=/var/lib/cthulhu-v3test/deploy-backups/deploy-ui-git-restore
test ! -e "$backup"
install -d -m 0700 "$backup"
cp -a /etc/cthulhu-v3test/temple.json "$backup/temple.json"
for unit in cthulhu-v3test-temple cthulhu-v3test-galaxy cthulhu-v3test-azgalaxy; do
  dropin=/etc/systemd/system/$unit.service.d/60-reviewed-code.conf
  test ! -e "$dropin"
done
python3 - <<'PY'
import json
from pathlib import Path
p = Path('/etc/cthulhu-v3test/temple.json')
c = json.loads(p.read_text())
c.setdefault('deploy', {})['repo'] = 'https://github.com/waaadreamer/cthulhu.git'
c['deploy'].pop('code_root', None)  # ROOT follows the code actually imported by the service.
p.write_text(json.dumps(c, ensure_ascii=False, indent=2) + '\n')
PY
for unit in cthulhu-v3test-temple cthulhu-v3test-galaxy cthulhu-v3test-azgalaxy; do
  install -d -m 0755 /etc/systemd/system/$unit.service.d
  cat > /etc/systemd/system/$unit.service.d/60-reviewed-code.conf <<'UNIT'
[Service]
WorkingDirectory=/opt/cthulhu-v3test-reviewed
Environment=PYTHONPATH=/opt/cthulhu-v3test-reviewed
UNIT
done
systemctl daemon-reload
systemctl restart cthulhu-v3test-temple.service
systemctl restart cthulhu-v3test-galaxy.service cthulhu-v3test-azgalaxy.service
systemctl is-active cthulhu-v3test-temple.service cthulhu-v3test-galaxy.service cthulhu-v3test-azgalaxy.service
```

影响：中心 WebSocket 短暂重连；只重启两个本地星系（在排空已确认后），不重启或部署 ali/korea。
等待中心认证连接报告 temple/test/az-test 的 root 都为候选目录、revision 都等于 reviewed_rev，
状态接口正确、每任务汇报仍存在，再用现有 maintenance_drain 解除这两个本地组的排空。
不要直接 POST deploy 来测试。此时版本与 Git 更新基础设施恢复；当前旧 main 仍不能快进到本地 v3，
界面应同时显示两个真实 SHA、解释无法快进并禁用相应按钮，而不是启用后再尝试回退。

若导入、启动、版本确认或汇报验证失败，保持排空，按同一维护窗口回滚：

```bash
set -eu
backup=/var/lib/cthulhu-v3test/deploy-backups/deploy-ui-git-restore
cp -a "$backup/temple.json" /etc/cthulhu-v3test/temple.json
for unit in cthulhu-v3test-temple cthulhu-v3test-galaxy cthulhu-v3test-azgalaxy; do
  rm /etc/systemd/system/$unit.service.d/60-reviewed-code.conf
done
systemctl daemon-reload
systemctl restart cthulhu-v3test-temple.service
systemctl restart cthulhu-v3test-galaxy.service cthulhu-v3test-azgalaxy.service
```

回滚只移除本方案添加的具名 drop-in，恢复原配置和原非 Git 运行副本；其版本应重新显示未知。
确认原服务、连接及任务汇报恢复后才能解除排空。

### 3. 恢复 main 的实际发布条件

现有部署机制固定读取 `refs/heads/main`，更新端固定 fetch 自己的 origin/main。
因此仅设置 deploy.repo 不会改变更新端来源。两者必须指向同一个已审核仓库。

恢复现有 GitHub main 一键发布，需要仓库维护者将审核的 v3 发布历史**保留祖先关系**地合入 main，
并推送；不能 squash 成与已运行 reviewed_rev 无祖先关系的新提交，也不能强制回退本地 v3。
主人已于 14:42 授权正式 v3 → main，本任务另用干净集成分支处理该发布；未验收的本部署修复、
任务短名/CoT 和 Official 模型功能不混入正式基线。运行服务仍需上述维护窗口，推 main 不等于服务已升级。
本修复的候选提交也必须单独审核并合入 main 后才具备后续快进条件；不创建替代 release 仓库，不更改分支语义。

main 迁移获批并由维护者完成后，以下是候选目录内只更新本地 Git 引用、不更新运行源码的核验：

```bash
sudo -u cthulhu env GIT_TERMINAL_PROMPT=0 git -C /opt/cthulhu-v3test-reviewed fetch origin main
sudo -u cthulhu git -C /opt/cthulhu-v3test-reviewed rev-parse HEAD FETCH_HEAD
sudo -u cthulhu git -C /opt/cthulhu-v3test-reviewed merge-base --is-ancestor HEAD FETCH_HEAD
sudo -u cthulhu git -C /opt/cthulhu-v3test-reviewed status --porcelain --untracked-files=no
```

祖先检查退出码必须为 0，工作区无已跟踪修改，GitHub main 查询必须与 FETCH_HEAD 一致。
随后刷新界面，由原部署器发布。若先只验证中心组，授权操作只能指定 `['__temple__']`；
它会按原机制自动包含 test/az-test。ali/korea 的发布仍需另外授权，不能按“全部部署”代替本地验证。
