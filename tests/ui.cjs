// 可选浏览器验收。依赖仅用于测试，主应用仍为零依赖。
// PLAYWRIGHT_PATH=/path/to/playwright node tests/ui.cjs
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const {spawn}=require('child_process');const fs=require('fs');const os=require('os');const path=require('path');const assert=require('assert');
(async()=>{
 const root=path.resolve(__dirname,'..'),state=fs.mkdtempSync(path.join(os.tmpdir(),'cthulhu-v4-ui-'));
 const server=spawn('python3',['-m','cthulhu_v4','--state',state,'--port','0','--heartbeat','1','--question-timeout','2','--review-interval','3'],{cwd:root});
 let browser;
 try{
  const url=await new Promise((resolve,reject)=>{let text='';const timer=setTimeout(()=>reject(Error('server timeout')),10000);server.stdout.on('data',b=>{text+=b;const m=text.match(/http:\/\/127\.0\.0\.1:\d+/);if(m){clearTimeout(timer);resolve(m[0]);}});server.on('exit',code=>reject(Error('server exit '+code)));});
  const token=fs.readFileSync(path.join(state,'web-token'),'utf8').trim();
  browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:960}});page.setDefaultTimeout(10000);const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(t=>sessionStorage.setItem('cthulhu-token',t),token);
  await page.goto(url);await page.getByText('暂无任务。',{exact:false}).waitFor();
  await page.locator('#input').fill('你好，先做一个可审核的试用。');await page.locator('#urgent').click();
  const urgentRequest=page.waitForRequest(r=>r.url().endsWith('/api/chat')&&r.method()==='POST');
  await page.locator('#chat-form .send-button').click();assert.equal((await urgentRequest).postDataJSON().priority,10);
  await page.locator('#chat').getByText('[模拟 Master]',{exact:false}).first().waitFor();
  await page.locator('#new-task').click();await page.locator('[name=title]').fill('试用问答流程');await page.locator('[name=description]').fill('只运行模拟任务');await page.locator('[name=acceptance]').fill('问题得到回答并完成');await page.locator('#task-form .primary-button').click();
  await page.getByText('模拟：请我回答并计时',{exact:true}).waitFor();await page.getByText('模拟：请我回答并计时',{exact:true}).click();
  await page.locator('#chat').getByText('等待主人超时，已交回 master 决定。',{exact:true}).waitFor({timeout:15000});
  page.once('dialog',d=>d.accept('继续'));await page.getByText('我来回答',{exact:true}).click();
  await page.locator('#tasks').getByText('已完成',{exact:true}).waitFor({timeout:15000});
  await page.locator('#tasks').getByText('CoT ↗',{exact:true}).click();
  await page.locator('#timeline .transcript-row').first().waitFor();
  assert(await page.locator('#timeline').innerText().then(t=>t.includes('你')&&t.includes('Master')));
  await page.locator('#agent-input').fill('侧栏消息验收');await page.locator('#agent-urgent').click();
  const agentRequest=page.waitForRequest(r=>r.url().endsWith('/api/chat')&&r.method()==='POST');
  await page.locator('#agent-form .send-button').click();assert.equal((await agentRequest).postDataJSON().priority,10);
  await page.waitForFunction(()=>document.getElementById('agent-input').value==='');
  const previous=await page.locator('#chat').evaluate(e=>{e.scrollTop=0;return e.scrollTop;});
  await page.locator('#timeline').evaluate(e=>e.scrollTop=e.scrollHeight);
  assert.equal(await page.locator('#chat').evaluate(e=>e.scrollTop),previous);
  await page.screenshot({path:path.join(root,'preview-demo-flow.png'),fullPage:true});
  await page.evaluate(()=>{openActivity('old_stream','流式展示测试');appendActivity([{seq:999001,at:1,actor:'old_stream',kind:'native:item/agentMessage/delta',text:'',data:{itemId:'stream-test',delta:'你好'}},{seq:999002,at:1,actor:'old_stream',kind:'native:item/agentMessage/delta',text:'',data:{itemId:'stream-test',delta:'世界'}}]);});
  assert.equal(await page.locator('#timeline .transcript-row').count(),1);
  assert((await page.locator('#timeline').innerText()).includes('你好世界'));
  await page.setViewportSize({width:390,height:844});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.locator('#close-activity').click();
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('PASS: chat, task, timeout, answer, completion, source labels, independent scroll, mobile viewport');
 }finally{if(browser)await browser.close();server.kill('SIGTERM');await new Promise(r=>server.once('exit',r));fs.rmSync(state,{recursive:true,force:true});}
})().catch(e=>{console.error(e);process.exitCode=1;});
