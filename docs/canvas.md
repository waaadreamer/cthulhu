# 独立 Canvas

Canvas 是独立实体，不需要现有 task。一个面板的一次 stdout 可以汇总多个 RoboTwin 测试或其他实验；`task_ids` 仅是可选的多个任务引用，不随任务的创建/关闭改变生命周期。后端不解析训练、Alluxio 或集群的专用协议。

中心保存配置、采集票据、最后成功数据和失败回执。独立 local Canvas 服务通过中心 HTTP API 定时触发采集，并提供看板页面；中心通过现有 WebSocket 节点远程命令服务执行脚本。无 SSH、无第二套执行器，也没有 Canvas 本地业务数据库。

## 启动与入口

在已经支持 Canvas/远程命令的中心代码上：

```bash
.venv/bin/python -m cthulhu_v4 canvas \
  --center http://127.0.0.1:8767 \
  --token-file /absolute/center-state/web-token \
  --port 8780
```

`--center` 必须是本机 HTTP 地址，可包含反向代理前缀；`--tick-interval` 默认 1 秒，可设 0.1–5 秒。业务采集间隔由各面板自己的 `interval` 决定。服务只监听 loopback。

- 现有中心网页顶部的 **Canvas** 进入 `./boards.html`，沿用本浏览器已有的中心访问凭证。通过现有中心反向代理也能访问，不需要新开公网端口。
- 独立服务也提供 `http://127.0.0.1:8780/` 看板；首次访问需要填写同一中心 web-token（不同浏览器来源有各自的 localStorage）。后端不会把凭证嵌进页面或模板。
- 两个入口的左上角品牌链接均返回中心主会话；独立入口保留 `--center` 的路径前缀。
- 页面左侧为 Canvas 导航，右侧只展示所选的一个面板；首次打开优先选择未关闭的面板。选择写入 URL 的 `#canvas=面板ID`，可直接分享，也会在当前浏览器记住；刷新、前进后退恢复对应选择，失效 ID 回退到首个未关闭面板。“已关闭”分组仍可查看历史结果。窄屏用顶部“Canvas 列表”打开导航抽屉，选择后收起，模板占满主区剩余宽高；编辑、暂停/恢复、关闭和采集详情在右上角“管理”中。
- 选择切换只发起 GET 读取，不会触发采集或恢复命令；未选中的面板照常按中心配置采集。页面只读取当前选择的详细数据，已打开的 iframe 保留交互状态；只有模板变化才重载。Alluxio、RoboTwin 等业务模板沿用同一隔离数据接口。
- `deploy/cthulhu-canvas.service` 是可选 systemd 示例，先修改中心状态路径和安装路径，再安装。它不随中心自动启动，不会隐式创建任何生产采集。
- 若现有中心仍运行旧代码，需要通过原有部署流程升级中心和节点，节点须报告 `commands-v1`。这可能涉及原生会话，不要为试用 Canvas 直接重启生产中心。可先使用下面的全隔离演示。

隔离演示（demo 后端不调用模型；目录为新建临时目录）：

```bash
demo_state=$(mktemp -d /tmp/cthulhu-canvas-demo.XXXXXX)
.venv/bin/python -m cthulhu_v4 center --state "$demo_state" --port 8877 --ws-port 8878
# 另一个终端，将 token-file 换成上面打印的临时目录：
.venv/bin/python -m cthulhu_v4 canvas --center http://127.0.0.1:8877 \
  --token-file /tmp/cthulhu-canvas-demo.XXXXXX/web-token --port 8879
```

打开 `http://127.0.0.1:8877/boards.html`，填写临时 token，点“新建面板 → 填入双实验演示 → 保存”。该按钮生成两个明确标注“演示，未运行”的实验，每次 `observed_at` 来自实际采集时间，不代表真实训练进度。结束后关闭面板并停止这两个隔离进程。

## 配置与操作

必填：`name`、`html_template`、`node_id`、节点上的绝对 `cwd`、`script`、`interval`（1–86400 秒）、`timeout`（0.1–3600 秒）。可选 `task_ids: []`。HTML 模板上限 256KiB，脚本上限 64000 字节。脚本由节点用户执行，和节点命令行一致；应选择短时、只读、范围明确的采集脚本。

