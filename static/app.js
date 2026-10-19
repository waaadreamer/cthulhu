'use strict';
const $=id=>document.getElementById(id);
let token=sessionStorage.getItem('cthulhu-token')||'',chatCursor=0,activityCursor=0,selected=null,following=true;
let state=null,lastState='',polling=false,activityGeneration=0,errorTimer;
let chatView=new Transcript($('chat'),'master'),activityView=null;
const statusNames={running:'进行中',waiting:'等待',paused:'已暂停',completed:'已完成',cancelled:'已取消',idle:'就绪'};
const stamp=t=>new Date(t*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false});
function node(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function fail(e){clearTimeout(errorTimer);$('error').textContent=e.message||String(e);$('error').hidden=false;errorTimer=setTimeout(()=>$('error').hidden=true,8000);}
const apiBase=new URL('./',location.href);
async function api(path,body){const r=await fetch(new URL(path.replace(/^\//,''),apiBase),{method:body===undefined?'GET':'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:body===undefined?undefined:JSON.stringify(body)});const value=await r.json();if(!r.ok||value.error)throw Error(value.error||r.statusText);return value;}
function button(text,fn,cls){const b=node('button',text,cls);b.type='button';b.onclick=()=>Promise.resolve().then(fn).catch(fail);return b;}
$('login').onclick=()=>{const value=prompt('粘贴 web-token 文件内容（仅保存在本标签页）',token);if(value!==null){token=value.trim();sessionStorage.setItem('cthulhu-token',token);lastState='';chatCursor=0;chatView=new Transcript($('chat'),'master');$('chat').replaceChildren();poll();}};
function renderState(s){
 state=s;const a=s.agents.find(x=>x.id==='master');if(!a)return;
 $('status').dataset.connected='true';$('status').textContent='中心已连接';
 $('model-label').textContent=[a.backend,a.model,a.reasoning_effort].filter(Boolean).join(' · ');
 $('master-badge').textContent=statusNames[a.paused?'paused':a.status]||a.status;
 $('pause-master').hidden=a.paused;$('resume-master').hidden=!a.paused;
 $('task-count').textContent=s.tasks.filter(t=>!['completed','cancelled'].includes(t.state)).length;
 const nodes=$('nodes');nodes.replaceChildren();$('node-count').textContent=(s.nodes||[]).filter(n=>n.online).length;
 for(const n of s.nodes||[]){const row=node('div',undefined,'node-entry '+(n.online?'online':'offline'));row.append(node('span','','live-dot'),node('span',n.id),node('span',n.online?'在线':'离线','node-state'));nodes.append(row);}
 if(!s.nodes?.length)nodes.append(node('p','尚未连接星系节点','memory-empty'));
 const tasks=$('tasks');tasks.replaceChildren();
 if(!s.tasks.length)tasks.append(node('p','暂无任务。\n和 Master 聊聊你的目标，或新建一个任务。','empty-tasks'));
 // 进行中的任务排在前面，原始状态与 session 不因显示顺序而改变。
 const ordered=[...s.tasks].sort((a,b)=>Number(['completed','cancelled'].includes(a.state))-Number(['completed','cancelled'].includes(b.state)));
 for(const t of ordered){
  const card=node('article',undefined,'task'+(selected===t.agent?' selected':''));card.dataset.state=t.state;card.dataset.agent=t.agent;card.title=t.id;
  const top=node('div',undefined,'task-top');top.append(node('span','','task-status-dot'),node('h3',t.title));
  const info=node('div',undefined,'task-details');info.append(node('span',statusNames[t.state]||t.state),node('span','·'),node('span',t.node||'local'));
  const actions=node('div',undefined,'task-controls');actions.append(button('CoT ↗',()=>openActivity(t.agent,t.title),'view-task'));
  if(!['completed','cancelled'].includes(t.state))for(const action of t.state==='paused'?['resume','cancel']:['pause','cancel'])actions.append(button({pause:'暂停',resume:'恢复',cancel:'取消'}[action],async()=>{await api('/api/control',{task_id:t.id,action,reason:'主人通过页面操作'});lastState='';await poll();}));
  card.append(top,info,node('div','截止 '+stamp(t.deadline),'task-deadline'),actions);tasks.append(card);
 }
 const questions=$('questions');questions.replaceChildren();
 for(const q of s.questions.filter(q=>!['answered','closed'].includes(q.state))){
  const task=s.tasks.find(t=>t.id===q.task),box=node('div',undefined,'question');
  box.append(node('div',q.state==='owner'?'需要你决定':'执行者有一个问题','question-title'),node('div',q.text),node('small',(task?.title||q.agent)+(q.state==='owner'?' · 等待你回答，截止 '+stamp(q.deadline):' · Master 正在处理')));
  box.append(button('我来回答',()=>answerQuestion(q)));
  if(a.backend==='demo'&&q.state==='master')box.append(button('模拟：请我回答并计时',async()=>{await api('/api/demo/escalate',{question_id:q.id,version:q.version,recommendation:q.recommendation});lastState='';}));questions.append(box);
 }
 const mem=$('memories');mem.replaceChildren();if(!s.memories.some(m=>!m.deleted))mem.append(node('p','在协作中，逐渐了解你。','memory-empty'));
 for(const m of s.memories.filter(m=>!m.deleted)){const row=node('div',(m.inferred?'推测 · ':'')+m.text,'memory');row.title=m.source;mem.append(row);}
}
async function answerQuestion(q){const answer=prompt('回答问题：'+q.text);if(answer?.trim()){await api('/api/answer',{question_id:q.id,version:q.version,answer});lastState='';}}
function appendChat(events){
 const el=$('chat'),bottom=el.scrollHeight-el.scrollTop-el.clientHeight<90;chatView.append(events);
 for(const e of events){chatCursor=e.seq;if(e.kind==='answer'){
  const row=el.querySelector(`[data-seq="${e.seq}"] .row-body`);if(row&&!row.querySelector('button'))row.append(button('补充 / 修正回答',async()=>{const current=(await api('/api/state')).questions.find(q=>q.id===e.data.id);if(current)await answerQuestion(current);},'quiet-button'));
 }}
 if(bottom)el.scrollTop=el.scrollHeight;else if(events.length)$('chat-latest').hidden=false;
}
function appendActivity(events){activityView.append(events);if(events.length)activityCursor=events.at(-1).seq;if(following)$('timeline').scrollTop=$('timeline').scrollHeight;updateFollow();}
function updateFollow(){$('activity-count').textContent=following?'实时过程 · 跟随中':'阅读历史 · 已暂停跟随';$('follow').dataset.following=String(following);}
function openActivity(agent,title){
 if(agent==='master'){$('chat').focus();return;} // Master 的原生过程始终在主会话内，侧栏只属于 task。
 selected=agent;activityGeneration++;activityCursor=0;following=true;$('timeline').replaceChildren(node('p','正在读取会话…','transcript-empty'));activityView=new Transcript($('timeline'),agent);
 $('activity-title').textContent=title;$('activity-agent').textContent=agent;$('activity').hidden=false;$('navigation').classList.remove('open');
 document.querySelectorAll('.task').forEach(t=>t.classList.toggle('selected',t.dataset.agent===agent));updateFollow();poll();
}
$('close-activity').onclick=()=>{selected=null;activityGeneration++;$('activity').hidden=true;document.querySelectorAll('.task.selected').forEach(t=>t.classList.remove('selected'));};
$('follow').onclick=()=>{following=true;$('timeline').scrollTop=$('timeline').scrollHeight;updateFollow();};
$('timeline').onscroll=()=>{const e=$('timeline');following=e.scrollHeight-e.scrollTop-e.clientHeight<60;updateFollow();};
$('chat').onscroll=()=>{$('chat-latest').hidden=$('chat').scrollHeight-$('chat').scrollTop-$('chat').clientHeight<90;};
$('chat-latest').onclick=()=>{$('chat').scrollTop=$('chat').scrollHeight;$('chat-latest').hidden=true;};
$('toggle-nav').onclick=()=>$('navigation').classList.toggle('open');
$('close-nav').onclick=()=>$('navigation').classList.remove('open');
// 加急是明确的 P10 打断；通过可访问的分段按钮选择，不让装饰控件影响真实优先级。
function setMode(prefix,urgent){
 $(prefix+'normal').setAttribute('aria-pressed',String(!urgent));$(prefix+'urgent').setAttribute('aria-pressed',String(urgent));
 $(prefix?'agent-form':'chat-form').dataset.urgent=String(urgent);
 if(!prefix){$('send-hint').textContent=urgent?'立即打断当前回合':'保留当前回合';$('send-hint').classList.toggle('urgent',urgent);}
}
for(const prefix of ['','agent-']){for(const mode of ['normal','urgent'])$(prefix+mode).onclick=()=>setMode(prefix,mode==='urgent');}
function composingInput(input,form){input.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();form.requestSubmit();}};input.oninput=()=>{input.style.height='auto';input.style.height=Math.min(input.scrollHeight,180)+'px';};}
composingInput($('input'),$('chat-form'));composingInput($('agent-input'),$('agent-form'));
async function send(form,input,prefix,to){
 const text=input.value.trim();if(!text)return;const submit=form.querySelector('[type=submit],.send-button');submit.disabled=true;
 try{await api('/api/chat',{...(to?{to}:{}),text,priority:$(prefix+'urgent').getAttribute('aria-pressed')==='true'?10:9});input.value='';input.style.height='auto';setMode(prefix,false);await poll();}catch(e){fail(e);}finally{submit.disabled=false;input.focus();}
}
$('chat-form').onsubmit=e=>{e.preventDefault();send($('chat-form'),$('input'),'');};
$('agent-form').onsubmit=e=>{e.preventDefault();if(selected)send($('agent-form'),$('agent-input'),'agent-',selected);};
for(const action of ['pause','resume'])$(action+'-master').onclick=async()=>{try{await api('/api/master',{action});lastState='';await poll();}catch(e){fail(e);}};
$('new-task').onclick=()=>{const form=$('task-form'),select=form.elements.node;select.replaceChildren(new Option('local · 中心机','local'));for(const n of state?.nodes||[])select.add(new Option(n.id+' · '+(n.online?'在线':'离线'),n.id));select.value=state?.nodes?.find(n=>n.online)?.id||'local';form.elements.backend.value=state?.agents.find(a=>a.id==='master')?.backend||'demo';const d=new Date(Date.now()+3600000);d.setMinutes(d.getMinutes()-d.getTimezoneOffset());form.elements.deadline.value=d.toISOString().slice(0,16);$('task-dialog').showModal();};
$('cancel-task-dialog').onclick=()=>$('task-dialog').close();
$('task-form').onsubmit=async e=>{e.preventDefault();const body=Object.fromEntries(new FormData(e.target));body.deadline=new Date(body.deadline).toISOString();if(!body.model)delete body.model;try{await api('/api/task',body);$('task-dialog').close();lastState='';await poll();}catch(e){fail(e);}};
async function readStream(query,cursor,append,valid=()=>true){
 // 分页立即追赶原生流，避免数千个文字 delta 以每秒一页慢慢回放。切换任务后拒绝迟到响应。
 for(let page=0;page<8;page++){
  const {events}=await api('/api/events?'+query+'&limit=500&after='+cursor);if(!valid())return;
  append(events);if(events.length<500)return;cursor=events.at(-1).seq;
 }
}
async function poll(){
 if(polling||!token)return;polling=true;
 try{
  const s=await api('/api/state'),signature=JSON.stringify({...s,now:0,seq:0});if(signature!==lastState){renderState(s);lastState=signature;}
  const generation=activityGeneration,agent=selected;
  await Promise.all([readStream('view=conversation',chatCursor,appendChat),agent?readStream('agent='+encodeURIComponent(agent),activityCursor,appendActivity,()=>generation===activityGeneration):Promise.resolve()]);
 }catch(e){$('status').dataset.connected='false';$('status').textContent=token?'中心未连接':'需要访问凭证';if(e.message!=='请填写本地访问 token'&&!e.message.includes('Failed to fetch'))fail(e);}finally{polling=false;}
}
setInterval(poll,1000);if(!token)$('status').textContent='需要访问凭证';poll();
