// 第二轮独立浏览器验收。只启动隔离 demo 中心/Canvas，执行明确的 fixture 文件读取，不调用模型。
// PLAYWRIGHT_PATH=/path/to/playwright-core PLAYWRIGHT_EXECUTABLE=/path/to/chrome node tests/ui_canvas_review.cjs
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
const artifacts = process.env.CANVAS_REVIEW_ARTIFACTS || '/tmp/cthulhu-canvas-review-ui';
const quote = text => "'" + text.replace(/'/g, "'\\''") + "'";
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
async function choose(page, id) {
  if (await page.locator('#boards-list-toggle').isVisible() && await page.locator('#boards-sidebar').evaluate(e => e.inert)) await page.locator('#boards-list-toggle').click();
  const item = page.locator(`.board-nav-item[data-canvas-id="${id}"]`);
  if (await item.evaluate(e => e.parentElement.matches('details') && !e.parentElement.open)) await page.locator('.board-nav-closed > summary').click();
  await item.click(); await page.locator(`.board-card[data-canvas-id="${id}"]:visible iframe`).waitFor();
}
async function action(card, name) {
  const menu = card.locator('.board-management');
  if (!await menu.evaluate(e => e.open)) await menu.locator(':scope > summary').click();
  await card.getByRole('button', { name, exact: true }).click();
}

(async () => {
  const state = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-canvas-review-state-'));
  let center, service, browser;
  try {
    center = launch(['center', '--state', path.join(state, 'center'), '--port', '0', '--ws-port', '0', '--git-proxy', 'http://127.0.0.1:1']);
    const centerURL = await center.ready;
    const tokenFile = path.join(state, 'center', 'web-token'), token = fs.readFileSync(tokenFile, 'utf8').trim();
    const startService = port => launch(['canvas', '--center', centerURL, '--token-file', tokenFile, '--port', String(port), '--tick-interval', '.1']);
    service = startService(0); const serviceURL = await service.ready;
    browser = await chromium.launch({ args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_EXECUTABLE || undefined });
    const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
    const page = await context.newPage(); page.setDefaultTimeout(15000);
    const errors = []; page.on('pageerror', error => errors.push(error.message));
    const shot = name => page.screenshot({ path: path.join(artifacts, name + '.png'), fullPage: true });
    const api = async (url, body) => {
      const response = await page.request.fetch(centerURL + '/api/canvas' + url, { method: body === undefined ? 'GET' : 'POST', headers: { Authorization: 'Bearer ' + token }, data: body });
      assert.ok(response.ok(), await response.text()); return response.json();
    };
    await page.goto(centerURL + '/boards.html');
    await page.locator('#board-token').fill('invalid-review-fixture'); await page.locator('#board-login-form button').click();
    await page.locator('#board-login-error').getByText('凭证无效', { exact: false }).waitFor();
    assert.equal(await page.locator('#board-token').inputValue(), '');
    await shot('login-error');
    await page.locator('#board-token').fill(token); await page.locator('#board-login-form button').click();
    await page.getByText('还没有 Canvas。', { exact: false }).waitFor(); await shot('empty');
    await page.goto(centerURL + '/'); await page.locator('a[href="./boards.html"]').click();
    await page.getByText('还没有 Canvas。', { exact: false }).waitFor();
    console.log('PASS main navigation, invalid credentials visible inside modal, empty state');

    await page.locator('#board-new').click(); const form = page.locator('#board-form');
    await form.locator('[name=name]').fill('恢复后的创建草稿');
    await form.locator('[name=cwd]').fill('relative');
    await form.locator('[name=script]').fill('printf draft');
    await form.locator('[name=html_template]').fill('<pre>draft</pre>');
    await page.locator('#board-editor-close').click(); await page.locator('#board-new').click();
    assert.equal(await form.locator('[name=name]').inputValue(), '恢复后的创建草稿');
    await page.reload(); await page.locator('#board-new').click();
    assert.equal(await form.locator('[name=name]').inputValue(), '恢复后的创建草稿');
    await form.locator('[type=submit]').click(); await page.locator('#board-form-error').getByText('绝对路径', { exact: false }).waitFor();
    await form.locator('[name=cwd]').fill(state); await form.locator('[name=name]').fill('中'.repeat(100));
    await form.locator('[type=submit]').click(); await page.locator('#board-form-error').getByText('UTF-8 字节', { exact: false }).waitFor();
    await page.setViewportSize({ width: 390, height: 844 }); await shot('narrow-validation');
    assert.ok(await page.locator('#board-editor').evaluate(e => e.scrollWidth <= e.clientWidth + 1));
    for (const name of ['name', 'cwd', 'node_id', 'script', 'html_template']) assert.ok(await form.locator(`[name=${name}]`).evaluate(e => e.labels.length));
    fs.writeFileSync(path.join(state, 'a.json'), JSON.stringify({ status: '隔离文件，未运行训练', metrics: { completed: 3 } }));
    fs.writeFileSync(path.join(state, 'b.json'), JSON.stringify({ status: '隔离文件，未运行训练', metrics: { completed: 5 } }));
    fs.writeFileSync(path.join(state, 'manifest.json'), JSON.stringify({ experiments: [{ name: '验收实验 A', path: 'a.json' }, { name: '验收实验 B', path: 'b.json' }] }));
    const script = `python3 ${quote(path.join(root, 'examples/canvas_collect.py'))} ${quote(path.join(state, 'manifest.json'))}`;
    await form.locator('[name=name]').fill('文件汇总 · 两个隔离实验');
    await form.locator('[name=interval]').fill('1'); await form.locator('[name=timeout]').fill('2');
    await form.locator('[name=script]').fill(script);
    await form.locator('[name=html_template]').fill(fs.readFileSync(path.join(root, 'static/examples/canvas-summary.html'), 'utf8'));
    await page.route('**/api/canvas', async route => {
      if (route.request().method() === 'POST') await new Promise(resolve => setTimeout(resolve, 700));
      await route.continue();
    });
    await form.locator('[type=submit]').click();
    assert.equal(await form.locator('[name=name]').isDisabled(), true);
    await page.keyboard.press('Escape'); assert.equal(await page.locator('#board-editor').evaluate(e => e.open), true);
    await page.locator('#board-editor').waitFor({ state: 'hidden' }); await page.unroute('**/api/canvas');
    let first = page.locator('.board-card').first(); await first.locator('.board-status.fresh').waitFor();
    assert.equal(await page.getByRole('region', { name: '文件汇总 · 两个隔离实验', exact: true }).count(), 1);
    assert.equal(await first.getByRole('status').count(), 1);
    await first.frameLocator('iframe').getByText('验收实验 B', { exact: true }).waitFor();
    let frame = await (await first.locator('iframe').elementHandle()).contentFrame();
    assert.equal(await frame.evaluate(() => Canvas.getData().data.experiments[0].metrics.completed), 3);
    fs.writeFileSync(path.join(state, 'a.json'), JSON.stringify({ status: '实际读取到更新的隔离文件', metrics: { completed: 4 } }));
    await frame.waitForFunction(() => Canvas.getData().data.experiments[0].metrics.completed === 4);
    console.log('PASS close/reopen/reload draft, labelled controls, UTF-8/path feedback, narrow create, delayed save protects draft, real files 3 -> 4');

    await action(first, '暂停'); await first.locator('.board-status').getByText('已暂停', { exact: true }).waitFor();
    if (!await first.locator('.board-management').evaluate(e => e.open)) await first.locator('.board-management > summary').click();
    if (!await first.locator('.board-receipt').evaluate(e => e.open)) await first.locator('.board-receipt > summary').click(); await frame.evaluate(() => { window.reviewMarker = 'preserved'; });
    await first.getByRole('button', { name: '全屏', exact: true }).click(); await page.keyboard.press('Escape');
    assert.equal(await first.evaluate(e => e.classList.contains('fullscreen')), false);
    await action(first, '编辑');
    const longTitle = 'LongContinuousTitle'.repeat(8);
    await form.locator('[name=name]').fill(longTitle); await form.locator('[type=submit]').click();
    await first.locator('h2').getByText(longTitle, { exact: true }).waitFor();
    assert.equal(await frame.evaluate(() => window.reviewMarker), 'preserved');
    assert.equal(await first.locator('.board-receipt').evaluate(e => e.open), true);
    assert.ok(await first.locator('h2').evaluate(e => e.scrollWidth <= e.clientWidth + 1));
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await shot('narrow-long-title');
    await action(first, '恢复'); await first.locator('.board-status.fresh').waitFor();
    console.log('PASS pause/edit/resume, long title wraps, iframe/details preserved, keyboard Escape');

    const firstId = await first.getAttribute('data-canvas-id');
    await action(first, '编辑'); await form.locator('[name=name]').fill('尚未提交的编辑草稿');
    const latest = await api('/' + firstId); await api('/' + firstId, { revision: latest.revision, name: '另一个窗口的新配置' });
    await form.locator('[type=submit]').click(); await page.locator('#board-form-error').getByText('已被修改', { exact: false }).waitFor();
    assert.equal(await form.locator('[name=name]').inputValue(), '尚未提交的编辑草稿'); await shot('edit-conflict');
    await page.reload(); first = page.locator('.board-card').first(); await action(first, '编辑');
    await page.locator('#board-editor[open]').waitFor();
    assert.equal(await form.locator('[name=name]').inputValue(), '尚未提交的编辑草稿');
    await form.locator('[type=submit]').click(); await page.locator('#board-form-error').getByText('已被修改', { exact: false }).waitFor();
    page.once('dialog', d => d.accept()); await page.locator('#board-draft-discard').click();
    assert.equal((await api('/' + firstId)).name, '另一个窗口的新配置');
    console.log('PASS concurrent edit conflict retained across refresh; draft never overwrites newer revision');

    await page.setViewportSize({ width: 1440, height: 1000 });
    await page.locator('#board-new').click(); await page.locator('#board-example').click();
    await form.locator('[name=interval]').fill('1'); await form.locator('[type=submit]').click();
    let second = page.locator('.board-card').nth(1); await second.frameLocator('iframe').getByText('RoboTwin 示例 A', { exact: true }).waitFor();
    const secondId = await second.getAttribute('data-canvas-id');
    await page.route('**/api/canvas/' + firstId, async route => { await new Promise(resolve => setTimeout(resolve, 400)); await route.continue(); });
    await page.reload(); await page.locator('.board-card:visible').waitFor();
    assert.deepEqual(await page.locator('.board-nav-item').evaluateAll(es => es.map(e => e.dataset.canvasId)), [firstId, secondId]);
    assert.equal(await page.locator('.board-card:visible').count(), 1);
    await choose(page, firstId);
    await page.unroute('**/api/canvas/' + firstId); first = page.locator(`.board-card[data-canvas-id="${firstId}"]`);
    await choose(page, secondId); second = page.locator(`.board-card[data-canvas-id="${secondId}"]`); await choose(page, firstId);
    await shot('desktop-multiple');
    console.log('PASS multi-panel order remains stable with deliberately reordered HTTP responses');

    await action(first, '编辑'); await form.locator('[name=script]').fill('echo review-failure >&2; exit 7'); await form.locator('[type=submit]').click();
    await first.locator('.board-status').getByText('采集失败', { exact: false }).waitFor();
    if (!await first.locator('.board-management').evaluate(e => e.open)) await first.locator('.board-management > summary').click();
    if (!await first.locator('.board-receipt').evaluate(e => e.open)) await first.locator('.board-receipt > summary').click(); await first.locator('.board-error').getByText('review-failure', { exact: false }).waitFor();
    frame = await (await first.locator('iframe').elementHandle()).contentFrame();
    assert.equal(await frame.evaluate(() => Canvas.getData().data.experiments[0].metrics.completed), 4);
    assert.equal(await frame.evaluate(() => Canvas.getData().fresh), false); await shot('failure-keeps-data');
    await action(first, '编辑'); await form.locator('[name=script]').fill(script); await form.locator('[type=submit]').click(); await first.locator('.board-status.fresh').waitFor();
    await context.setOffline(true); await first.locator('.board-status').getByText('中心连接中断', { exact: false }).waitFor();
    assert.equal(await frame.evaluate(() => Canvas.getData().fresh), false);
    await context.setOffline(false); await first.locator('.board-status.fresh').waitFor();
    console.log('PASS real exit 7/stderr retains last successful data, edit recovers, browser offline revokes freshness then reconnects');

    await page.route('**/api/canvas', async route => {
      await new Promise(resolve => setTimeout(resolve, 11000));
      try { await route.continue(); } catch { /* 浏览器已取消超时请求 */ }
    });
    await first.locator('.board-status').getByText('中心连接中断', { exact: false }).waitFor();
    assert.equal(await frame.evaluate(() => Canvas.getData().fresh), false);
    await page.unroute('**/api/canvas'); await first.locator('.board-status.fresh').waitFor();
    console.log('PASS stalled polling times out and revokes freshness; subsequent poll recovers');

    const local = await context.newPage(); await local.goto(serviceURL + '/');
    assert.equal(await local.locator('#board-home').getAttribute('href'), centerURL + '/');
    await local.locator('#board-token').fill(token); await local.locator('#board-login-form button').click(); await choose(local, secondId);
    await local.locator('#board-home').click(); await local.waitForURL(centerURL + '/'); await local.close();
    await choose(page, secondId); await action(second, '编辑');
    await form.locator('[name=script]').fill("python3 -c \"print('long-output-' * 9000)\""); await form.locator('[type=submit]').click();
    await second.locator('.board-status.fresh').waitFor();
    const textFrame = await (await second.locator('iframe').elementHandle()).contentFrame();
    await textFrame.waitForFunction(() => Canvas.getData().format === 'text' && Canvas.getData().data.length > 100000);
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await second.screenshot({ path: path.join(artifacts, 'long-output.png') });
    console.log('PASS real 108KB stdout displayed as text with bounded card layout');
    page.once('dialog', d => d.accept()); await action(second, '关闭采集'); await second.locator('.board-status').getByText('已关闭', { exact: true }).waitFor();
    await choose(page, firstId); await stop(service.proc); await first.locator('.board-status').getByText('Canvas 服务离线', { exact: true }).waitFor({ timeout: 22000 });
    assert.equal(await frame.evaluate(() => Canvas.getData().fresh), false); await shot('collector-offline');
    service = startService(Number(new URL(serviceURL).port)); await service.ready; await first.locator('.board-status.fresh').waitFor();
    await action(first, '暂停'); await first.locator('.board-status').getByText('已暂停', { exact: true }).waitFor();
    assert.deepEqual(errors, []);
    assert.equal((await api('')).canvases.length, 2);
    console.log('PASS standalone return link, close, collector offline/restart, no page errors; artifacts ' + artifacts);
  } finally {
    if (browser) await browser.close(); await stop(service?.proc); await stop(center?.proc);
    fs.rmSync(state, { recursive: true, force: true });
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