创建后等待独立服务采集；编辑使用 `revision` 防止覆盖其他页面的并发修改。暂停不再启动新采集，本次已提交的采集仍可回收。恢复立即进入下次采集窗口。关闭是终态，停止后续采集，已提交采集等待自身超时收尾并丢弃迟到结果；关闭不会向它展示的训练/评测发送任何停止命令。需要重新采集时新建面板。

创建与编辑的输入在当前标签页的 sessionStorage 中暂存，关闭编辑器、刷新或返回看板后，重新打开对应的新建/编辑入口即可继续；保存或“丢弃草稿”会清除该草稿，关闭标签页后不保证保留。存储被浏览器禁用或写满时会显示无法暂存的提示。草稿不含访问凭证；编辑草稿保留最初的 revision，其他窗口已修改时仍会拒绝覆盖并保留输入。名称上限按 UTF-8 字节计算，界面会在提交前显示对应字段的错误。切换配置时保持模板未变的 iframe、采集详情展开状态和键盘焦点；模板变化才重载隔离 iframe。

同一面板只保留一个在途命令。采集未结束时跳过 tick，结束回收后从当前时间再等待一个 `interval`，不补跑错过的采集。中心事务同时保存命令和在途记录，多个服务实例、HTTP 响应丢失和 Canvas 服务重启均沿用原票据。节点每个最多 4 个未结束命令；容量满或升级时本面板显示失败并等待下一间隔，其他面板仍可回收。

中心/节点重启、租约丢失后的命令可能是 `unknown`。面板会自动暂停；核验节点旧采集及其子进程已经结束后，才能通过“恢复”的显式确认启动新采集。程序不会自动重放未知命令。

采集 stdout/stderr 合计最多 1MiB。只有正常退出（exit_code 0）、未截断、配置版本仍一致且面板未关闭时，才更新最后成功数据。JSON stdout 自动解析；其他 stdout 作为文本保留。失败、超时、输出截断保留上次成功数据并展示 stderr/exit_code/error。编辑后的旧配置结果只留采集回执，不覆盖成功数据。

页面和模板都会收到采集时间、失败、节点离线、采集服务离线、过期信息。鲜度从中心创建该采集命令的时间保守计算，超过 `max(2 × interval, interval + timeout)` 秒或配置版本改变就标为过期；展示的采集时间是中心收到成功回执的时间，不能用节点时钟或 worker 恢复时间给旧数据刷新鲜度。Canvas 服务连续 15 秒没有成功 tick 即离线。浏览器无法读取中心时也会主动撤销 iframe 的 fresh 标志。保留的数据不是实时状态，采集到的文件内容自身也可能旧，模板应显示文件时间。

## 多实验真实文件汇总

`examples/canvas_collect.py` 只读取清单指定的至多 100 个 JSON 文件，不递归扫描、不启动训练。将脚本放在执行节点可读位置，例如：

```json
{"experiments":[
  {"name":"RoboTwin test A","path":"/experiments/test-a/status.json"},
  {"name":"RoboTwin test B","path":"/experiments/test-b/status.json"}
]}
```

各状态文件格式为 `{"status":"实际状态","metrics":{"实际指标名":123}}`。清单/单个状态文件上限 256KiB；相对路径按清单所在目录解析。缺失/无效文件在对应实验行报错；脚本不推测百分比。采集脚本填写：

```bash
python3 /path/to/canvas_collect.py /path/to/manifest.json
```

模板使用 `static/examples/canvas-summary.html`，显示数据源、多个实验行、文件路径与文件修改时间。这里的 `demo: false` 表示读了明确指定的文件，不保证文件内容来自真实训练；数据可信度由你指定的数据源决定。

## 模板固定接口与隔离

模板运行在 `sandbox="allow-scripts"` iframe，不含 `allow-same-origin`。严格 CSP 禁止 fetch/XHR/WebSocket、外部脚本、表单、嵌套 iframe；图像仅可用 data/blob。模板不会收到中心 token、采集配置或命令执行接口。不要把采集输出插入 HTML。

```html
<pre id="output"></pre>
<script>
addEventListener('canvas:update', event => {
  const snapshot = event.detail;
  document.getElementById('output').textContent = JSON.stringify({
    data: snapshot.data, status: snapshot.status,
    sampled_at: snapshot.sampled_at, fresh: snapshot.fresh
  }, null, 2);
});
</script>
```

