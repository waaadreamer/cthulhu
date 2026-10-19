// 可选浏览器验收：临时状态目录 + demo 后端，不调用模型。
// PLAYWRIGHT_PATH=/path/to/playwright node tests/ui.cjs
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process'); const fs = require('fs'); const os = require('os'); const path = require('path'); const assert = require('assert');
(async () => {
  const root = path.resolve(__dirname, '..'), state = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-ui-'));
  const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
  const server = spawn(py, ['-m', 'cthulhu_v4', 'center', '--state', state, '--port', '0', '--ws-port', '0', '--heartbeat', '1', '--question-timeout', '3'], { cwd: root });
  let browser; const errors = [];
  try {
    const url = await new Promise((ok, bad) => { let t = ''; const timer = setTimeout(() => bad(Error('server timeout')), 10000);
      server.stdout.on('data', b => { t += b; const m = t.match(/http:\/\/127\.0\.0\.1:\d+/); if (m) { clearTimeout(timer); ok(m[0]); } });
      server.on('exit', c => bad(Error('server exit ' + c))); });
    const token = fs.readFileSync(path.join(state, 'web-token'), 'utf8').trim();
    browser = await chromium.launch({ args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 900 } }); page.setDefaultTimeout(15000);
    page.on('pageerror', e => errors.push(e.message)); page.on('console', m => m.type() === 'error' && errors.push(m.text()));
    await page.goto(url + '/');
    await page.locator('#login-token').fill(token); await page.locator('#login-form button').click();
    await page.getByText('从一个目标开始').waitFor();
    // 加急发送
    await page.locator('#input').fill('你好'); await page.locator('[data-priority="10"]').click();
    const req = page.waitForRequest(r => r.url().endsWith('/api/chat'));
    await page.locator('#input').press('Enter'); assert.equal((await req).postDataJSON().priority, 10);
    // 发送后立即有“工作中”反馈，回复到达后消失。
    await page.locator('#chat .live').waitFor({ timeout: 3000 });
    await page.locator('.reply').getByText('[模拟 Master]', { exact: false }).waitFor();
    await page.locator('#chat .live').waitFor({ state: 'detached' });
    // 切换模型：同后端直接保存；换后端需要确认新会话。
    await page.locator('#model').click();
    await page.locator('#model-form [name=model]').fill('demo-model');
    await page.locator('#model-form .btn.primary').click();
    await page.locator('#model').getByText('demo-model').waitFor();
    // 新建任务 → 抽屉自动打开
    await page.locator('#new-task').click();
    const f = page.locator('#task-form');
    // 工作目录与中心状态目录分开：状态目录受保护，不能经网页读取。
    const work = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-work-'));
    fs.writeFileSync(path.join(work, 'report.md'), '# 检查结论\n\n全部通过。');
    await f.locator('[name=title]').fill('试用问答流程'); await f.locator('[name=description]').fill('只运行模拟任务，参考 ' + path.join(work, 'report.md'));
    await f.locator('[name=acceptance]').fill('问题得到回答并完成'); await f.locator('[name=cwd]').fill(work);
    await f.locator('button[type=submit]').click();
    await page.locator('#drawer .task-card').waitFor();
    // 任务卡片里的裸路径按任务所在节点渲染为固定链接，点开后由节点读取并渲染 Markdown。
    const link = page.locator('#drawer .task-card a.file-link');
    assert.equal(await link.getAttribute('data-ref'), 'local::' + path.join(work, 'report.md'));
    await link.click();
    await page.locator('#viewer-body .doc').getByText('全部通过。').waitFor();
    assert((await page.evaluate(() => location.hash)).startsWith('#file='));
    await page.locator('#viewer-close').click();
    assert((await page.evaluate(() => location.hash)).startsWith('#task='));
    // 中心状态目录（含 web-token）不能经网页读出。
    await page.evaluate(p => { location.hash = '#file=' + encodeURIComponent('local::' + p); }, path.join(state, 'web-token'));
    await page.locator('#viewer-body .bad').getByText('凭证', { exact: false }).waitFor();
    await page.locator('#viewer-close').click();
    assert((await page.evaluate(() => location.hash)).startsWith('#task='));
    errors.splice(0);  // 上面这次 400 是预期的拒绝
    await page.locator('#timeline .card.question').waitFor();
    // 转给我 → 交给 Master：问题回到 Master 手里，卡片仍在、输入框仍可用。
    await page.locator('#questions').getByText('模拟：转给我').click();
    await page.locator('#questions .q[data-state=owner]').waitFor();
    await page.locator('#questions').getByText('交给 Master').click();
    await page.locator('#questions .q[data-state=master]').waitFor();
    // 全权回答开关
    await page.locator('#decides').click();
    await page.locator('#decides[aria-checked=true]').waitFor();
    await page.locator('#decides').click();
    await page.locator('#decides[aria-checked=false]').waitFor();
    // 在输入区回答问题
    // 输入期间轮询不能抢走焦点或清空草稿。
    const qa = page.locator('#questions textarea');
    await qa.click(); await page.keyboard.type('继');
    await page.waitForTimeout(2500);
    assert(await qa.evaluate(n => n === document.activeElement), '回答框在轮询后失去焦点');
    await page.keyboard.type('续');
    assert.equal(await qa.inputValue(), '继续');
    await page.locator('#questions .btn.primary').click();
    await page.locator('#timeline .card.result').waitFor();
    await page.locator('#d-state').getByText('已完成').waitFor();
    const taskHash = await page.evaluate(() => location.hash);
    // 工具与命令默认折叠
    const steps = page.locator('#timeline details.step');
    assert((await steps.count()) >= 2);
    assert.equal(await steps.first().evaluate(d => d.open), false);
    await steps.first().locator('summary').click(); assert.equal(await steps.first().evaluate(d => d.open), true);
    await page.screenshot({ path: path.join(root, 'preview.png') });
    // Esc 关闭、hash 直达
    const hash = await page.evaluate(() => location.hash); assert(hash.startsWith('#task='));
    await page.keyboard.press('Escape'); assert(await page.locator('#drawer').isHidden());
    await page.goto(url + '/' + hash); await page.locator('#timeline .card.result').waitFor();
    // 归档：从列表收起到“已归档”，取消归档后回来。
    await page.locator('#d-controls').getByText('归档', { exact: true }).click();
    await page.locator('#drawer').waitFor({ state: 'hidden' });
    await page.locator('#tasks .archive summary').waitFor();
    assert.equal(await page.locator('#tasks > .task').count(), 0);
    await page.locator('#tasks .archive summary').click();
    await page.locator('#tasks .archive .task').click();
    await page.locator('#d-controls').getByText('取消归档').click();
    await page.locator('#tasks > .task').waitFor();
    // 添加节点命令
    await page.locator('#add-node').click(); await page.locator('#invite-form [name=name]').fill('gpu1');
    await page.locator('#invite-form .btn.primary').click(); await page.locator('#invite-cmd').getByText('curl -fsSL', { exact: false }).waitFor();
    await page.locator('#invite-form [data-close]').click();
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(root, 'preview-mobile.png') });
    assert.deepEqual(errors, []);
    console.log('ui: ok');
  } catch (e) { if (errors.length) console.error('page errors:', errors); throw e; }
  finally { if (browser) await browser.close(); server.kill(); fs.rmSync(state, { recursive: true, force: true }); }
})().catch(e => { console.error(e); process.exit(1); });
