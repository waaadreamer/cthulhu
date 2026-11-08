// 可选浏览器验收：真实临时中心 + local 节点 + shell，不调用模型。
// PLAYWRIGHT_PATH=/path/to/playwright[-core] [CHROMIUM_PATH=/path/to/chrome] node tests/ui_execution.cjs
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert');
(async () => {
  const root = path.resolve(__dirname, '..'), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-command-ui-'));
  const state = path.join(tmp, 'state'), cwd = path.join(tmp, 'work'); fs.mkdirSync(cwd);
  const python = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
  const server = spawn(python, ['-m', 'cthulhu_v4', 'center', '--state', state, '--port', '0', '--ws-port', '0', '--git-proxy', 'http://127.0.0.1:1'], { cwd: root });
  let browser, stderr = ''; server.stderr.on('data', b => stderr += b);
  try {
    const url = await new Promise((resolve, reject) => {
      let text = ''; const timer = setTimeout(() => reject(Error('中心启动超时：' + stderr)), 10000);
      server.stdout.on('data', b => { text += b; const match = text.match(/http:\/\/127\.0\.0\.1:\d+/); if (match) { clearTimeout(timer); resolve(match[0]); } });
      server.on('exit', code => { clearTimeout(timer); reject(Error('中心退出 ' + code + ': ' + stderr)); });
    });
    const token = fs.readFileSync(path.join(state, 'web-token'), 'utf8').trim();
    browser = await chromium.launch({ args: ['--no-sandbox'], ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}) });
    const page = await browser.newPage({ viewport: { width: 1100, height: 900 } }); page.setDefaultTimeout(12000);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.goto(url); await page.locator('#login-token').fill(token); await page.locator('#login-form button').click();
    const open = async () => { await page.locator('#nodes button').filter({ hasText: 'local（中心）' }).click(); await page.locator('#node-dialog[open]').waitFor(); };
    await open();
    const form = page.locator('#command-form');
    await form.locator('[name=cwd]').fill(cwd);
    await form.locator('[name=script]').fill("printf '<img src=x onerror=alert(1)>stream'; printf stderr >&2; sleep 2; printf done");
    await page.locator('#command-run').click();
    await page.waitForFunction(() => document.querySelector('#command-stdout').textContent.includes('stream'));
    assert.equal(await page.locator('#command-stdout img').count(), 0);
    assert.equal(await page.locator('#command-stop').isEnabled(), true);
    // 关闭对话框不取消进程；重新打开能找回同一条命令与输出。
    await page.locator('#node-form [data-close]').click(); await open();
    await page.locator('#command-state').filter({ hasText: '退出码 0' }).waitFor();
    assert.equal(await page.locator('#command-stdout').textContent(), '<img src=x onerror=alert(1)>streamdone');
    assert.equal(await page.locator('#command-stderr').textContent(), 'stderr');
    await form.locator('[name=script]').fill('printf waiting; sleep 20');
    await page.locator('#command-run').click();
    await page.waitForFunction(() => document.querySelector('#command-stdout').textContent === 'waiting');
    await page.locator('#command-stop').click();
    await page.locator('#command-state').filter({ hasText: '已停止' }).waitFor();
    await form.locator('[name=script]').fill('sleep 20'); await form.locator('[name=timeout]').fill('0.3');
    await page.locator('#command-run').click();
    await page.locator('#command-state').filter({ hasText: '已超时' }).waitFor();
    await form.locator('[name=timeout]').fill('60');
    await form.locator('[name=script]').fill("head -c 1200000 /dev/zero | tr '\\000' x");
    await page.locator('#command-run').click();
    await page.locator('#command-state').filter({ hasText: '输出已截断' }).waitFor();
    await page.locator('#command-state').filter({ hasText: '退出码 0' }).waitFor();
    assert.equal((await page.locator('#command-stdout').textContent()).length, 1024 * 1024);
    await page.setViewportSize({ width: 390, height: 844 });
    assert(await page.locator('#node-dialog').evaluate(d => d.getBoundingClientRect().width <= innerWidth));
    assert.deepEqual(errors, []);
    console.log('节点命令行浏览器验收通过：实时输出/文本安全/关闭恢复/停止/超时/1MiB截断/窄屏。');
  } finally {
    if (browser) await browser.close();
    server.kill('SIGTERM');
    await new Promise(resolve => server.exitCode != null ? resolve() : server.once('exit', resolve));
    fs.rmSync(tmp, { recursive: true, force: true });
  }
})().catch(e => { console.error(e); process.exitCode = 1; });
