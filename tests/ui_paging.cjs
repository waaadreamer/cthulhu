// 可选浏览器验收：长历史懒加载。预先写入 300 轮对话，检查首屏只加载一页、落在底部，向上滚动才加载更早的记录。
// PLAYWRIGHT_PATH=/path/to/playwright node tests/ui_paging.cjs
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn, execFileSync } = require('child_process'); const fs = require('fs'); const os = require('os'); const path = require('path'); const assert = require('assert');
(async () => {
  const root = path.resolve(__dirname, '..'), state = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-paging-'));
  const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
  execFileSync(py, ['-c', `
import sys; sys.path.insert(0, ${JSON.stringify(root)})
from cthulhu_v4.center import Center
c = Center(${JSON.stringify(state)}); s = c.store
for i in range(300):
    s.emit('dispatch', '', target='master', data={'job': f'turn_{i}'})
    s.emit('message', f'问题 {i}', actor='owner', target='master', visible=True, data={'id': f'm{i}', 'priority': 9})
    s.emit('native:thread/tokenUsage/updated', actor='master', data={'pad': 'x' * 2000})
    s.emit('chat', f'回答 {i}', actor='master', visible=True)
s.close()`]);
  const server = spawn(py, ['-m', 'cthulhu_v4', 'center', '--state', state, '--port', '0', '--ws-port', '0'], { cwd: root });
  let browser;
  try {
    const url = await new Promise((ok, bad) => { let t = ''; const timer = setTimeout(() => bad(Error('server timeout')), 10000);
      server.stdout.on('data', b => { t += b; const m = t.match(/http:\/\/127\.0\.0\.1:\d+/); if (m) { clearTimeout(timer); ok(m[0]); } }); });
    const token = fs.readFileSync(path.join(state, 'web-token'), 'utf8').trim();
    browser = await chromium.launch({ args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1280, height: 800 } }); page.setDefaultTimeout(15000);
    let bytes = 0; page.on('response', async r => { if (r.url().includes('/api/events')) bytes += (await r.body()).length; });
    await page.goto(url + '/'); await page.locator('#login-token').fill(token); await page.locator('#login-form button').click();
    await page.locator('#chat .reply').getByText('回答 299', { exact: true }).waitFor();
    await page.waitForTimeout(1500);
    const first = await page.locator('#chat .me').count();
    assert(first > 0 && first < 100, `首屏应只加载一页，实际 ${first} 条`);
    assert(await page.locator('#chat').evaluate(n => n.scrollHeight - n.scrollTop - n.clientHeight < 10), '首屏应落在底部');
    assert(bytes < 200000, `首屏不应传输遥测与整段历史，实际 ${bytes} 字节`);
    // 向上滚动：加载更早一页，视线不跳。
    await page.locator('#chat').evaluate(n => (n.scrollTop = 0));
    await page.waitForFunction(n => document.querySelectorAll('#chat .me').length > n, first);
    const anchor = await page.locator('#chat').evaluate(n => n.scrollTop);
    assert(anchor > 0, '加载更早记录后应保持原阅读位置');
    // 一直向上翻到开头，顺序完整、无重复。
    for (let i = 0; i < 10 && !(await page.locator('#chat .older').isHidden()); i++) {
      await page.locator('#chat').evaluate(n => (n.scrollTop = 0)); await page.waitForTimeout(400);
    }
    const texts = await page.locator('#chat .me').allInnerTexts();
    assert.equal(texts.length, 300);
    assert.equal(new Set(texts).size, 300);
    assert(texts[0].startsWith('问题 0') && texts[299].startsWith('问题 299'));
    console.log('ui_paging: ok');
  } finally { if (browser) await browser.close(); server.kill(); fs.rmSync(state, { recursive: true, force: true }); }
})().catch(e => { console.error(e); process.exit(1); });
