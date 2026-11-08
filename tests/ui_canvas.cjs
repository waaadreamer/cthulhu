// 可选浏览器验收：独立服务、真实文件采集和隔离 iframe。不调用模型。
// PLAYWRIGHT_PATH=/path/to/playwright-core node tests/ui_canvas.cjs
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert/strict');

const root = path.resolve(__dirname, '..');
const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
const quote = text => "'" + text.replace(/'/g, "'\\''") + "'";
function launch(args) {
  const proc = spawn(py, ['-m', 'cthulhu_v4', ...args], { cwd: root,
    env: { ...process.env, PATH: path.dirname(py) + ':/usr/bin:/bin' } });
  let log = '';
  proc.stderr.on('data', b => { log += b; });
  const ready = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(Error('服务启动超时 ' + log)), 15000);
    let out = '';
    proc.stdout.on('data', b => {
      out += b;
      const match = out.match(/http:\/\/127\.0\.0\.1:\d+/);
      if (match) { clearTimeout(timer); resolve(match[0]); }
    });
    proc.on('exit', code => { clearTimeout(timer); reject(Error('服务退出 ' + code + ' ' + log)); });
  });
  return { proc, ready };
}
async function stop(proc) {
  if (!proc || proc.exitCode != null) return;
  await new Promise(resolve => { proc.once('exit', resolve); proc.kill('SIGTERM'); });
}

(async () => {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-canvas-ui-'));
  let center, service, browser;
  try {
    center = launch(['center', '--state', path.join(state, 'center'), '--port', '0', '--ws-port', '0', '--git-proxy', 'http://127.0.0.1:1']);
    const centerURL = await center.ready;
    const tokenFile = path.join(state, 'center', 'web-token'), token = fs.readFileSync(tokenFile, 'utf8').trim();
    service = launch(['canvas', '--center', centerURL, '--token-file', tokenFile, '--port', '0', '--tick-interval', '.1']);
    const serviceURL = await service.ready;
    browser = await chromium.launch({ args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_EXECUTABLE || undefined });
    const page = await browser.newPage({ viewport: { width: 1500, height: 1000 } });
    page.setDefaultTimeout(15000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    await page.goto(centerURL + '/boards.html');
    await page.locator('#board-token').fill(token); await page.locator('#board-login-form button').click();
    await page.getByText('还没有 Canvas。', { exact: false }).waitFor();
    fs.writeFileSync(path.join(state, 'a.json'), JSON.stringify({ status: '隔离测试文件', metrics: { completed: 3 } }));
    fs.writeFileSync(path.join(state, 'b.json'), JSON.stringify({ status: '隔离测试文件', metrics: { completed: 5 } }));
    fs.writeFileSync(path.join(state, 'manifest.json'), JSON.stringify({ experiments: [{ name: '实验 A', path: 'a.json' }, { name: '实验 B', path: 'b.json' }] }));
    await page.locator('#board-new').click();
    const form = page.locator('#board-form');
    await form.locator('[name=name]').fill('清单汇总 · 两个隔离实验');
    await form.locator('[name=cwd]').fill(state);
    await form.locator('[name=interval]').fill('1');
    await form.locator('[name=timeout]').fill('2');
    await form.locator('[name=script]').fill(`python3 ${quote(path.join(root, 'examples/canvas_collect.py'))} ${quote(path.join(state, 'manifest.json'))}`);
    await form.locator('[name=html_template]').fill(fs.readFileSync(path.join(root, 'static/examples/canvas-summary.html'), 'utf8'));
    await form.locator('[type=submit]').click();
    const first = page.locator('.board-card').first();
    await first.locator('.board-status.fresh').waitFor();
    const iframe = first.frameLocator('iframe');
    await iframe.getByText('实验 A', { exact: true }).waitFor();
    await iframe.getByText('实验 B', { exact: true }).waitFor();
    let frame = page.frames().find(f => f.url().endsWith('/board-frame.html'));
    assert.equal(await frame.evaluate(() => Canvas.getData().data.experiments[0].metrics.completed), 3);
    fs.writeFileSync(path.join(state, 'a.json'), JSON.stringify({ status: '文件已更新', metrics: { completed: 4 } }));
    await frame.waitForFunction(() => Canvas.getData().data.experiments[0].metrics.completed === 4);
    assert.equal(await frame.evaluate(() => { try { return parent.localStorage.getItem('cthulhu-token'); } catch { return 'blocked'; } }), 'blocked');
    assert.ok(!(await frame.evaluate(() => JSON.stringify(Canvas.getData()))).includes(token));
    await first.getByRole('button', { name: '暂停', exact: true }).click();
    await first.locator('.board-status').getByText('已暂停', { exact: true }).waitFor();
    await first.getByRole('button', { name: '恢复', exact: true }).click();
    await first.locator('.board-status.fresh').waitFor();
    // 第二面板来自显式演示脚本；无 task 创建/关联，与第一面板同时展示。
    await page.locator('#board-new').click(); await page.locator('#board-example').click();
    await form.locator('[name=interval]').fill('1'); await form.locator('[type=submit]').click();
    const second = page.locator('.board-card').nth(1);
    await second.frameLocator('iframe').getByText('RoboTwin 示例 A', { exact: true }).waitFor();
    await page.screenshot({ path: process.env.CANVAS_SCREENSHOT || '/tmp/cthulhu-canvas-preview.png', fullPage: true });
    // 模板只能收固定快照；采集 stdout 中的 HTML 仍作为纯文本进入模板。
    await second.getByRole('button', { name: '编辑', exact: true }).click();
    await form.locator('[name=script]').fill("printf '<script>window.injected=true</script>'");
    await form.locator('[type=submit]').click();
    await second.frameLocator('iframe').locator('#text').getByText('<script>window.injected=true</script>', { exact: true }).waitFor();
    frame = page.frames().filter(f => f.url().endsWith('/board-frame.html')).at(-1);
    assert.equal(await frame.evaluate(() => window.injected), undefined);
    const stateResponse = await page.request.get(centerURL + '/api/state', { headers: { Authorization: 'Bearer ' + token } });
    assert.equal((await stateResponse.json()).tasks.length, 0);
    // 独立服务自身也提供展示页面，浏览器凭证仍需显式填写，页面不注入后端凭证。
    const local = await browser.newPage(); await local.goto(serviceURL + '/');
    await local.locator('#board-token').fill(token); await local.locator('#board-login-form button').click();
    await local.locator('.board-card').nth(1).waitFor();
    page.once('dialog', dialog => dialog.accept());
    await first.getByRole('button', { name: '关闭采集', exact: true }).click();
    await first.locator('.board-status').getByText('已关闭', { exact: true }).waitFor();
    await stop(service.proc);
    await second.locator('.board-status').getByText('Canvas 服务离线', { exact: true }).waitFor({ timeout: 20000 });
    frame = page.frames().filter(f => f.url().endsWith('/board-frame.html')).at(-1);
    assert.equal(await frame.evaluate(() => Canvas.getData().fresh), false);
    assert.deepEqual(errors, []);
    console.log('Canvas browser passed: real files 3→4, two experiments, two panels, pause/resume/close, opaque sandbox, stdout text, independent service UI; screenshot /tmp/cthulhu-canvas-preview.png');
  } finally {
    if (browser) await browser.close();
    await stop(service?.proc); await stop(center?.proc);
    fs.rmSync(state, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
