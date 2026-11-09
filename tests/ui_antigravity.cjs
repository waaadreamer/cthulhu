// 手动浏览器验收，隔离 demo 中心 + 目录证据回放；不启动模型，不写生产状态。
// AGY_MODELS_FILE 可提供真实 agy models 的 stdout，缺省使用明确的协议夹具。
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert');
(async () => {
  const root = path.resolve(__dirname, '..'), tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'cthulhu-agy-ui-'));
  const proof = process.env.AGY_MODELS_FILE;
  const list = (proof ? fs.readFileSync(proof, 'utf8') : 'gemini-fixture-high\tProtocol fixture\n').trim().split('\n').map(line => {
    const [id, name] = line.split('\t'); return { id, name, availability: 'unavailable' };
  });
  fs.writeFileSync(path.join(tmp, 'catalog.json'), JSON.stringify(list));
  let evidence = {};
  if (process.env.AGY_FAILURE_FILE) {
    const frames = fs.readFileSync(process.env.AGY_FAILURE_FILE, 'utf8').trim().split('\n').map(JSON.parse);
    const failure = frames.find(f => f.event === 'result')?.result;
    assert.equal(failure?.status, 'ERROR'); assert.match(failure.error, /不支持 Gemini 协议/);
    evidence = { inference_status: 'protocol_unsupported', inference_error: failure.error,
      inference_model: process.env.AGY_FAILURE_MODEL || frames.find(f => f.event === 'init')?.init?.model,
      inference_at: fs.statSync(process.env.AGY_FAILURE_FILE).mtimeMs / 1000 };
  }
  fs.writeFileSync(path.join(tmp, 'evidence.json'), JSON.stringify(evidence));
  const code = `
import asyncio, json, sys, time
from pathlib import Path
from cthulhu_v4.app import App
from cthulhu_v4.antigravity import SOURCE
from cthulhu_v4 import node
async def main():
 app=App(sys.argv[1], port=0, ws_port=0)
 async def discover(*a, **kw):
  await asyncio.sleep(.2)
  return {'antigravity': {**json.loads((Path(sys.argv[1])/'evidence.json').read_text()),'list':json.loads((Path(sys.argv[1])/'catalog.json').read_text()),'at':time.time(),'source':SOURCE,'status':'unavailable','error':'继承 Claude 的渠道目录未报告这些 Gemini 模型；尚不能确认调用能力，需核验 Gemini 协议与额度'}}
 node.discover=discover
 await app.start()
 app.node.backends=['demo','antigravity']
 app.node.discover_models=True
 app.deployer.check=lambda: {}
 print(app.web.url,flush=True)
 try: await asyncio.Event().wait()
 finally: await app.stop()
asyncio.run(main())
`;
  const server = spawn(process.env.PYTHON || 'python3', ['-u', '-c', code, tmp], { cwd: root });
  let browser; const errors = [];
  try {
    const url = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(Error('server startup timeout')), 15000);
      server.stdout.on('data', b => { const m = b.toString().match(/http:\/\/127\.0\.0\.1:\d+/); if (m) { clearTimeout(timer); resolve(m[0]); } });
      server.stderr.on('data', b => process.stderr.write(b)); server.on('exit', c => reject(Error('server exit ' + c)));
    });
    browser = await chromium.launch({ executablePath: process.env.PLAYWRIGHT_EXECUTABLE, args: ['--no-sandbox'] });
    const page = await browser.newPage({ viewport: { width: 1440, height: 1100 } });
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(url);
    await page.locator('#login-token').fill(fs.readFileSync(path.join(tmp, 'web-token'), 'utf8').trim());
    await page.locator('#login-form button').click();
    await page.locator('#nodes button').first().click();
    await page.locator('#node-dialog[open]').waitFor();
    await page.locator('#node-form [name=backend]').selectOption('antigravity');
    await page.locator('#node-catalog').getByText('渠道目录未列出候选模型', { exact: false }).waitFor();
    assert.equal(await page.locator('#node-pick [data-value^=gemini-]').count(), list.length);
    if (evidence.inference_status) assert.match(await page.locator('#node-catalog').innerText(), /原生验证失败：该渠道不支持 Gemini 协议/);
    const selected = list.find(m => m.id === 'gemini-4-argon-medium') || list[0];
    await page.locator(`#node-pick [data-value="${selected.id}"]`).click();
    await page.locator('#node-model-refresh').click();
    await page.locator('#node-model-refresh').getByText('刷新中…', { exact: true }).waitFor();
    await page.locator('#node-model-refresh').getByText('刷新目录', { exact: true }).waitFor();
    assert.equal(await page.locator('#node-form [name=model]').inputValue(), selected.id, '刷新不应覆盖未保存选择');
    const screenshot = process.env.AGY_SCREENSHOT || path.join(tmp, 'antigravity-models.png');
    await page.screenshot({ path: screenshot, fullPage: true });
    const save = page.waitForRequest(r => r.url().endsWith('/api/node') && r.method() === 'POST');
    await page.locator('#node-form button[type=submit]').click();
    const saved = (await save).postDataJSON();
    assert.equal(saved.backend, 'antigravity'); assert.equal(saved.model, selected.id);
    await page.locator('#node-dialog').waitFor({ state: 'hidden' });
    await page.locator('#new-task').click();
    await page.locator('#task-form [name=backend]').selectOption('antigravity');
    assert.equal(await page.locator('#task-models option').count(), list.length);
    assert.match(await page.locator('#task-catalog').innerText(), /不代表渠道已可调用/);
    await page.locator('#task-dialog [data-close]').click();
    await page.locator('#model').click();
    await page.locator('#model-form [name=backend]').selectOption('antigravity');
    assert.match(await page.locator('#model-catalog').innerText(), /渠道目录未列出候选模型/);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ result: 'PASS', catalog_source: proof || 'protocol fixture', models: list.length,
      selected_model: selected.id, screenshot, scope: 'isolated real browser UI; no production changes or inference' }));
  } finally {
    await browser?.close(); server.kill('SIGTERM');
    await new Promise(resolve => server.exitCode != null ? resolve() : server.once('exit', resolve));
  }
})().catch(e => { console.error(e); process.exit(1); });
