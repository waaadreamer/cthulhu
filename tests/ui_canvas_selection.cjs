// 单 Canvas 工作区：真实隔离中心与浏览器，只验证选择/布局，不启动模型或采集。
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
const artifacts = process.env.CANVAS_SELECTION_ARTIFACTS || '/tmp/cthulhu-canvas-selection';
fs.mkdirSync(artifacts, { recursive: true });
function launch(args) {
  const proc = spawn(py, ['-m', 'cthulhu_v4', ...args], { cwd: root, env: { ...process.env, PATH: path.dirname(py) + ':/usr/bin:/bin' } });
  let log = '', out = '';
  proc.stderr.on('data', b => { log += b; });
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('服务启动超时 ' + log)), 15000);
    proc.stdout.on('data', b => { out += b; const match = out.match(/http:\/\/127\.0\.0\.1:\d+/); if (match) { clearTimeout(timer); resolve(match[0]); } });
    proc.on('exit', code => { clearTimeout(timer); reject(Error('服务退出 ' + code + ' ' + log)); });
  });
  return { proc, ready };
}
async function stop(proc) {
  if (!proc || proc.exitCode != null) return;
  await new Promise(resolve => { proc.once('exit', resolve); proc.kill('SIGTERM'); });
}
(async () => {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-canvas-select-'));
  let center, browser;
  try {
    center = launch(['center', '--state', path.join(state, 'center'), '--port', '0', '--ws-port', '0', '--git-proxy', 'http://127.0.0.1:1']);
    const url = await center.ready;
    const token = fs.readFileSync(path.join(state, 'center', 'web-token'), 'utf8').trim();
    browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_EXECUTABLE || undefined, args: ['--no-sandbox'] });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage(); page.setDefaultTimeout(15000);
    const api = async (route, data) => {
      const response = await page.request.fetch(url + route, { method: data === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token }, data });
      assert.ok(response.ok(), await response.text()); return response.json();
    };
    const template = '<!doctype html><style>body{background:#15171e;color:#e8e9ef;font:18px system-ui}pre{white-space:pre-wrap}</style><h1 id="title"></h1><pre id="data"></pre><script>addEventListener("canvas:update",e=>{document.getElementById("title").textContent=e.detail.name;document.getElementById("data").textContent=JSON.stringify(e.detail)})</script>';
    const boards = [];
    for (const name of ['已关闭的部署记录', '实验 A · 单 Canvas 工作区', '实验 B · 单 Canvas 工作区']) {
      const board = await api('/api/canvas', { name, node_id: 'local', cwd: state, script: 'printf unused', interval: 60, timeout: 2, task_ids: [], html_template: template });
      await api('/api/canvas/' + board.id + (boards.length ? '/pause' : '/close'), {}); boards.push(board);
    }
    const [closed, a, b] = boards;
    const requests = [], errors = [];
    page.on('request', request => { if (new URL(request.url()).pathname.startsWith('/api/')) requests.push({ method: request.method(), path: new URL(request.url()).pathname }); });
    page.on('pageerror', error => errors.push(error.message));
    const active = () => page.locator('.board-card:visible');
    const frame = async () => (await active().locator('iframe').elementHandle()).contentFrame();
    const selected = async id => {
      await page.waitForFunction(id => document.querySelector('.board-card:not([hidden])')?.dataset.canvasId === id, id);
      await (await frame()).waitForFunction(id => window.Canvas?.getData().id === id, id);
      assert.equal(await active().count(), 1);
    };
    const choose = async id => {
      if (await page.locator('#boards-list-toggle').isVisible() && await page.locator('#boards-sidebar').evaluate(e => e.inert)) await page.locator('#boards-list-toggle').click();
      await page.locator(`.board-nav-item[data-canvas-id="${id}"]`).click(); await selected(id);
    };
    await page.goto(url + '/boards.html'); await page.locator('#board-token').fill(token); await page.locator('#board-login-form button').click();
    await selected(a.id); assert.ok(page.url().includes('canvas=' + a.id));
    assert.equal(await page.locator('.board-card').count(), 1, '首次只读取并创建选中的面板');
    const rect = await active().locator('iframe').boundingBox(); assert.ok(rect.width > 1100 && rect.height > 800);
    assert.equal(await active().getByRole('button', { name: '编辑', exact: true }).isVisible(), false);
    await (await frame()).evaluate(() => { window.selectionMarker = 'preserved'; });
    await choose(b.id); await choose(a.id); assert.equal(await (await frame()).evaluate(() => window.selectionMarker), 'preserved');
    await choose(b.id); await page.reload(); await selected(b.id);
    await page.goto(url + '/boards.html'); await selected(b.id);
    await choose(a.id); await page.goBack(); await selected(b.id); await page.goForward(); await selected(a.id);
    console.log('PASS default skips closed record, only one visible/lazy iframe, full viewport, menu collapsed, hash/refresh/storage/back-forward, preserved iframe');

    // 故意延迟尚未缓存的 A；用户已选 B 后，A 的迟到 GET 不能夺走选择。
    await page.goto(url + '/boards.html#canvas=' + b.id); await selected(b.id);
    await page.route('**/api/canvas/' + a.id + '?view=config', async route => { await new Promise(resolve => setTimeout(resolve, 600)); await route.continue(); });
    await page.locator(`.board-nav-item[data-canvas-id="${a.id}"]`).click();
    await page.locator(`.board-nav-item[data-canvas-id="${b.id}"]`).click(); await selected(b.id);
    await page.waitForTimeout(800); await selected(b.id); await page.unroute('**/api/canvas/' + a.id + '?view=config');
    await page.screenshot({ path: path.join(artifacts, 'desktop.png'), fullPage: true });
    console.log('PASS late GET for old selection does not replace current Canvas');

    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForFunction(() => document.querySelector('#boards-sidebar').getBoundingClientRect().right <= 1);
    assert.equal(await page.locator('#boards-sidebar').evaluate(e => e.inert), true);
    const mobile = await active().locator('iframe').boundingBox(); assert.ok(mobile.width >= 389 && mobile.height > 580);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(artifacts, 'narrow.png'), fullPage: true });
    await active().locator('.board-management > summary').click();
    const menu = await active().locator('.board-menu').boundingBox();
    assert.ok(menu.x >= 0 && menu.x + menu.width <= 391, '窄屏管理菜单必须留在视口内');
    await active().getByRole('button', { name: '编辑', exact: true }).click(); await page.locator('#board-editor[open]').waitFor();
    assert.equal(await page.locator('[name=name]').inputValue(), b.name); await page.locator('#board-editor-close').click();
    await page.locator('#boards-list-toggle').click();
    assert.equal(await page.locator('#boards-main').evaluate(e => e.inert), true);
    await page.screenshot({ path: path.join(artifacts, 'narrow-list.png') });
    await page.keyboard.press('Escape'); assert.equal(await page.locator('#boards-main').evaluate(e => e.inert), false);
    await choose(a.id); assert.equal(await page.locator('#boards-list-toggle').getAttribute('aria-expanded'), 'false');
    await page.locator('#boards-list-toggle').click(); await page.locator('.board-nav-closed > summary').click();
    await page.locator(`.board-nav-item[data-canvas-id="${closed.id}"]`).click(); await selected(closed.id);
    await page.goto(url + '/boards.html#canvas=does-not-exist'); await selected(a.id);
    await page.locator('#boards-list-toggle').click(); await page.locator('#board-new').click();
    await page.locator('#board-editor[open]').waitFor(); await page.locator('[name=name]').fill('切换前的草稿');
    await page.locator('#board-editor-close').click(); await choose(b.id);
    await page.locator('#boards-list-toggle').click(); await page.locator('#board-new').click();
    assert.equal(await page.locator('[name=name]').inputValue(), '切换前的草稿'); await page.locator('#board-editor-close').click();
    console.log('PASS narrow drawer open/Escape/select, main inert/focus, full-width Canvas, closed link, invalid hash fallback, draft retained across navigation');
    assert.deepEqual(errors, []); assert.ok(requests.every(request => request.method === 'GET'), JSON.stringify(requests));
    assert.deepEqual(await api('/api/executions?node_id=local'), []);
    const after = (await api('/api/canvas')).canvases;
    assert.deepEqual(after.map(board => [board.id, board.state, board.sequence]), [[closed.id, 'closed', 0], [a.id, 'paused', 0], [b.id, 'paused', 0]]);
    console.log('PASS all browser API requests GET; identities/states/sequence unchanged, command table empty, no model or collection executed; artifacts ' + artifacts);
  } finally { if (browser) await browser.close(); await stop(center?.proc); fs.rmSync(state, { recursive: true, force: true }); }
})().catch(error => { console.error(error); process.exitCode = 1; });
