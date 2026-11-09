// 数据刷新视图事务：真实隔离中心与浏览器；fixture 消息不运行模型或采集。
const { chromium } = require(process.env.PLAYWRIGHT_PATH || 'playwright');
const { spawn } = require('child_process');
const fs = require('fs'), os = require('os'), path = require('path'), assert = require('assert/strict');
const root = path.resolve(__dirname, '..');
const py = fs.existsSync(path.join(root, '.venv/bin/python')) ? path.join(root, '.venv/bin/python') : 'python3';
const artifacts = process.env.CANVAS_REFRESH_ARTIFACTS || '/tmp/cthulhu-canvas-refresh';
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
// 明确的交互 fixture，只验证数据消息事务，不运行采集或模型。
(async () => {
 const state=fs.mkdtempSync(path.join(os.tmpdir(),'cthulhu-canvas-refresh-'));let center,browser;
 try {
 center=launch(['center','--state',path.join(state,'center'),'--port','0','--ws-port','0','--git-proxy','http://127.0.0.1:1']);const url=await center.ready;
 const token=fs.readFileSync(path.join(state,'center','web-token'),'utf8').trim();
 browser=await chromium.launch({executablePath:process.env.PLAYWRIGHT_EXECUTABLE||undefined,args:['--no-sandbox']});
 const page=await browser.newPage({viewport:{width:1440,height:1000}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const api=async(route,data)=>{let r=await page.request.fetch(url+route,{method:data===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token},data});assert.ok(r.ok(),await r.text());return r.json()};
 const template=`<style>body{margin:0;height:2400px}#scroll{width:220px;height:100px;overflow:auto}#inside{width:1200px;height:500px}#host{padding-top:150px}svg{width:400px;height:150px}</style><h1>刷新交互 fixture</h1><output id="count"></output><div id="host"></div><script>
 let value='draft',version=0;function draw(s){version=s.data?.n||0;document.getElementById('count').textContent=version;const host=document.getElementById('host');host.innerHTML='<details id="outer"><summary>来源</summary><details data-canvas-key="inner"><summary>子项</summary><input id="draft"><div id="scroll"><div id="inside">'+version+'</div></div></details></details><details id="lazy"><summary>惰性来源</summary></details><svg data-chart="series"><circle data-task="'+(s.data?.gone?'new':'original')+'" tabindex="0" r="25" cx="80" cy="80"/></svg>';let input=document.getElementById('draft');input.value=value;input.oninput=()=>value=input.value;const lazy=document.getElementById('lazy');lazy.addEventListener('toggle',()=>{if(lazy.open&&!lazy.children[1]){let scroll=document.createElement('div');scroll.id='lazy-scroll';scroll.style.cssText='height:80px;width:200px;overflow:auto';scroll.innerHTML='<div style="height:500px;width:600px">lazy '+version+'</div>';const nested=document.createElement('details');nested.id='lazy-inner';nested.innerHTML='<summary>内层惰性详情</summary>';nested.append(scroll);lazy.append(nested)}});}
 draw({data:{n:0}});addEventListener('canvas:update',e=>{if(!e.detail.data)return;if(e.detail.data?.async)Canvas.preserveView(async()=>{await new Promise(r=>setTimeout(r,80));draw(e.detail)});else draw(e.detail)});
 </script>`;
 const board=await api('/api/canvas',{name:'刷新状态 fixture',node_id:'local',cwd:state,script:'printf unused',interval:60,timeout:2,task_ids:[],html_template:template});await api('/api/canvas/'+board.id+'/pause',{});
 await page.goto(url+'/boards.html');await page.locator('#board-token').fill(token);await page.locator('#board-login-form button').click();
 let f=await page.locator('.board-card:visible iframe').elementHandle().then(e=>e.contentFrame());await f.locator('#outer').waitFor();
 const send=async(data)=>{await page.evaluate(data=>document.querySelector('.board-card:not([hidden]) iframe').contentWindow.postMessage({type:'cthulhu:canvas:data',snapshot:{data}},'*'),data);await f.waitForFunction(n=>document.querySelector('#count').textContent===String(n),data.n);await page.waitForTimeout(100)};
 const capture=()=>f.evaluate(()=>({draft:document.querySelector('#draft').value,focus:document.activeElement.id,start:document.activeElement.selectionStart,end:document.activeElement.selectionEnd,details:[...document.querySelectorAll('details')].map(n=>n.open),x:document.querySelector('#scroll').scrollLeft,y:document.querySelector('#scroll').scrollTop,windowY:scrollY,lazyX:document.querySelector('#lazy-scroll')?.scrollLeft,lazyY:document.querySelector('#lazy-scroll')?.scrollTop}));
 for(const width of [1440,390]){
 await page.setViewportSize({width,height:1000});await f.locator('#outer > summary').click();await f.locator('#outer details > summary').click();await f.locator('#lazy > summary').click();await f.locator('#lazy-inner > summary').click();await f.locator('#lazy-scroll').waitFor();await f.locator('#draft').fill('typed draft');await f.locator('#draft').focus();await f.evaluate(()=>{document.querySelector('#draft').setSelectionRange(2,7);document.querySelector('#scroll').scrollTo(120,80);document.querySelector('#lazy-scroll').scrollTo(60,45);scrollTo(0,130)});
 const before=await capture();for(let n=1;n<=2;n++){await send({n,width,async:n===2});assert.deepEqual(await capture(),before)}
 console.log('PASS desktop/narrow synchronous + asynchronous data values advance; draft/focus/caret/details/inner+outer scroll preserved',width);
 // 数据项消失不能按位置把焦点恢复到另一项。
 await f.locator('circle').focus();await send({n:3,gone:true});assert.equal(await f.evaluate(()=>document.activeElement.dataset.task||null),null);
 // 真实模板内容变化应重建上下文，普通配置版本变化则不应。
 await f.evaluate(()=>window.refreshMarker=42);const current=await api('/api/canvas/'+board.id);await api('/api/canvas/'+board.id,{...current,name:'重命名',revision:current.revision});await page.waitForTimeout(2400);assert.equal(await f.evaluate(()=>window.refreshMarker),42);
 if(width===1440)await f.evaluate(()=>{document.querySelector('details details').open=false;document.querySelector('#outer').open=false;document.querySelector('#lazy').open=false;document.querySelector('#lazy-inner').open=false});
 }
 // 等待异步渲染期间的新滚轮动作优先于旧事务捕获的位置。
 await f.evaluate(()=>{window.pendingView=Canvas.preserveView(async()=>{await new Promise(r=>setTimeout(r,250));document.querySelector('#count').textContent='user wins'});});
 const box=await page.locator('.board-card:visible iframe').boundingBox();await page.mouse.move(box.x+box.width/2,box.y+box.height-100);await page.mouse.wheel(0,180);await page.waitForTimeout(120);const newScroll=await f.evaluate(()=>scrollY);await f.evaluate(()=>window.pendingView);assert.equal(await f.evaluate(()=>scrollY),newScroll);console.log('PASS user scroll during async update is not overwritten');
 const current=await api('/api/canvas/'+board.id);await api('/api/canvas/'+board.id,{...current,revision:current.revision,html_template:template+'<p id="changed">真实模板版本变化</p>'});await page.waitForTimeout(2500);f=await page.locator('.board-card:visible iframe').elementHandle().then(e=>e.contentFrame());await f.locator('#changed').waitFor();assert.equal(await f.evaluate(()=>window.refreshMarker),undefined);

 // 项目默认汇总模板：明确标注的合成 fixture 验证长数据更新，非线上训练结果。
 const summary=await api('/api/canvas',{name:'默认汇总 · 合成刷新fixture',node_id:'local',cwd:state,script:'printf unused',interval:60,timeout:2,task_ids:[],html_template:fs.readFileSync(path.join(root,'static/examples/canvas-summary.html'),'utf8')});await api('/api/canvas/'+summary.id+'/pause',{});
 let defaultNumber=0;
 // 回放走外壳原有列表→data GET链；轮询也读同一fixture，避免测试消息被旧空快照覆盖。
 await page.route('**/api/canvas',async route=>{const response=await route.fetch(),body=await response.json();body.canvases.find(b=>b.id===summary.id).data_stamp=['default-fixture',defaultNumber];await route.fulfill({response,json:body})});
 await page.route('**/api/canvas/'+summary.id+'/data',async route=>{const response=await route.fetch(),body=await response.json();body.format='json';body.data={demo:true,source:'合成刷新 fixture，非训练结果',experiments:Array.from({length:40},(_,i)=>({name:'合成实验 '+i,path:'/fixture/'+i,status:'fixture',metrics:{completed:defaultNumber},file_modified_at:1}))};await route.fulfill({response,json:body})});

 if(await page.locator('#boards-list-toggle').isVisible())await page.locator('#boards-list-toggle').click();
 await page.locator('.board-nav-item[data-canvas-id="'+summary.id+'"]').click();await page.waitForTimeout(200);f=await page.locator('.board-card:visible iframe').elementHandle().then(e=>e.contentFrame());await f.waitForFunction(()=>window.Canvas);
 for(const width of [1440,390]){
  await page.setViewportSize({width,height:1000});
  const sample=async n=>{defaultNumber=n;await f.waitForFunction(n=>document.querySelector('#rows td:nth-child(2)')?.textContent.includes('"completed":'+n),n);await page.waitForTimeout(100)};
  await sample(3);await f.locator('#rows tr').nth(20).scrollIntoViewIfNeeded();const position=await f.evaluate(()=>scrollY);await page.screenshot({path:path.join(artifacts,'default-'+width+'-before.png')});
  for(const n of [4,5]){await sample(n);assert.equal(await f.evaluate(()=>scrollY),position);assert.equal(await page.locator('.board-card:visible').getAttribute('data-canvas-id'),summary.id)}
  await page.screenshot({path:path.join(artifacts,'default-'+width+'-after.png')});console.log('PASS default summary fixture completed 3→4→5 with selection/scroll preserved',width);
 }
 assert.deepEqual(errors,[]);console.log('PASS template vs configuration changes; no stale focus identity; pageerror=[]');
 } finally {if(browser)await browser.close();await stop(center?.proc);fs.rmSync(state,{recursive:true,force:true})}
})().catch(e=>{console.error(e);process.exitCode=1});
