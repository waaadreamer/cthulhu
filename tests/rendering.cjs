// 浏览器协议回归 + 可审核预览。仅使用合成原生帧，不启动模型或生产服务。
const {chromium}=require(process.env.PLAYWRIGHT_PATH||'playwright');
const fs=require('fs'),path=require('path'),assert=require('assert');
const root=path.resolve(__dirname,'..');
let seq=0;
const event=(actor,kind,data={},text='',extra={})=>({seq:++seq,at:1792251000+seq,actor,kind,data,text,visible:false,...extra});
const native=(actor,method,data,turn='turn-1')=>event(actor,'native:'+method,{threadId:'session-'+actor,turnId:turn,...data});
const item=(actor,value,done=true,turn='turn-1')=>native(actor,'item/'+(done?'completed':'started'),{item:value},turn);
const base=1792251000;
const tasks=[
 {id:'t_research',agent:'old_research',title:'核查 S 集群预训练状态',node:'galaxy-01',state:'running',deadline:base+3600},
 {id:'t_ui',agent:'old_ui',title:'优化控制台的会话阅读体验',node:'galaxy-01',state:'waiting',deadline:base+7200},
 {id:'t_docs',agent:'old_docs',title:'梳理部署说明与接入步骤',node:'alpha',state:'completed',deadline:base+1800}
];
const snapshot={agents:[{id:'master',backend:'codex',model:'gpt-6-astra',reasoning_effort:'medium',status:'idle',paused:false},...tasks.map(t=>({id:t.agent,backend:'codex',status:'running'}))],tasks,nodes:[{id:'galaxy-01',online:true},{id:'alpha',online:true}],questions:[],memories:[{text:'优先提供可审核的小步改动',source:'主人明确要求',inferred:false}],seq:0,now:base};
const chat=[
 event('owner','message',{id:'owner-1',sender:'owner',target:'master'},'看看 S 集群上 30 个节点的训练进展。先只读检查，有异常及时告诉我。',{target:'master'}),
 item('master',{id:'comment-1',type:'agentMessage',phase:'commentary',text:'我会先确认执行节点与工作目录，再让一个独立的 old_one 读取训练状态。核查过程会实时显示在任务侧栏。'}),
 item('master',{id:'tool-1',type:'mcpToolCall',server:'cthulhu',tool:'list_nodes',arguments:{},status:'completed',result:{content:[{type:'text',text:'galaxy-01 在线 · Codex 可用\nalpha 在线 · Codex / Claude 可用'}]}}),
 event('master','task_created',{},'已分配「核查 S 集群预训练状态」给 galaxy-01 上的独立执行者。'),
 item('master',{id:'comment-2',type:'agentMessage',phase:'commentary',text:'执行者已开始检查。我会关注实际节点数、最近的训练步数和错误日志；目前没有做任何训练配置变更。'}),
 item('master',{id:'final-1',type:'agentMessage',phase:'final_answer',text:'核查已经开始。\n\n- **执行节点**：galaxy-01\n- **检查范围**：作业状态、实际节点数、训练进度\n- **操作边界**：只读，不重启、不修改训练\n\n你可以打开左侧任务的 **CoT** 查看完整过程，也可以继续在这里补充要求。'}),
 event('master','chat',{},'核查已经开始。\n\n- **执行节点**：galaxy-01\n- **检查范围**：作业状态、实际节点数、训练进度\n- **操作边界**：只读，不重启、不修改训练\n\n你可以打开左侧任务的 **CoT** 查看完整过程，也可以继续在这里补充要求。')
];
const worker=[
 event('master','message',{id:'assign-1',sender:'master',target:'old_research'},'只读核查 S 集群训练状态。先确认 envs/hg.yaml，再检查作业、节点数和训练日志。',{target:'old_research'}),
 item('old_research',{id:'work-1',type:'agentMessage',phase:'commentary',text:'先阅读项目的集群操作说明，确认当前环境和只读查询入口。'}),
 item('old_research',{id:'cmd-1',type:'commandExecution',command:'pwd && sed -n \'1,80p\' docs/runbooks/training.md',status:'completed',exitCode:0,aggregatedOutput:'/home/wangzeyuan/ss-embodied\n环境：hg\n查询入口：squeue / sacct\n本次仅检查状态，不提交或修改训练作业。'}),
 item('old_research',{id:'work-2',type:'agentMessage',phase:'commentary',text:'工作目录与环境一致。接下来核对作业的实际分配节点，并查看最后一段训练日志。'}),
 item('old_research',{id:'cmd-2',type:'commandExecution',command:'squeue --user "$USER" --format="%.18i %.12j %.8T %.6D"',status:'completed',exitCode:0,aggregatedOutput:'JOBID              NAME         STATE    NODES\n482139             pretrain-30  RUNNING  30'}),
 event('old_research','message',{id:'report-1',sender:'old_research',target:'master'},'已确认作业处于 RUNNING，分配节点数为 30；正在核对最新训练步数与日志时间。',{target:'master'})
];
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
 try{
  const page=await browser.newPage({viewport:{width:1512,height:982},deviceScaleFactor:1});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>sessionStorage.setItem('cthulhu-token','fixture-only'));
  await page.route('http://preview.local/**',async route=>{
   const u=new URL(route.request().url());
   if(u.pathname==='/api/state')return route.fulfill({json:snapshot});
   if(u.pathname==='/api/events'){
    const stream=u.searchParams.has('agent')?worker:chat,after=Number(u.searchParams.get('after')||0);
    return route.fulfill({json:{events:stream.filter(e=>e.seq>after)}});
   }
   const file=u.pathname==='/'?'index.html':u.pathname.slice(1);
   if(!['index.html','app.js','style.css','transcript.js'].includes(file))return route.fulfill({status:404,body:''});
   return route.fulfill({body:fs.readFileSync(path.join(root,'static',file)),contentType:file.endsWith('js')?'text/javascript':file.endsWith('css')?'text/css':'text/html'});
  });
  await page.goto('http://preview.local/');await page.locator('#chat .transcript-row').first().waitFor();
  assert((await page.locator('#chat').innerText()).includes('我会先确认执行节点'));
  assert.equal(await page.locator('#chat').getByText('核查已经开始。',{exact:true}).count(),1,'final + chat must not duplicate');
  await page.locator('#urgent').click();assert.equal(await page.locator('#urgent').getAttribute('aria-pressed'),'true');assert.equal(await page.locator('#send-hint').innerText(),'立即打断当前回合');
  await page.screenshot({path:path.join(root,'preview-master.png'),fullPage:true});
  await page.locator('#normal').click();await page.locator('.view-task').first().click();await page.locator('#timeline .command').first().waitFor();
  assert((await page.locator('#timeline').innerText()).includes('squeue --user'));
  assert(!(await page.locator('#timeline').innerText()).includes('threadId'));
  await page.screenshot({path:path.join(root,'preview.png'),fullPage:true});
  // 直接向独立渲染器投喂完整协议边界：stream → complete、同 ID 不同 turn、工具、diff。
  const results=await page.evaluate(()=>{
   const container=document.createElement('div');document.body.append(container);const v=new Transcript(container,'old_test');let n=100000;
   const ev=(method,data,turn='a')=>({seq:++n,at:1,actor:'old_test',kind:'native:'+method,text:'',data:{threadId:'s',turnId:turn,...data}});
   const records=[
    ev('item/started',{item:{id:'text',type:'agentMessage',text:'',phase:'commentary'}}),
    ev('item/agentMessage/delta',{itemId:'text',delta:'正在'}),ev('item/agentMessage/delta',{itemId:'text',delta:'检查'}),
    ev('item/completed',{item:{id:'text',type:'agentMessage',text:'正在检查',phase:'commentary'}}),
    ev('item/completed',{item:{id:'text',type:'agentMessage',text:'另一个回合',phase:'final_answer'}},'b'),
    ev('item/started',{item:{id:'cmd',type:'commandExecution',command:'printf hello',status:'inProgress'}}),
    ev('item/commandExecution/outputDelta',{itemId:'cmd',delta:'hello'}),
    ev('item/completed',{item:{id:'cmd',type:'commandExecution',command:'printf hello',aggregatedOutput:'hello',status:'completed',exitCode:0}}),
    ev('item/reasoning/summaryTextDelta',{itemId:'reason',delta:'公开摘要'}),
    ev('item/completed',{item:{id:'reason',type:'reasoning',summary:['公开摘要'],content:[]}}),
    ev('item/completed',{item:{id:'empty',type:'reasoning',summary:[],content:[]}}),
    ev('item/completed',{item:{id:'diff',type:'fileChange',status:'completed',changes:[{path:'app.py',diff:'-old\n+new'}]}}),
    ev('item/completed',{item:{id:'danger',type:'agentMessage',text:'<img src=x onerror="window.evil=true"> [link](javascript:alert(1))',phase:'final_answer'}}),
    ev('thread/tokenUsage/updated',{tokenUsage:{total:500}})
   ];v.append(records);v.append(records);
   const codex={count:container.querySelectorAll('.transcript-row').length,text:container.innerText,command:container.querySelector('.command').innerText,summary:container.innerText.split('公开摘要').length-1,diff:container.querySelector('.diff').innerText,unsafe:!!container.querySelector('img,a[href^="javascript:"]')};
   container.replaceChildren();const c=new Transcript(container,'old_test');
   const ce=(method,data)=>ev(method,data);
   c.append([
    ce('stream_event',{event:{type:'message_start',message:{id:'claude-msg'}}}),
    ce('stream_event',{event:{type:'content_block_start',index:0,content_block:{type:'thinking',thinking:''}}}),
    ce('stream_event',{event:{type:'content_block_delta',index:0,delta:{type:'thinking_delta',thinking:'公开思考片段'}}}),
    ce('stream_event',{event:{type:'content_block_start',index:1,content_block:{type:'text',text:''}}}),
    ce('stream_event',{event:{type:'content_block_delta',index:1,delta:{type:'text_delta',text:'已完成核验。'}}}),
    ce('stream_event',{event:{type:'content_block_start',index:2,content_block:{type:'tool_use',id:'bash-1',name:'Bash',input:{}}}}),
    ce('stream_event',{event:{type:'content_block_delta',index:2,delta:{type:'input_json_delta',partial_json:'{"command":"pwd"}'}}}),
    ce('assistant',{message:{id:'claude-msg',content:[{type:'thinking',thinking:'公开思考片段'},{type:'text',text:'已完成核验。'},{type:'tool_use',id:'bash-1',name:'Bash',input:{command:'pwd'}}]}}),
    ce('user',{message:{content:[{type:'tool_result',tool_use_id:'bash-1',content:'/work/project'}]}}),
    ce('result',{result:'已完成核验。',is_error:false})
   ]);
   const claude={count:container.querySelectorAll('.transcript-row').length,text:container.innerText};container.remove();return {codex,claude};
  });
  assert.equal(results.codex.count,6,JSON.stringify(results.codex));assert.equal(results.codex.summary,1);assert(results.codex.command.includes('hello'));assert(results.codex.diff.includes('+new'));assert(!results.codex.unsafe);assert(!results.codex.text.includes('tokenUsage'));
  assert.equal(results.claude.count,3,JSON.stringify(results.claude));assert(results.claude.text.includes('/work/project'));assert.equal(results.claude.text.split('已完成核验。').length-1,1);
  await page.setViewportSize({width:390,height:844});await page.screenshot({path:path.join(root,'preview-mobile.png'),fullPage:true});
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'mobile width');
  await page.locator('#close-activity').click();await page.locator('#toggle-nav').click();assert(await page.locator('#navigation').isVisible());await page.locator('#close-nav').click();
  assert.equal(errors.length,0,errors.join('\n'));
  console.log('PASS: native Codex/Claude stream, commands, diff, public reasoning, deduplication, safe text, master/task split, urgency controls, mobile layout');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
