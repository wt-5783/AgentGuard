const token='__TOKEN__', $=s=>document.querySelector(s), selected=new Set();
let cases=[],lastRows=[],mode='manual',liveKey='',offsets={off:0,on:0},liveOutput='',livePair='',liveBusy=false,refreshing=false;
const names={attack_success:'攻击成功',guard_block:'防护拦截',model_refusal:'模型自行拒绝',no_harm_observed:'未观察到危害',error:'故障 / 未知',utility_success:'正常完成',false_block:'良性任务被拦截',task_failure:'任务未完成',manual_complete:'自定义任务结束'};
const phases={WAITING:'等待另一侧 / 排队',STARTING:'启动 OpenCode',READY:'准备下一轮',POLICY:'编译防护策略',MODEL:'等待模型响应',ACTION_RESULT:'工具审查与执行',FINAL:'最终回答审查',RECOVERY:'阻断后恢复上下文',RECOVERED:'已恢复，继续任务',COMPLETE:'已完成',FINISHED:'已结束',ERROR:'运行故障',DENIED:'策略拒绝任务',INTERRUPTED:'已停止',ISOLATION:'检查隔离环境',RUNNING:'运行中'};
const pct=x=>x==null?'—':(100*x).toFixed(1)+'%';
function el(tag,text,cls){const e=document.createElement(tag);e.textContent=text;if(cls)e.className=cls;return e}
for(const side of ['off','on']){
  const lane=el('article','','lane '+side);lane.id=side;
  const head=el('div','','lane-head'),title=el('h2',side.toUpperCase()+' · 防护'+(side==='on'?'开启':'关闭'));
  const phase=el('span','等待运行','phase');phase.id=side+'-phase';title.append(phase);head.append(title);
  for(const [id,cls] of [['counts','counters'],['session','session'],['activity','activity']]){const node=el('div','',cls);node.id=side+'-'+id;head.append(node)}
  const feed=el('div','','feed');feed.id=side+'-feed';feed.setAttribute('role','log');feed.setAttribute('aria-label',side.toUpperCase()+' 实时事件');
  const final=el('details','','final-box');final.append(el('summary','原生 OpenCode 最终回答'));const body=el('pre','尚无已释放回答');body.id=side+'-final';final.append(body);
  lane.append(head,feed,final);$('#lanes').append(lane);
}
function tab(next){mode=next;for(const v of ['manual','benchmark']){$('#tab-'+v).setAttribute('aria-pressed',String(v===mode));$('#'+v+'-controls').hidden=v!==mode}$('#batch-actions').hidden=mode!=='benchmark';$('#run').textContent=mode==='manual'?'发送到两个 OpenCode':'运行所选双组对照'}
$('#tab-manual').onclick=()=>tab('manual');$('#tab-benchmark').onclick=()=>tab('benchmark');
function renderCases(){const box=$('#cases');box.replaceChildren();for(const r of cases.filter(r=>$('#group').value==='all'||r.group===$('#group').value)){
  const label=el('label','','case'),c=document.createElement('input');c.type='checkbox';c.checked=selected.has(r.id);c.onchange=()=>{c.checked?selected.add(r.id):selected.delete(r.id);$('#selection').textContent='已选择 '+selected.size+' 个样例'};
  const text=el('span',r.title);text.append(el('small',r.id+' · '+r.group));label.append(c,text);box.append(label)}}
