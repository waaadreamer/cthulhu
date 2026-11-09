// 单 Canvas 工作区：真实隔离中心与浏览器，只验证选择/布局，不启动模型或采集。
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
const artifacts = process.env.CANVAS_NETWORK_ARTIFACTS || '/tmp/cthulhu-canvas-network';
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
(async()=>{const state=fs.mkdtempSync(path.join(os.tmpdir(),'cthulhu-network-'));let center,collector,browser;
try{
 center=launch(['center','--state',path.join(state,'center'),'--port','0','--ws-port','0','--git-proxy','http://127.0.0.1:1']);const url=await center.ready,token=fs.readFileSync(path.join(state,'center','web-token'),'utf8').trim();
 collector=launch(['canvas','--center',url,'--token-file',path.join(state,'center','web-token'),'--port','0','--tick-interval','.1']);await collector.ready;
 browser=await chromium.launch({executablePath:process.env.PLAYWRIGHT_EXECUTABLE||undefined,args:['--no-sandbox']});const context=await browser.newContext({viewport:{width:1440,height:1000}}),p=await context.newPage();p.setDefaultTimeout(15000);
 const api=async(route,data)=>{let r=await p.request.fetch(url+route,{method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token},data});assert.ok(r.ok(),await r.text());return r.json()};
 fs.writeFileSync(path.join(state,'data.json'),JSON.stringify({message:'真实隔离文件读取，非训练数据',value:'bounded fixture '.repeat(9000)}));
 const board=await api('/api/canvas',{name:'网络验收fixture',node_id:'local',cwd:state,script:'cat '+path.join(state,'data.json'),interval:86400,timeout:2,task_ids:[],html_template:'<input id="filter" value="keep"><details id="detail"><summary>详情</summary><pre id="data"></pre></details><script>addEventListener("canvas:update",e=>document.getElementById("data").textContent=e.detail.data?.message||"等待")</script>'});
 for(let i=0;i<80;i++){let d=await api('/api/canvas/'+board.id+'/data');if(d.data)break;await p.waitForTimeout(50)}await api('/api/canvas/'+board.id+'/pause',{});
 const cdp=await context.newCDPSession(p);await cdp.send('Network.enable');let responses=[],requests=[],errors=[];cdp.on('Network.responseReceived',e=>{if(new URL(e.response.url).pathname.startsWith('/api/canvas'))responses.push({path:new URL(e.response.url).pathname,status:e.response.status})});p.on('request',r=>{if(new URL(r.url()).pathname.startsWith('/api/canvas'))requests.push({path:new URL(r.url()).pathname,conditional:Boolean(r.headers()['if-none-match'])})});p.on('pageerror',e=>errors.push(e.message));
 await p.goto(url+'/boards.html');await p.locator('#board-token').fill(token);await p.locator('#board-login-form button').click();let f=await p.locator('.board-card:not([hidden]) iframe').elementHandle().then(e=>e.contentFrame());await f.waitForFunction(()=>Canvas.getData().data);assert.equal(await f.evaluate(()=>Canvas.getData().data.value.length),144000);assert.equal(await f.evaluate(()=>JSON.parse(Canvas.getData().attempt.stdout).message),'真实隔离文件读取，非训练数据');
 await f.locator('#detail>summary').click();await f.locator('#filter').fill('keep-focus');await f.locator('#filter').focus();await f.evaluate(()=>window.networkMarker=42);
 await p.waitForTimeout(4400);assert.ok(responses.some(r=>r.status===304));assert.equal(await f.locator('#filter').inputValue(),'keep-focus');assert.equal(await f.locator('#detail').evaluate(e=>e.open),true);assert.equal(await f.evaluate(()=>document.activeElement.id),'filter');
 await context.setOffline(true);await p.locator('.board-card .board-status').getByText('中心连接中断',{exact:false}).waitFor();await context.setOffline(false);await p.waitForFunction(()=>document.querySelector('.board-card .board-status').textContent==='已暂停');assert.equal(await f.evaluate(()=>window.networkMarker),42);
 for(const width of [1440,390]){
  await p.setViewportSize({width,height:1000});await p.evaluate(()=>{Object.defineProperty(document,'hidden',{configurable:true,get:()=>true});document.dispatchEvent(new Event('visibilitychange'))});await p.waitForTimeout(150);let count=requests.length;await p.waitForTimeout(2400);assert.equal(requests.length,count);
  const started=Date.now(),next=p.waitForRequest(r=>new URL(r.url()).pathname==='/api/canvas');await p.evaluate(()=>{delete document.hidden;document.dispatchEvent(new Event('visibilitychange'))});await next;assert.ok(Date.now()-started<1000);await p.waitForTimeout(300);assert.equal(await f.locator('#filter').inputValue(),'keep-focus');assert.equal(await f.locator('#detail').evaluate(e=>e.open),true);assert.equal(await f.evaluate(()=>window.networkMarker),42);
 }
 // 身份切换必须重新认证；旧304不能替代凭证检查。
 await p.evaluate(()=>localStorage.setItem('cthulhu-token','invalid-identity'));await p.locator('#board-login-error').getByText('凭证无效',{exact:false}).waitFor();await p.locator('#board-token').fill(token);await p.locator('#board-login-form button').click();await p.locator('#board-login').waitFor({state:'hidden'});f=await p.locator('.board-card:not([hidden]) iframe').elementHandle().then(e=>e.contentFrame());await f.waitForFunction(()=>Canvas?.getData().data);assert.equal(await f.evaluate(()=>window.networkMarker),undefined);
 assert.deepEqual(errors,[]);console.log('PASS compact exact stdout/data, private304, offline/reconnect, identity change, hidden/no GET and immediate resume, 1440/390 state preserved');
}finally{if(browser)await browser.close();await stop(collector?.proc);await stop(center?.proc);fs.rmSync(state,{recursive:true,force:true})}})().catch(e=>{console.error(e);process.exitCode=1});