普通数据更新通过 `canvas:update` 传入现有文档，模板版本的 HTML 变化才重新创建 iframe。运行时在同步事件前后保留详情展开、键盘焦点、输入光标及内外滚动；重建节点用 `id` 或 `data-canvas-key` 等业务身份匹配，不能用数组下标替代已消失的数据项。动态详情、图表标记和局部滚动容器应提供稳定且在所属容器中唯一的身份，避免把会变化的数值当 key。筛选、图表选择等业务状态由模板保存，并在所选对象消失时显示无数据或明确回退。

异步解压后的绘图、模板自己的定时刷新要显式包在 `Canvas.preserveView(() => render())` 中；支持返回 Promise。捕获后若用户又进行了输入、点击、键盘或滚轮操作，用户操作优先，不再恢复过时的位置。此接口只保护视图，不停止采集、不忽略新数据；模板仍应更新数值和图表，并保留自己的筛选/选择状态。运行时更新通过静态文件热发布，已经打开的旧文档需要首次刷新一次才能加载新运行时；之后数据刷新不需要刷新整页。部署模板 HTML 新版本也会有一次有意的上下文重建。

每次更新后也可用 `window.Canvas.getData()` 读取当前快照。快照包含 `id/name/state/status/stale/fresh/node_online/collector_online/sampled_at/now/revision/blocked_unknown/data_revision/data/format/attempt`。`attempt` 为最近回执，带 stdout、stderr、exit_code、error、truncated、discarded 和命令 ID。首次没有成功数据时 `data` 为 null。模板应监听事件获取初始数据；API 在模板执行后的首次事件前建立。

## 中心 API

所有接口均要求现有中心 Bearer token 与 Origin 校验：

| 方法 | 路径 | 含义 |
| --- | --- | --- |
| GET/POST | `/api/canvas` | 列表（轻量元数据与节点）/ 创建 |
| GET/POST | `/api/canvas/{id}` | 配置详情 / 编辑（必填 revision） |
| GET | `/api/canvas/{id}/data` | 固定数据快照 |
| POST | `/api/canvas/{id}/pause` | 暂停，body `{}` |
| POST | `/api/canvas/{id}/resume` | 恢复，unknown 后需 `{"acknowledge_unknown":true}` |
| POST | `/api/canvas/{id}/close` | 关闭，body `{}` |
| POST | `/api/canvas/tick` | 独立服务心跳、回收与调度，body `{"worker":"实例标识"}` |

列表不传模板/脚本/大输出；页面只在数据标记变更时读取数据。独立服务只代理 Canvas API，不代理任意中心 API。停止服务后无新 tick；已提交节点命令依照自身 timeout 和中心租约结束，最后成功数据仍保存在中心。

## 验证

```bash
PATH="$PWD/.venv/bin:$PATH" python3 -m unittest discover -s tests -v
for f in static/js/*.js; do node --check "$f"; done
node tests/test_model.mjs
node tests/test_board_data.mjs
PLAYWRIGHT_PATH=/path/to/playwright-core node tests/ui_canvas.cjs
PLAYWRIGHT_PATH=/path/to/playwright-core node tests/ui_canvas_review.cjs
PLAYWRIGHT_PATH=/path/to/playwright-core node tests/ui_canvas_selection.cjs
# 缓存浏览器与 Playwright 版本不同时可加 PLAYWRIGHT_EXECUTABLE=/path/to/chrome
```

单元测试验证票据/事务/恢复不变量；HTTP 端到端和可选浏览器测试通过真实隔离命令读取两份状态文件，再将 completed 从 3 改为 4 验证模板更新。它们不调用模型，也不把 fixture 当成真实训练表现。浏览器脚本的隔离进程/目录会清理，截图默认 `/tmp/cthulhu-canvas-preview.png`。

第二轮浏览器脚本另外覆盖主入口导航、凭证失败、草稿与并发编辑冲突、窄屏输入错误、长标题、多面板响应乱序、失败保留数据、浏览器离线及 Canvas 服务停止/恢复；截图默认保存在 `/tmp/cthulhu-canvas-review-ui`，可用 `CANVAS_REVIEW_ARTIFACTS` 指定位置。其 HTTP 乱序由测试主动延迟请求实现，文件采集与失败/恢复则使用实际隔离命令。
