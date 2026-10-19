'use strict';
/* 原生事件 → 可读会话。只呈现 CLI 已公开的文字，不生成、推测或补写思考内容。
 * 同一 item 的 start/delta/complete 更新同一行；主聊天与任务栏各自保存状态，不能串流。
 */
(()=>{
 const el=(tag,text,cls)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=String(text);if(cls)n.className=cls;return n;};
 const names={owner:'你',master:'Master',system:'系统'};
 const who=id=>names[id]||id||'系统';
 const clean=text=>String(text??'').replace(/\x1b\[[0-?]*[ -/]*[@-~]/g,'');
 const parse=value=>{if(typeof value!=='string')return value;try{return JSON.parse(value);}catch{return value;}};
 const status=s=>({inProgress:'执行中',running:'执行中',completed:'完成',failed:'失败',interrupted:'已中断',error:'失败'})[s]||s||'';
 function inline(parent,text){
  // 不使用 innerHTML；即使命令输出包含 HTML 或 javascript: 链接也只能显示为文本。
  const re=/(`[^`\n]+`|\*\*[^*\n]+\*\*|\[[^\]\n]+\]\([^\s)]+\))/g;let pos=0;
  for(const m of text.matchAll(re)){
   parent.append(document.createTextNode(text.slice(pos,m.index)));const s=m[0];
   if(s[0]==='`')parent.append(el('code',s.slice(1,-1)));
   else if(s.startsWith('**'))parent.append(el('strong',s.slice(2,-2)));
   else{const p=s.match(/^\[([^\]]+)\]\((.+)\)$/);if(/^https?:\/\//i.test(p[2])){const a=el('a',p[1]);a.href=p[2];a.target='_blank';a.rel='noopener noreferrer';parent.append(a);}else parent.append(el('span',p[1]+' ('+p[2]+')'));}
   pos=m.index+s.length;
  }parent.append(document.createTextNode(text.slice(pos)));
 }
 function markdown(parent,text){
  parent.replaceChildren();const lines=clean(text).split('\n');let code=null,list=null;
  for(const line of lines){
   if(/^\s*```/.test(line)){if(code){parent.append(code);code=null;}else{code=el('pre','','code-block');code.dataset.language=line.trim().slice(3);}list=null;continue;}
   if(code){code.textContent+=(code.textContent?'\n':'')+line;continue;}
   if(!line.trim()){list=null;continue;}
   const item=line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
   if(item){if(!list){list=el('ul');parent.append(list);}const li=el('li');inline(li,item[1]);list.append(li);continue;}
   list=null;const h=line.match(/^#{1,4}\s+(.+)/);const row=el(h?'h4':'p');inline(row,h?h[1]:line);parent.append(row);
  }if(code)parent.append(code);
 }
 function readable(value,depth=0){
  value=parse(value);if(value===null||value===undefined)return '';if(typeof value!=='object')return clean(value);
  if(depth>5)return '…';
  if(Array.isArray(value))return value.map(v=>readable(v,depth+1)).filter(Boolean).join('\n');
  if(value.type==='text'&&typeof value.text==='string')return readable(value.text,depth+1);
  if(value.content)return readable(value.content,depth+1);
  const labels={id:'标识',state:'状态',status:'状态',title:'任务',text:'内容',result:'结果',error:'错误',message:'消息',online:'在线',node:'节点',cwd:'目录',command:'命令',path:'文件',file_path:'文件',reason:'原因',evidence:'证据',deadline:'截止时间',backend:'后端',model:'模型',arguments:'参数',tasks:'任务',nodes:'节点',agents:'执行者',exit_code:'退出码',exitCode:'退出码'};
  return Object.entries(value).filter(([k,v])=>v!==null&&!['token','_meta','seen','ack','instance','pid_identity','review_cursor'].includes(k))
   .map(([k,v])=>`${labels[k]||k} · ${typeof v==='boolean'?(v?'是':'否'):readable(v,depth+1)}`).join('\n');
 }
 function output(parent,text,cls='terminal-output'){
  text=clean(text);parent.replaceChildren();if(!text)return;
  const lines=text.split('\n');const preview=lines.slice(0,9).join('\n').slice(0,1600);
  parent.append(el('pre',preview,cls));
  if(text.length>preview.length){const more=el('details',undefined,'more-output');more.append(el('summary',`展开完整输出 · ${lines.length} 行`),el('pre',text,cls));parent.append(more);}
 }
 class Transcript {
  constructor(container,agent){this.container=container;this.agent=agent;this.records=new Map();this.seen=new Set();this.messageIds=new Set();this.claudeId='';this.claudeTurn=0;this.finalText='';this.finalRow=null;this.toolRows=new Map();}
  key(e,id){const d=e.data||{};return `${e.actor}:${d.threadId||''}:${d.turnId||''}:${id}`;}
  row(key,e,kind,title){
   if(this.records.has(key))return this.records.get(key);
   this.container.querySelector('.welcome,.transcript-empty')?.remove();
   const row=el('article',undefined,'transcript-row '+kind);row.dataset.kind=kind;row.dataset.seq=e.seq;
   const mark=el('span',({message:'›',thinking:'·',tool:'⌘',command:'›_',file:'±',notice:'◇',error:'!','hook':'↳'})[kind]||'·','row-mark');
   const main=el('div',undefined,'row-main'),head=el('div',undefined,'row-head'),label=el('span',title,'row-label');
   const time=el('time',new Date(e.at*1000).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}));time.title=new Date(e.at*1000).toLocaleString();
   const state=el('span','','row-state');head.append(label,state,time);
   const body=el('div',undefined,'row-body');main.append(head,body);row.append(mark,main);this.container.append(row);
   const record={row,head,label,state,body,text:'',kind};this.records.set(key,record);return record;
  }
  prose(key,e,text,{title=who(e.actor),kind='message',delta=false,final=false}={}){
   if(!text&&!this.records.has(key))return;
   const r=this.row(key,e,kind,title);r.text=delta?r.text+text:text;r.label.textContent=title;
   r.row.classList.remove(r.kind);r.kind=kind;r.row.classList.add(kind);r.row.dataset.kind=kind;r.row.classList.toggle('owner',e.actor==='owner');
   markdown(r.body,r.text);if(final){this.finalText=r.text.trim();this.finalRow=r;}
   return r;
  }
  tool(key,e,{title,command,input,result,state,kind='tool',diff}){
   const r=this.row(key,e,kind,title);r.label.textContent=title;r.state.textContent=status(state);r.row.dataset.state=state||'';
   if(!r.output){r.body.replaceChildren();r.input=el('div',undefined,'tool-input');r.output=el('div');r.body.append(r.input,r.output);}
   if(command!==undefined){r.command=command;r.input.replaceChildren(el('pre',command,'command-line'));}
   else if(input!==undefined){r.input.replaceChildren();const text=readable(input);if(text)r.input.append(el('pre',text,'tool-arguments'));}
   if(result!==undefined)output(r.output,readable(result));
   if(diff!==undefined){r.output.replaceChildren();const pre=el('pre',undefined,'diff');for(const line of diff.split('\n'))pre.append(el('span',line+'\n',line.startsWith('+')?'added':line.startsWith('-')?'removed':''));r.output.append(pre);}
   return r;
  }
  codexItem(e,item,done){
   const key=this.key(e,item.id),type=item.type;
   if(type==='userMessage')return; // harness message 事件已携带准确来源，不能再次展示包装后的路由 JSON。
   if(type==='agentMessage'){
    // start 往往没有正文，仍记住 phase，后续 delta 才能按原生通道显示。
    if(!item.text&&!done)this.row(key,e,item.phase==='commentary'?'thinking':'message',item.phase==='commentary'?who(e.actor)+' · 过程':who(e.actor));
    return this.prose(key,e,item.text||'',{title:item.phase==='commentary'?who(e.actor)+' · 过程':who(e.actor),kind:item.phase==='commentary'?'thinking':'message',final:item.phase==='final_answer'});
   }
   if(type==='reasoning'){
    const text=readable(item.summary?.length?item.summary:item.content);
    // summary 流和 complete 是同一段公开摘要，不能各显示一份。
    const existing=this.records.has(key+':summary')?key+':summary':key;
    if(!text)return;
    return this.prose(existing,e,text,{title:'思考',kind:'thinking'});
   }
   if(type==='commandExecution')return this.tool(key,e,{title:'执行命令',kind:'command',command:item.command,input:undefined,result:item.aggregatedOutput??undefined,state:item.exitCode!=null&&item.exitCode!==0?'failed':item.status|| (done?'completed':'inProgress')});
   if(type==='mcpToolCall'||type==='dynamicToolCall'){
    const name=[item.server,item.tool].filter(Boolean).join(' / ');return this.tool(key,e,{title:name||'调用工具',input:item.arguments,result:item.error||item.result||undefined,state:item.error?'failed':item.status||(done?'completed':'inProgress')});
   }
   if(type==='fileChange'){
    const changes=item.changes||[];return this.tool(key,e,{title:'编辑文件 · '+changes.map(c=>c.path).join(', '),kind:'file',diff:changes.map(c=>(c.path+'\n'+(c.diff||''))).join('\n'),state:item.status||(done?'completed':'inProgress')});
   }
   if(type==='webSearch')return this.tool(key,e,{title:'搜索网页',input:item.query||item.action?.query||item.action, result:item.result,state:done?'completed':'inProgress'});
   if(type==='plan')return this.prose(key,e,item.text,{title:'计划'});
   if(type==='contextCompaction')return this.prose(key,e,'原生 CLI 正在整理会话上下文。',{title:'上下文整理',kind:'notice'});
   // 未适配的新动作也保留入口；协议细节默认收起，不能静默丢掉动作。
   const r=this.row(key,e,'tool','原生活动 · '+(type||'未知'));r.state.textContent=done?'完成':'进行中';
   r.body.replaceChildren();const details=el('details');details.append(el('summary','查看动作内容'),el('pre',readable(item),'terminal-output'));r.body.append(details);
  }
  claudeBlock(e,b,index,messageId){
   const key=`${e.actor}:claude:${messageId}:${index}`;
   if(b.type==='text')return this.prose(key,e,b.text||'',{title:who(e.actor),final:true});
   if(b.type==='thinking')return this.prose(key,e,b.thinking||'',{title:'思考',kind:'thinking'});
   if(b.type==='tool_use'){
    const command=b.name==='Bash'?b.input?.command:undefined;
    const r=this.tool(key,e,{title:b.name==='Bash'?'执行命令':b.name,kind:b.name==='Bash'?'command':'tool',command,input:command?undefined:b.input,state:'inProgress'});
    r.toolName=b.name;r.partial='';this.toolRows.set(b.id,key);return r;
   }
   if(b.type==='tool_result'){
    const old=this.toolRows.get(b.tool_use_id);if(old){const r=this.records.get(old);output(r.output,readable(b.content));r.state.textContent=b.is_error?'失败':'完成';r.row.dataset.state=b.is_error?'failed':'completed';}
    else this.tool(key,e,{title:'工具结果',result:b.content,state:b.is_error?'failed':'completed'});
   }
  }
  native(e){
   const d=e.data||{},method=e.kind.slice(7);
   if(method==='item/started'||method==='item/completed')return this.codexItem(e,d.item||{},method.endsWith('completed'));
   if(method.startsWith('item/reasoning/')){
    if(method.includes('summaryTextDelta'))return this.prose(this.key(e,d.itemId)+':summary',e,d.delta||'',{delta:true,title:'思考',kind:'thinking'});
    if(method.includes('textDelta'))return this.prose(this.key(e,d.itemId),e,d.delta||'',{delta:true,title:'思考',kind:'thinking'});
    return;
   }
   if(method.startsWith('item/')&&method.endsWith('/delta')){
    const key=this.key(e,d.itemId);
    if(method==='item/agentMessage/delta'){const r=this.records.get(key);return this.prose(key,e,d.delta||'',{delta:true,title:r?.label.textContent||(who(e.actor)+' · 过程'),kind:r?.kind||'thinking'});}
    if(method.includes('reasoning')){
     // summary 与原文是不同通道；有公开 summary 时只显示 summary，避免重复拼接。
     if(method.includes('summaryText'))return this.prose(key+':summary',e,d.delta||'',{delta:true,title:'思考',kind:'thinking'});
     if(method.includes('textDelta')||method.includes('text/'))return this.prose(key,e,d.delta||'',{delta:true,title:'思考',kind:'thinking'});
    }
   }
   if(method==='item/commandExecution/outputDelta'||method==='item/fileChange/outputDelta'){
    const key=this.key(e,d.itemId),r=this.records.get(key)||this.tool(key,e,{title:'执行输出',kind:'command',state:'inProgress'});
    r.text+=d.delta||'';output(r.output,r.text);return;
   }
   if(method==='turn/completed'){
    for(const item of d.turn?.items||[])this.codexItem(e,item,true);
    if(d.turn?.error)this.prose(this.key(e,'turn-error'),e,readable(d.turn.error),{title:'执行失败',kind:'error'});return;
   }
   if(method==='stream_event'){
    const ev=d.event||{};
    if(ev.type==='message_start'){this.claudeId=ev.message?.id||String(++this.claudeTurn);return;}
    if(ev.type==='content_block_start')return this.claudeBlock(e,ev.content_block||{},ev.index,this.claudeId);
    if(ev.type==='content_block_delta'){
     const key=`${e.actor}:claude:${this.claudeId}:${ev.index}`,delta=ev.delta||{};
     if(delta.type==='text_delta')return this.prose(key,e,delta.text||'',{delta:true,final:true});
     if(delta.type==='thinking_delta')return this.prose(key,e,delta.thinking||'',{delta:true,title:'思考',kind:'thinking'});
     if(delta.type==='input_json_delta'){
      const r=this.records.get(key);if(!r)return;r.partial+=delta.partial_json||'';const input=parse(r.partial);
      if(typeof input==='object')this.tool(key,e,{title:r.label.textContent,kind:r.kind,command:r.toolName==='Bash'?input.command:undefined,input:r.toolName==='Bash'?undefined:input,state:'inProgress'});
     }
    }return;
   }
   if(method==='assistant'||method==='user'){
    const m=d.message||{};if(method==='user'&&!Array.isArray(m.content))return;
    (Array.isArray(m.content)?m.content:[]).forEach((b,i)=>{if(method==='user'&&b.type!=='tool_result')return;this.claudeBlock(e,b,i,m.id||this.claudeId);});return;
   }
   if(method==='result'){
    if(d.is_error)return this.prose('event:'+e.seq,e,readable(d.result||d.errors),{title:'执行失败',kind:'error'});
    if(d.result&&d.result.trim()!==this.finalText)return this.prose('event:'+e.seq,e,d.result,{final:true});return;
   }
   if(method==='hook/started'||method==='hook/completed'){
    const run=d.run||{},key=this.key(e,'hook:'+run.id);
    if(method==='hook/started')return;
    if(run.status==='failed'||run.statusMessage||run.entries?.length){const r=this.row(key,e,'hook','Hook · '+(run.eventName||'执行'));r.state.textContent=status(run.status);output(r.body,run.statusMessage||readable(run.entries));}return;
   }
   if(method==='system'&&d.subtype?.startsWith('hook')){
    if(d.output||d.stderr||d.exit_code)this.tool('hook:'+d.hook_id,e,{title:'Hook · '+(d.hook_name||d.subtype),result:d.output||d.stderr,state:d.exit_code?'failed':'completed'});return;
   }
   if(method==='stderr'||method==='error')return this.prose('event:'+e.seq,e,d.text||readable(d.error||d),{title:'运行提示',kind:'error'});
   if(method==='demo')return this.prose('event:'+e.seq,e,d.text,{title:'模拟过程',kind:'thinking'});
   if(method==='turn/plan/updated')return this.prose('event:'+e.seq,e,readable(d.plan),{title:'计划'});
   // account、tokenUsage、thread/status、启动握手等是传输遥测，不是 CLI 会话正文。
  }
  append(events){
   for(const e of events){
    if(this.seen.has(e.seq))continue;this.seen.add(e.seq);
    if(e.kind.startsWith('native:')){this.native(e);continue;}
    const d=e.data||{};
    if(e.kind==='message'){
     if(d.id&&this.messageIds.has(d.id))continue;if(d.id)this.messageIds.add(d.id);
     const route=who(e.actor)+(e.target&&e.target!==this.agent?' → '+who(e.target):'');
     let text=e.text;
     if(text.startsWith('你的唯一任务：\n')){
      const task=parse(text.slice('你的唯一任务：\n'.length));
      if(task&&typeof task==='object')text=`**${task.title}**\n\n目标：${task.description}\n验收：${task.acceptance}\n工作目录：${task.cwd||'见任务配置'}\n截止时间：${new Date(task.deadline*1000).toLocaleString()}`;
     }
     this.prose('event:'+e.seq,e,text,{title:route,kind:e.actor==='system'?'notice':'message'});continue;
    }
    if(e.kind==='instruction'&&d.message_id&&this.messageIds.has(d.message_id))continue;
    if(['harness_tool','dispatch','remote_result'].includes(e.kind)){
     if(e.kind==='remote_result'&&d.error)this.prose('event:'+e.seq,e,e.text||d.error,{title:'执行失败',kind:'error'});continue;
    }
    if(['chat','reply'].includes(e.kind)&&e.actor===this.agent&&e.text.trim()===this.finalText)continue;
    const labels={review:'任务进展',question:'执行者提问',ask_owner:'需要你决定',answer:'问题答复',control:'任务调整',owner_control:'你的操作',master_control:'Master 控制',task_created:'新任务',completed:'任务完成',deadline:'期限调整',overdue:'期限提醒',question_timeout:'等待超时',node_attention:'节点提醒',recovery:'恢复检查',runtime_error:'执行失败',instruction:'Master → 执行者',interrupted:'回合中断'};
    const kind=['runtime_error','node_attention'].includes(e.kind)?'error':e.actor==='system'?'notice':'message';
    const r=this.prose('event:'+e.seq,e,e.text,{title:labels[e.kind]||who(e.actor),kind});
    if(r&&e.kind==='answer')r.row.dataset.questionId=d.id||'';
   }
  }
 }
 window.Transcript=Transcript;
})();
