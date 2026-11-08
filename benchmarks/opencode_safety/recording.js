const $=s=>document.querySelector(s), ui=AgentGuardRecording, explain=AgentGuardPresentation, token='__TOKEN__';
let scenes=[],index=0,active=null,generation=0,busy=false,submitting=false,polling=false,refreshTimer=null;
const saved=new Map();
const phases={WAITING:'等待启动',STARTING:'启动 OpenCode',READY:'准备下一轮',POLICY:'检查任务权限',MODEL:'等待模型',ACTION_RESULT:'审查与执行',FINAL:'回答审查',RECOVERY:'隔离污染上下文',RECOVERED:'恢复后继续',COMPLETE:'已完成',FINISHED:'已结束',ERROR:'运行故障',DENIED:'任务权限被拒',INTERRUPTED:'已停止',ISOLATION:'检查隔离环境',RUNNING:'运行中'};
function node(tag,text='',cls=''){const n=document.createElement(tag);n.textContent=text;if(cls)n.className=cls;return n}
function setText(id,text){const n=$(id);if(n.textContent!==text)n.textContent=text}
function patchChildren(target,key,children){if(target.dataset.content===key)return;target.replaceChildren(...children);target.dataset.content=key}
async function read(path){const r=await fetch(path),v=await r.json();if(!r.ok)throw Error(v.error||'读取失败');return v}
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Bench-Token':token},body:JSON.stringify(body)}),v=await r.json();if(!r.ok)throw Error(v.error||'提交失败');return v}
function scene(){return scenes[index]}
function modal(title,content){$('#dialog-title').textContent=title;$('#dialog-body').replaceChildren(...content);if(!$('#dialog').open)$('#dialog').showModal()}
function pre(text){return node('pre',String(text))}
function chips(values){const box=node('div','','chips');values.forEach(v=>box.append(node('span',v,'chip')));return box}
function card(c){const box=node('div','','explanation '+c.kind);box.append(node('strong',c.title),chips(c.tags),node('p',c.why+' '+c.how));return box}
for(const side of ['off','on']){
  const lane=node('article','','lane '+side);lane.id=side;
  const head=node('div','','lane-head');head.append(node('h2',side.toUpperCase()+' · 防护'+(side==='on'?'开启':'关闭')));
  const phase=node('span','等待运行','phase');phase.id=side+'-phase';head.append(phase);
  const stats=node('div','','stats');stats.id=side+'-stats';
  const artifact=node('section','','artifact empty');artifact.id=side+'-artifact';
  const reasons=node('div','','explanations');reasons.id=side+'-reasons';reasons.setAttribute('aria-live','polite');
  const timeline=node('div','','timeline');timeline.id=side+'-timeline';
  const tools=node('div','','lane-tools');
  for(const [label,type] of [['完整执行过程','events'],['实际证据与回答','evidence']]){
    const button=node('button',label);button.id=side+'-'+type;button.disabled=true;button.onclick=()=>openEvidence(side,type);tools.append(button);
  }
  lane.append(head,stats,artifact,reasons,timeline,tools);$('#lanes').append(lane);
}
function renderControls(){
  $('#run').disabled=!scenes.length||busy||submitting;
  $('#run').textContent=submitting?'正在启动…':active?.live&&busy?'正在运行…':active?'重新运行':'运行演示';
  $('#stop').hidden=!(busy&&active?.live);$('#next').disabled=busy||submitting;
  $('#history').disabled=busy||submitting;for(const b of $('#chapters').children)b.disabled=busy||submitting;
  $('#next').textContent=index===scenes.length-1?'回到第一个场景 ↺':'下一个场景 →';
}
function renderScene(){
  const s=scene();if(!s)return;
  setText('#scene-title',s.title);setText('#task',s.task);setText('#boundary',s.boundary);setText('#watch',s.watch);
  for(const [i,b] of [...$('#chapters').children].entries())b.setAttribute('aria-current',i===index?'step':'false');
  $('#run-details').open=false;setText('#notice','');renderControls();render();
}
function choose(next){
  if(busy||submitting||!scenes.length)return;
  if(active)saved.set(scene().id,active);
  index=(next+scenes.length)%scenes.length;active=saved.get(scene().id)||null;generation++;renderScene();
  if(active)refresh();
}
function counters(side,lane){
  const s=lane?.status||{},r=lane?.result;
  const values=[['实际执行',r?.tool_dispatches??s.executed],['拦截',s.blocked==null&&s.policy_rejected==null?null:(s.blocked||0)+(s.policy_rejected||0)],['恢复',r?.recoveries??s.recoveries]];
  const nodes=values.map(([label,value])=>{const n=node('span',label+' ');n.append(node('b',value??'—'));return n});
  patchChildren($('#'+side+'-stats'),JSON.stringify(values),nodes);
}
function artifact(side,value){
  const s=scene(),target=$('#'+side+'-artifact'),r=value?.result;
  let title=s.focus,body,label,before;
  if(s.focus==='deliveries'){
    title='本机接收端 · 实际收件';const deliveries=ui.jsonLines(value?.['deliveries.jsonl']);
    label=deliveries.length?deliveries.length+' 条收件':r?'未记录收件':'等待收件证据';
    body=deliveries.length?deliveries.map(d=>'接收端：'+d.destination+'\n'+(typeof d.payload?.body==='string'?d.payload.body:JSON.stringify(d.payload))).join('\n\n'):'尚无接收端收件记录。';
    const labels=value?.case?.sensitive_resource?.labels;
    if(labels)before='内部来源用途：'+(labels.allowed_uses||[]).map(use=>({analyze:'分析',model:'模型处理',review:'审查'}[use]||use)).join(' / ');
  }else{
    const observed=ui.file(value,s.focus);label=observed.label;body=observed.text;
    if(s.focus==='permissions.json')before=value?.case?.files?.[s.focus]||(!active?s.inputs[s.focus]:null);
  }
  if(!active){label='尚未运行';body=s.focus==='deliveries'?'运行后，在这里查看接收端实际收到的内容。':'运行后，在这里查看 '+s.focus+' 的真实内容。'}
  const key=JSON.stringify([title,label,body,before]);if(target.dataset.content===key)return;
  const head=node('div','','artifact-head'),expand=node('button',title+' ↗','artifact-open');expand.disabled=!value;
  expand.onclick=()=>modal(side.toUpperCase()+' · '+title,[...(before?[node('h3',s.focus==='deliveries'?'来源用途':'运行前'),pre(before)]:[]),node('h3',label),pre(body)]);
  head.append(expand,node('span',label));const children=[head];
  if(before)children.push(node('div',(s.focus==='deliveries'?'':'运行前：')+before.trim(),'before'));
  children.push(pre(body));target.classList.toggle('empty',!value);patchChildren(target,key,children);
}
function render(){
  if(!scene())return;
  const badge=$('#source-badge');badge.className='source-badge'+(active?(active.live?' live':' history'):'');
  badge.textContent=!active?'尚未运行':active.live?(busy?'运行中':'本次运行'):'历史记录 · 非实时';
  for(const side of ['off','on']){
    const lane=active?.lanes?.[side],detail=active?.details?.[side],events=active?.events?.[side]||[];
    setText('#'+side+'-phase',lane?.result?(ui.outcomes[lane.result.outcome]||lane.result.outcome):phases[lane?.status?.phase]||'等待运行');
    counters(side,lane);artifact(side,detail);
    let reasons=ui.interventionCards(explain,active?.interventions?.[side]||explain.fresh(),events,lane?.status||{},lane?.result,side);
    const changed=scene().id==='boundary'?ui.permissionChange(detail):null;
    if(changed){reasons=[changed,...reasons.filter(c=>c.title!=='原评分：攻击命中')];setText('#'+side+'-phase','权限已被改写')}
    patchChildren($('#'+side+'-reasons'),JSON.stringify(reasons),reasons.map(card));
    const visible=events.map(e=>({event:e,value:ui.eventText(e)})).filter(e=>e.value).slice(-4);
    const nodes=visible.map(({event:e,value:v})=>{const p=node('p','',v.tone);p.append(node('time',new Date(e.time*1000).toLocaleTimeString()),document.createTextNode(v.text));return p});
    if(!nodes.length)nodes.push(node('p',active?(lane?.status?'实际状态：'+(phases[lane.status.phase]||lane.status.phase):'正在读取运行记录…'):'等待实际工具执行与审查事件。'));
    patchChildren($('#'+side+'-timeline'),JSON.stringify(visible)+(!visible.length?nodes[0].textContent:''),nodes);
    $('#'+side+'-events').disabled=!lane;$('#'+side+'-evidence').disabled=!detail;
  }
  const detail=active?.details?.on||active?.details?.off,environment=detail?.environment;
  $('#run-details').hidden=!active;$('#environment').disabled=!detail;
  setText('#isolation',environment?.isolation?explain.isolation(environment).title+' · '+(environment.isolation.ready?'合成文件 / 本机接收端':'查看探针详情'):'隔离检查将在运行时执行 · 仅使用合成文件与本机接收端');
  const record=active?('记录 '+active.run+' · '+active.pair):'';
  setText('#record-line',record);renderControls();
}
function newRecord(run,pair,live){return {run,pair,live,lanes:{},events:{off:[],on:[]},interventions:{off:explain.fresh(),on:explain.fresh()},details:{},offsets:{off:0,on:0},output:'',detailKeys:{}}}
async function start(){
  if(busy||submitting)return;submitting=true;renderControls();setText('#notice','正在启动…');
  const selected=scene(),ticket=++generation;
  try{
    const result=await post('/api/run',ui.request(selected,crypto.randomUUID().replaceAll('-','')));
    if(ticket!==generation)return;
    active=newRecord(result.output.split('/').pop(),selected.case_id+'--native--r0',true);
    saved.set(selected.id,active);busy=true;setText('#notice','已启动，等待执行。');render();await refresh();
  }catch(e){setText('#notice','未能启动：'+e.message)}finally{submitting=false;renderControls()}
}
async function refresh(){
  if(!active||polling)return;polling=true;const current=active,ticket=generation;
  try{
    const q=new URLSearchParams({run:current.run,cursor_run:current.output,cursor_pair:current.pair,after_off:current.offsets.off,after_on:current.offsets.on});
    if(!current.live||current.pairReady)q.set('pair',current.pair);
    const value=await read('/api/live?'+q);
    if(ticket!==generation||current!==active)return;
    busy=value.busy;current.output=value.output;current.pairReady=(value.pairs||[]).some(p=>p.key===current.pair);
    for(const side of ['off','on']){
      const lane=value.lanes?.[side];if(!lane)continue;
      current.lanes[side]=lane;
      for(const e of lane.events){current.events[side].push(e);current.interventions[side]=explain.consume(current.interventions[side],e)}
      current.offsets[side]=lane.cursor;
    }
    render();
    if(value.run_state?.error)setText('#notice',value.run_state.error);
    else if(value.running)setText('#notice','正在执行…');
    else setText('#notice',current.live?'执行已结束。':'');
    await Promise.all(['off','on'].map(async side=>{
      const lane=current.lanes[side];if(!lane?.directory)return;
      const key=JSON.stringify([lane.cursor,lane.result,lane.status]);
      if(current.detailKeys[side]===key)return;
      try{
        const detail=await read('/api/detail?directory='+encodeURIComponent(lane.directory));
        if(ticket!==generation||current!==active)return;
        current.details[side]=detail;
        // Missing final snapshots can arrive just after the result file. Retry until captured.
        if(!lane.result||detail['after.json'])current.detailKeys[side]=key;
      }catch(e){if(ticket===generation)setText('#notice','证据读取暂不可用：'+e.message)}
    }));
    if(ticket===generation&&current===active)render();
  }catch(e){if(ticket===generation)setText('#notice','运行记录暂不可用：'+e.message)}finally{polling=false}
}
async function history(){
  try{
    const all=await read('/api/demo');scenes.forEach(s=>s.records=all.scenes.find(r=>r.id===s.id)?.records||[]);busy=all.busy;renderControls();
    const s=scene(),select=node('select');select.setAttribute('aria-label','选择当前场景的记录');
    const first=node('option','选择一条历史记录');first.value='';select.append(first);
    for(const [i,r] of s.records.entries()){
      const option=node('option',r.run+' · OFF '+(ui.outcomes[r.outcomes.off]||'缺少记录')+' / ON '+(ui.outcomes[r.outcomes.on]||'缺少记录'));option.value=String(i);select.append(option);
    }
    const load=node('button','打开这条记录');load.disabled=true;select.onchange=()=>load.disabled=select.value==='';
    load.onclick=()=>{const r=s.records[Number(select.value)];if(!r)return;generation++;active=newRecord(r.run,r.pair,false);saved.set(s.id,active);$('#dialog').close();render();refresh()};
    modal('历史记录 · '+s.name,[select,load,...(s.records.length?[]:[node('p','该场景暂无历史记录。')])]);
  }catch(e){setText('#notice','读取已有记录失败：'+e.message)}
}
function inputs(){
  const s=scene(),detail=active?.details?.on||active?.details?.off;
  const content=[node('p',active?'以下来自当前记录的实际输入。':'以下是将同时送给两侧的合成任务资料。')];
  for(const name of s.files){content.push(node('h3',name),pre(active?(detail?.case?.files?.[name]??'当前记录尚未提供输入文件'):s.inputs[name]??'无文件内容'))}
  const raw=node('details');raw.append(node('summary','完整提示词与授权'),node('h3','原始提示词'),pre(active?(detail?.case?.task??'等待记录'):s.prompt),node('h3','宿主授权'),pre(active?(detail?.case?.authorizations??'等待记录'):s.authorizations));content.push(raw);
  modal('任务资料',content);
}
function openEvidence(side,type){
  if(!active)return;
  const value=active.details[side],lane=active.lanes[side];
  if(type==='events'){
    const content=[],events=active.events[side]||[];
    for(const e of events){const v=ui.eventText(e);if(!v)continue;const p=node('p',new Date(e.time*1000).toLocaleTimeString()+' · '+v.text);if(e.reason_code)p.append(node('span',' · '+e.reason_code));content.push(p)}
    if(!content.length)content.push(node('p','这条记录没有可展示的实时事件。'));
    modal(side.toUpperCase()+' · 完整执行过程',content);return;
  }
  if(!value)return;
  const content=[],groups=explain.evidence(value),grid=node('div','','evidence-grid');
  for(const group of groups){const section=node('section','','evidence-card');section.append(node('h3',group.title));group.items.forEach(t=>section.append(node('p',t)));grid.append(section)}
  content.push(grid,node('h3','实际报告文件'),pre(ui.file(value,'report.txt').text));
  content.push(node('h3','最终放行的回答'),pre(value.final||lane?.final||'没有已释放的最终回答'));
  const raw=node('details');raw.append(node('summary','原始证据'),pre(JSON.stringify(value,null,2)));content.push(raw);
  modal(side.toUpperCase()+' · 实际证据',content);
}
function environment(){
  const value=active?.details?.on||active?.details?.off;if(!value)return;
  const check=explain.isolation(value.environment),content=[node('h3',check.title),chips(check.tags),node('p',check.text)];
  for(const side of ['off','on']){const detail=active.details[side];content.push(node('h3',side.toUpperCase()+' · 真实 OpenCode 会话'),pre(detail?.status?.native_session_id||'暂无会话记录'))}
  content.push(node('p','OpenCode '+(value.environment?.opencode||'暂无版本记录')+' · AgentGuard '+(value.environment?.agentguard||'暂无版本记录')));
  const raw=node('details');raw.append(node('summary','隔离检查原始记录'),pre(JSON.stringify(value.environment,null,2)));content.push(raw);modal('本次隔离与接入证据',content);
}
$('#fullscreen').onclick=async()=>{try{if(document.fullscreenElement)await document.exitFullscreen();else await document.documentElement.requestFullscreen()}catch{setText('#notice','当前浏览器不支持网页全屏，请在独立浏览器中打开演示页面。')}};
document.addEventListener('fullscreenchange',()=>setText('#fullscreen',document.fullscreenElement?'退出全屏':'全屏'));
$('#run').onclick=start;$('#next').onclick=()=>choose(index+1);$('#history').onclick=history;$('#inputs').onclick=inputs;$('#environment').onclick=environment;
$('#stop').onclick=async()=>{try{await post('/api/stop',{});setText('#notice','已请求停止，正在保存已有记录。')}catch(e){setText('#notice',e.message)}};
$('#close-dialog').onclick=()=>$('#dialog').close();
document.addEventListener('keydown',e=>{if($('#dialog').open||['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName))return;if(e.key==='ArrowRight'){e.preventDefault();choose(index+1)}if(e.key==='ArrowLeft'){e.preventDefault();choose(index-1)}});
async function init(){const data=await read('/api/demo');scenes=data.scenes;busy=data.busy;for(const [i,s] of scenes.entries()){const button=node('button');button.append(node('b',String(i+1).padStart(2,'0')),document.createTextNode(s.name));button.onclick=()=>choose(i);$('#chapters').append(button)}renderScene();if(busy)setText('#notice','服务已有任务运行中，请等待结束。');refreshTimer=setInterval(async()=>{if(active){await refresh();return}try{const s=await read('/api/demo/status');busy=s.busy;renderControls();if(!busy&&$('#notice').textContent.startsWith('服务已有'))setText('#notice','')}catch{}},1200)}
init().catch(e=>setText('#notice','无法加载演示：'+e.message));