$('#group').onchange=renderCases;
async function template(fill){const r=await(await fetch('/api/template?case='+encodeURIComponent($('#fixture').value))).json();$('#fixture-files').textContent='文件：'+r.files.join('、');if(fill){$('#prompt').value=r.prompt;$('#authority').value=r.authorizations}}
$('#fixture').onchange=()=>template(false);$('#load-template').onclick=()=>template(true);
async function post(path,body){const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-Bench-Token':token},body:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data}
async function start(profile){
  if(profile==='selected'&&!selected.size){$('#notice').textContent='请先选择至少一个样例。';return}
  if(profile==='manual'&&!$('#prompt').value.trim()){$('#notice').textContent='请输入提示词。';return}
  const body={submission_id:crypto.randomUUID().replaceAll('-',''),profile,cases:profile==='selected'?[...selected]:[],attack:profile==='manual'?'native':$('#attack').value,thinking:$('#thinking').value,jobs:Number($('#jobs').value)};
  if(profile==='manual')body.manual={prompt:$('#prompt').value,authorizations:$('#authority').value,fixture:$('#fixture').value};
  $('#run').disabled=true;$('#notice').textContent='正在启动相同提示词的双组 OpenCode…';
  try{const r=await post('/api/run',body);$('#history').value='';$('#pair').value='';resetLive();if(window.innerWidth<1050){$('#controls').open=false;$('#lanes').scrollIntoView({block:'start'})}$('#notice').textContent='已提交，正在检查隔离并启动两个会话。';await refreshLive();refreshReport();loadHistory()}
  catch(e){$('#notice').textContent=e.message;$('#run').disabled=false}
}
$('#run').onclick=()=>start(mode==='manual'?'manual':'selected');$('#smoke').onclick=()=>start('smoke');$('#full').onclick=()=>start('full');
$('#stop').onclick=async()=>{try{await post('/api/stop',{});$('#notice').textContent='已请求停止，正在结束两侧隔离进程。';$('#stop').disabled=true}catch(e){$('#notice').textContent=e.message}};
function resetLive(){liveKey='';liveOutput='';livePair='';offsets={off:0,on:0};for(const side of ['off','on']){$('#'+side+'-feed').replaceChildren();$('#'+side+'-final').textContent='尚无已释放回答';$('#'+side+'-activity').textContent=''}}
function appendEvent(side,event){
  const feed=$('#'+side+'-feed'),node=el('div','','event');node.dataset.kind=event.kind;node.append(el('time',new Date(event.time*1000).toLocaleTimeString()));let text='',extra='',activity='';
  if(event.kind==='status'){text='状态 · '+(side==='off'&&event.status.phase==='FINAL'?'生成最终回答':phases[event.status.phase]||event.status.phase);if(event.status.reason_code)text+=' · '+event.status.reason_code;activity=text;if(['DENIED','ERROR'].includes(event.status.phase))node.classList.add('block')}
  else if(event.kind==='proposal'){text='OpenCode 提议 · '+event.tool;extra=side==='off'?event.args:'';activity=event.status==='PENDING_REVIEW'?'工具调用待防护审查':'工具调用待执行'}
  else if(event.kind==='review_started'){text=event.phase+' 审查开始';activity=text}
  else if(event.kind==='review_finished'){text=event.phase+' 审查 · '+event.decision+' · '+event.reason_code;activity=text;node.classList.add(['BLOCK','FAILED'].includes(event.decision)?'block':'allow')}
  else if(event.kind==='execution_started'){text='实际开始执行 · '+event.tool;extra=side==='off'?event.args:'';activity=text}
  else if(event.kind==='execution_finished'){text='执行'+(event.ok?'完成':'失败')+' · '+event.tool+' · '+event.latency_ms+'ms';if(event.error)text+=' · '+event.error;activity=text;node.classList.add(event.ok?'allow':'block')}
  else if(event.kind==='receipt'){text='防护回执 · '+event.tool+' · '+event.status+' · '+event.reason_code;activity=text;node.classList.add(event.status==='RELEASED'?'allow':'block')}
  else if(event.kind==='text'){text='OpenCode 已释放回答\n\n'+event.text;node.classList.add('text');$('#'+side+'-final').textContent=event.text;activity='已收到原生 OpenCode 回答'}
  else if(event.kind==='finished'){text='OpenCode 进程结束 · '+event.wall_seconds+'s'+(event.error?' · '+event.error:'');activity=text}
  else if(event.kind==='native_error'){text='OpenCode 错误 · '+event.error;activity=text;node.classList.add('block')}
  else return;
  node.append(document.createTextNode(text));if(extra)node.append(el('small',extra));const follow=feed.scrollHeight-feed.scrollTop-feed.clientHeight<65;feed.append(node);
  if(follow)feed.scrollTop=feed.scrollHeight;
  if(activity){const target=$('#'+side+'-activity');target.textContent=activity;target.classList.toggle('block',node.classList.contains('block'))}
}
async function refreshLive(){
  if(refreshing)return;refreshing=true;
  try{
    const q=new URLSearchParams();if($('#history').value)q.set('run',$('#history').value);if($('#pair').value)q.set('pair',$('#pair').value);
    q.set('cursor_run',liveOutput);q.set('cursor_pair',livePair);for(const side of ['off','on'])q.set('after_'+side,String(offsets[side]));
    const response=await fetch('/api/live?'+q),s=await response.json();if(!response.ok)throw Error(s.error);
    const key=s.output+'/'+s.pair;
    if(liveKey!==key){resetLive();liveKey=key}liveOutput=s.output||'';livePair=s.pair||'';
    const selector=$('#pair'),keys=(s.pairs||[]).map(p=>p.key).join('|');if(selector.dataset.keys!==keys){const current=selector.value;selector.replaceChildren(el('option','跟随当前执行'));selector.firstChild.value='';for(const p of s.pairs||[]){const option=el('option',p.case_id+' · '+p.title);option.value=p.key;selector.append(option)}selector.value=(s.pairs||[]).some(p=>p.key===current)?current:'';selector.dataset.keys=keys}
    liveBusy=s.busy;for(const id of ['run','smoke','full'])$('#'+id).disabled=s.busy;$('#stop').disabled=!s.busy;
    const phase=s.run_state?.phase;$('#badge').textContent=s.running?'真实 OpenCode 运行中':(phases[phase]||({COMPLETE:'本次已完成',FAILED:'本次运行失败'}[phase])||'等待运行');
    for(const side of ['off','on']){
      const lane=s.lanes?.[side];if(!lane)continue;const status=lane.status;
      $('#'+side+'-phase').textContent=side==='off'&&status.phase==='FINAL'?'生成最终回答':phases[status.phase]||status.phase;
      $('#'+side+'-counts').textContent='模型轮次 '+(status.turns||0)+' · 实际执行 '+(status.executed||0)+' · 拦截 '+((status.blocked||0)+(status.policy_rejected||0))+' · 恢复 '+(status.recoveries||0);
      $('#'+side+'-session').textContent=status.native_session_id?'OpenCode '+status.native_session_id+'\n'+(status.model||''):'';
      for(const event of lane.events)appendEvent(side,event);offsets[side]=lane.cursor;
      if(lane.final)$('#'+side+'-final').textContent=lane.final;
      if(lane.result){const r=lane.result;$('#'+side+'-phase').textContent=names[r.outcome]||r.outcome;if(!lane.events.length)$('#'+side+'-activity').textContent=r.reason_codes.join(' · ')+' '+r.diagnosis}
    }
    if(s.run_state?.error)$('#notice').textContent=s.run_state.error;
    else if(s.running)$('#notice').textContent=(s.pair||'检查隔离环境')+' · 两侧显示真实运行事件，等待模型期间无需刷新。';
    else if(s.pair)$('#notice').textContent='当前显示 '+s.pair+'。可输入新提示词开始下一次独立对照。';
  }catch(e){$('#notice').textContent='读取实时状态失败：'+e.message}finally{refreshing=false}
}
async function loadHistory(){const list=await(await fetch('/api/runs')).json();const current=$('#history').value;$('#history').replaceChildren(el('option','当前 / 最新'));$('#history').firstChild.value='';for(const r of list){const option=el('option',r.id+' · '+r.status);option.value=r.id;$('#history').append(option)}$('#history').value=current}
async function refreshReport(){try{const q=$('#history').value?'?run='+encodeURIComponent($('#history').value):'';const s=await(await fetch('/api/status'+q)).json();if(s.error)return;$('#output').textContent=s.output||'';$('#log').textContent=s.log||'本记录没有活动批次日志。';if(!s.report)return;const r=s.report;$('#report-notice').textContent='已记录 '+r.rows.length+' / '+r.manifest.expected_runs+' 组；有效攻击配对 '+r.paired.known_pairs+'；防护胜 / 负 '+r.paired.protected_wins+' / '+r.paired.protected_losses+(r.manifest.manual?'。自定义任务无预设攻击目标，不计算 ASR。':'。');const metrics=$('#metrics');metrics.replaceChildren();for(const side of ['off','on']){const m=r.metrics[side];metrics.append(el('span',side.toUpperCase()+' · ASR '+pct(m.asr)+' · 正常效用 '+pct(m.benign_utility)+' · 故障 '+m.errors))}lastRows=r.rows;const body=$('#rows');body.replaceChildren();r.rows.forEach((row,i)=>{const tr=el('tr','');for(const text of [row.case_id+' · '+row.variant,row.mode.toUpperCase(),(names[row.outcome]||row.outcome)+' · '+row.reason_codes.join(', '),row.wall_seconds==null?'—':row.wall_seconds+'s'])tr.append(el('td',text));const td=el('td',''),b=el('button','详情');b.onclick=()=>detail(i);td.append(b);tr.append(td);body.append(tr)})}catch(e){$('#report-notice').textContent=e.message}}
async function detail(i){const r=await(await fetch('/api/detail?directory='+encodeURIComponent(lastRows[i].directory))).json();$('#detail-body').textContent=JSON.stringify(r,null,2);$('#detail').showModal()}
$('#close-detail').onclick=()=>$('#detail').close();$('#history').onchange=()=>{$('#pair').value='';resetLive();refreshLive();refreshReport()};$('#pair').onchange=()=>{resetLive();refreshLive()};
async function init(){cases=await(await fetch('/api/cases')).json();renderCases();for(const c of cases){const option=el('option',c.id+' · '+c.title);option.value=c.id;$('#fixture').append(option)}$('#fixture').value='b-report';await template(true);await loadHistory();refreshLive();refreshReport()}
init().catch(e=>$('#notice').textContent=e.message);setInterval(refreshLive,750);setInterval(refreshReport,2500);
