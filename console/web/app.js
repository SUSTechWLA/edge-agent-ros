'use strict';
const el=id=>document.getElementById(id);
const fmt=(n,d=2)=>Number.isFinite(n)?(Math.abs(n)<0.5*10**(-d)?0:n).toFixed(d):'—';
const clock=utc=>{const d=new Date(typeof utc==='number'?utc*1000:utc);return Number.isFinite(d.valueOf())?d.toLocaleTimeString('zh-CN',{hour12:false}):'—';};
function node(tag,cls,text){const n=document.createElement(tag);if(cls)n.className=cls;if(text!=null)n.textContent=text;return n;}
function fields(container,pairs){
  const deflist=container.tagName==='DL', frag=document.createDocumentFragment();
  for(const [key,v] of pairs){if(deflist)frag.append(node('dt','',key),node('dd','',v));else{const div=node('div','field');div.append(node('span','',key),node('b','mono',v));frag.append(div);}}
  container.replaceChildren(frag);
}
async function api(path,opts={}){
  const res=await fetch(path,{cache:'no-store',signal:AbortSignal.timeout(8500),...opts});
  let data;try{data=await res.json();}catch{throw new Error('服务响应格式异常（HTTP '+res.status+'）');}
  if(!res.ok)throw new Error(typeof data.error==='string'?data.error:data.detail||'服务返回 HTTP '+res.status);
  return data;
}
const icons={map:'<path d="m3 6 6-3 6 3 6-3v15l-6 3-6-3-6 3Z M9 3v15 M15 6v15"/>',scan:'<path d="M4 9V4h5m6 0h5v5m0 6v5h-5m-6 0H4v-5"/><circle cx="12" cy="12" r="4"/><path d="m12 12 5-5"/>',layers:'<path d="m3 8 9-5 9 5-9 5-9-5Zm0 4 9 5 9-5m-18 4 9 5 9-5"/>',activity:'<path d="M2 13h5l3-7 4 12 3-6h5"/>',task:'<path d="M5 4h14v16H5z M8 8h8M8 12h8M8 16h5"/>'};
document.querySelectorAll('[data-icon]').forEach(i=>i.innerHTML='<svg viewBox="0 0 24 24" aria-hidden="true">'+icons[i.dataset.icon]+'</svg>');
let live=null, replay=null, replayRecord=null, liveOk=false, requestBusy=false, sensor='scan', recent=[], page=[], selected=null, eventRequest=0;
let pageBefore=null, eventFilters=new URLSearchParams(), observedKinds=new Set(), taskRecord=null, taskDraft=null, submitBusy=false;
const filterInputs={task_id:'task-filter',mission_id:'mission-filter',kind:'kind-filter',trace_id:'trace-filter',
  module:'module-filter',code:'code-filter',trigger_kind:'trigger-kind-filter',trigger_id:'trigger-filter'};
const section=()=>location.hash==='#events'?'events':'monitor';
const displayed=()=>replay||live;
function health(out,source){
  if(!replay && live && !liveOk){out.className='badge warn';out.textContent='连接中断';return;}
  if(source?.status==='error'){out.className='badge warn';out.textContent='来源异常';return;}
  if(source?.status==='stale' && !source.available){out.className='badge warn';out.textContent='超时未采样';return;}
  out.className='badge '+(source?.available?(source.fresh?'ok':'warn'):'');
  out.textContent=source?.available?(source.fresh?'有新数据':'数据过期'):'等待数据';
}
let extraCatalog='';
function syncSensorPicker(extras){
  const entries=Object.entries(extras).map(([id,source])=>[id,source.name||id]).sort(([a],[b])=>a.localeCompare(b));
  const catalog=JSON.stringify(entries), select=el('extra-sensor');
  if(catalog!==extraCatalog){
    extraCatalog=catalog;
    select.replaceChildren(new Option('选择传感器',''),...entries.map(([id,name])=>new Option(name+' · '+id,id)));
  }
  el('extra-picker').hidden=entries.length===0;
  const id=sensor.startsWith('extra:')?sensor.slice(6):'';
  if(id && !Object.hasOwn(extras,id))sensor='scan';
  select.value=sensor.startsWith('extra:')?id:'';
  document.querySelectorAll('[data-sensor]').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset.sensor===sensor)));
}
function renderExtra(id,item){
  health(el('raw-health'),item);
  el('raw-topic').textContent=item.topic||'未指定话题';
  el('polar').hidden=true;
  el('raw-values').textContent=item.error||item.name||id;
  fields(el('raw-fields'),[
    ['消息类型',item.type||'—'],['坐标系',item.frame||'—'],
    ['ROS 采样时间',Number.isFinite(item.stamp_s)?fmt(item.stamp_s,6)+' s':'无源时间戳'],
    ['样本年龄 / 过期阈值',fmt(item.age_s,1)+' / '+fmt(item.ttl_s,1)+' s']
  ]);
  const warning=el('raw-warning');
  let message=item.truncated?'采集端已截断负载。以下预览仅含保留的字段和数据。':'';
  if(document.querySelector('.payload').open){
    const limit=16000;
    const json=item.data==null?'暂无采样':JSON.stringify(item.data,null,2);
    const clipped=json.length>limit;
    el('raw-json').textContent=clipped?json.slice(0,limit)+'\n…':json;
    if(clipped)message+=(message?' ':'')+'页面预览限前 '+limit+' 个字符。';
  }
  warning.textContent=message;
  warning.hidden=!message;
}
function renderRaw(state){
  const extras=state?.extra_sources||{};
  syncSensorPicker(extras);
  if(sensor.startsWith('extra:')){
    const id=sensor.slice(6);
    renderExtra(id,extras[id]);return;
  }
  el('raw-warning').hidden=true;
  const item=state?.sources?.[sensor],d=item?.data||null;
  el('raw-topic').textContent=item?.topic||('/'+sensor);health(el('raw-health'),item);
  el('polar').hidden=sensor!=='scan'||!d;
  let pairs=[],description='';
  if(sensor==='scan' && d){
    EdgeMap.polar(d);const hits=d.ranges.filter(v=>Number.isFinite(v)&&v>=d.range_min&&v<d.range_max);
    pairs=[['有效命中 / 波束',hits.length+' / '+d.ranges.length],['最近回波',hits.length?fmt(Math.min(...hits))+' m':'无回波']];
  }else if(sensor==='imu' && d){pairs=[['z 轴角速度',fmt(d.angular_z,3)+' rad/s'],['XY 加速度',fmt(d.linear_x)+' / '+fmt(d.linear_y)+' m/s²']];description='机体惯性测量';}
  else if(sensor==='odom' && d){pairs=[['轮式位置 XY',fmt(d.x)+' / '+fmt(d.y)+' m'],['线速度 / 角速度',fmt(d.v)+' m/s · '+fmt(d.w)+' rad/s']];description='轮式里程计读数';}
  else description='尚未收到该来源的采样';
  el('raw-values').textContent=description;
  pairs.push(['坐标系',d?.frame||'—'],['样本年龄 / 频率',(Number.isFinite(item?.age_s)?fmt(item.age_s,1)+' s':'—')+' · '+(Number.isFinite(item?.rate_hz)?fmt(item.rate_hz,1)+' Hz':'—')]);
  fields(el('raw-fields'),pairs);
  if(document.querySelector('.payload').open)el('raw-json').textContent=d?JSON.stringify({topic:item.topic,stamp_s:item.stamp_s,data:d},null,2):'等待采样';
}
function render(){
  const s=displayed(),src=s?.sources||{},pose=src.pose?.data,odom=src.odom?.data,grid=src.map?.data,diag=src.perception?.data;
  el('env').textContent=s?.environment==='simulation'?'Gazebo 仿真':s?.environment==='robot'?'实机':'ROS 2';
  el('robot').textContent=s?.robot_id||'Edge';
  el('clock').textContent=s?.now?clock(s.now):'—';el('clock-label').textContent=replay?'历史快照时间':'状态更新时间';
  el('conn').textContent=replay?'历史回放':liveOk?'服务已连接':live?'服务连接中断':'等待服务';
  el('conn-pip').className='pip '+(replay?'history':liveOk?'ok':live?'bad':'');
  el('metric-pos').textContent=pose?fmt(pose.x)+' , '+fmt(pose.y):'—';
  el('metric-yaw').textContent=pose?fmt(pose.yaw*180/Math.PI,1):'—';
  el('metric-speed').textContent=odom?fmt(odom.v,3):'—';
  el('metric-action').textContent=s?(s.active_operation||'无活动动作'):'—';
  el('history-bar').hidden=!replay;el('open-task').disabled=!!replay;
  if(replay){
    const telemetry=replay.telemetry_at===null?' · 此事件没有同期遥测':Number.isFinite(replay.telemetry_at)?' · 遥测 '+clock(replay.telemetry_at):'';
    el('history-msg').textContent='历史回放　#'+replayRecord.seq+'　'+clock(replayRecord.utc)+'　'+replayRecord.kind+telemetry;
  }
  el('task-panel').hidden=!!replay||!taskRecord;
  if(section()==='events')return;
  EdgeMap.update(s);renderRaw(s);
  health(el('fusion-health'),src.pose);
  el('fusion-desc').textContent=!pose?'等待融合位姿与建图数据':diag?.mode==='wheel_only'?'当前采用轮式里程计推进位姿，激光观测用于建图。':'轮速估计平移，惯导与轮式航向共同估计朝向。激光观测用于建图。';
  const fusionMode={wheel_imu:'轮速 + IMU',wheel_only:'仅轮式里程计'}[diag?.mode]||diag?.mode||'—';
  fields(el('fusion-fields'),[['位姿来源',src.pose?.topic||'—'],['融合模式',fusionMode],['地图来源',src.map?.topic||'—'],['地图分辨率',grid?fmt(grid.resolution,3)+' m/格':'—']]);
}
document.querySelectorAll('[data-sensor]').forEach(btn=>btn.onclick=()=>{
  sensor=btn.dataset.sensor;renderRaw(displayed());
});
el('extra-sensor').onchange=e=>{sensor=e.target.value?'extra:'+e.target.value:'scan';renderRaw(displayed());};
document.querySelector('.payload').addEventListener('toggle',()=>renderRaw(displayed()));
function eventsRows(where,records,message){
  const frag=document.createDocumentFragment();
  for(const ev of records){
    const b=node('button','event-row'), k=node('span','event-key'), pip=node('i',ev.level);
    k.append(pip,document.createTextNode(ev.kind));k.title=ev.kind;
    const desc=node('span','event-detail',ev.detail||ev.source);desc.title=ev.detail||ev.source;
    b.append(node('time','event-time',clock(ev.utc)),k,desc,node('span','arrow','›'));
    b.setAttribute('aria-label','查看事件 '+ev.seq+' '+ev.kind+' '+(ev.detail||''));
    b.onclick=()=>showEvent(ev);frag.append(b);
  }
  if(!records.length)frag.append(node('div','empty-state',message));
  el(where).replaceChildren(frag);
}
function showEvent(ev){
  selected=ev;el('event-title').textContent='事件 #'+ev.seq;el('event-summary').textContent=ev.detail||ev.kind;
  const agentEvent=ev.source==='mission_agent/tasks';
  const details=[['发生时间',ev.utc],['类型 / 等级',ev.kind+' · '+ev.level],['采集来源',ev.source],
    ['模块',ev.module],['诊断码',ev.code],['所属层',ev.layer],['调用链 ID',ev.trace_id],['事件 ID',ev.event_id],
    ['触发类型',ev.trigger_kind],['触发 ID',ev.trigger_id],['触发来源',ev.trigger_source],
    ['自然语言任务',ev.task_id||ev.data?.task_id||(agentEvent?ev.mission_id:null)],
    ['ROS 委托',agentEvent?null:ev.mission_id],['动作 ID',ev.operation_id],['调用 ID',ev.call_id],
    ['直接前因',ev.cause_seq?'事件 #'+ev.cause_seq:(ev.parent_event_id?'待归档 · '+ev.parent_event_id:'未记录')]];
  fields(el('event-fields'),details.filter(([,v])=>v!=null));
  if(ev.cause_seq){
    const jump=node('button','cause','查看事件 #'+ev.cause_seq);
    jump.onclick=async()=>{try{const r=await api('/api/events?before='+(ev.cause_seq+1)+'&limit=1');if(r.events[0]?.seq!==ev.cause_seq)throw new Error('找不到该事件');showEvent(r.events[0]);}catch(e){el('replay-error').textContent=e.message;}};
    el('event-fields').lastChild.replaceChildren(jump);
  }
  el('event-json').textContent=JSON.stringify(ev,null,2);el('replay-error').textContent='';el('replay').disabled=false;
  if(!el('event-modal').open)el('event-modal').showModal();
}
el('close-modal').onclick=()=>el('event-modal').close();
function pageQuery(){
  return new URLSearchParams(eventFilters);
}
function registerKinds(records){
  for(const r of records)observedKinds.add(r.kind);
  el('kind-options').replaceChildren(...[...observedKinds].sort().map(k=>{const option=node('option');option.value=k;return option;}));
}
async function loadPage(before=null){
  const ticket=++eventRequest,q=pageQuery();q.set('limit','31');if(before!=null)q.set('before',before);
  el('export').href='/api/export?'+pageQuery().toString();
  try{
    const result=await api('/api/events?'+q);if(ticket!==eventRequest)return;
    pageBefore=before;page=result.events.slice(0,30);registerKinds(page);
    eventsRows('event-rows',page,'没有符合条件的记录');
    el('older').disabled=result.events.length<=30;el('newer').disabled=before==null;
    el('page-range').textContent=page.length?'#'+page[0].seq+' – #'+page[page.length-1].seq:'0 条';
  }catch(e){if(ticket===eventRequest)eventsRows('event-rows',[],'读取事件失败 · '+e.message);}
}
function applyFilter(){
  eventFilters=new URLSearchParams();
  for(const [key,id] of Object.entries(filterInputs)){const value=el(id).value.trim();if(value)eventFilters.set(key,value);}
  loadPage();
}
function clearFilterInputs(){Object.values(filterInputs).forEach(id=>el(id).value='');}
el('filter').onclick=applyFilter;el('clear-filter').onclick=()=>{clearFilterInputs();applyFilter();};
Object.values(filterInputs).forEach(id=>el(id).onkeydown=e=>{if(e.key==='Enter')applyFilter();});
el('older').onclick=()=>{if(page.length)loadPage(page.at(-1).seq);};el('newer').onclick=()=>loadPage();
function route(){
  const journal=section()==='events';
  el('monitor').hidden=journal;el('events').hidden=!journal;el('page-title').textContent=journal?'事件记录':'机器人观测台';
  el('nav-events').toggleAttribute('aria-current',journal);el('nav-live').toggleAttribute('aria-current',!journal);
  el(journal?'nav-events':'nav-live').setAttribute('aria-current','page');if(journal)loadPage(pageBefore);else if(!replay)eventsRows('recent-rows',recent,'尚无事件');render();
}
window.addEventListener('hashchange',route);
el('replay').onclick=async()=>{
  if(!selected)return;const ev=selected;el('replay').disabled=true;
  try{
    const s=await api('/api/replay?at='+encodeURIComponent(ev.seq));
    const log=await api('/api/events?before='+(ev.seq+1)+'&limit=5');
    replay=s;replayRecord=ev;el('event-modal').close();location.hash='monitor';route();
    eventsRows('recent-rows',log.events,'该时刻暂无事件');window.scrollTo({top:0,behavior:'smooth'});
  }catch(e){el('replay-error').textContent='无法回放 · '+e.message;}finally{el('replay').disabled=false;}
};
el('resume').onclick=()=>{replay=replayRecord=null;render();eventsRows('recent-rows',recent,'尚无事件');poll();};
function setNotice(text){el('notice').hidden=!text;el('notice').textContent=text||'';}
let lastCursor=null;
async function poll(){
  if(requestBusy)return;requestBusy=true;
  try{
    const s=await api('/api/state');live=s;liveOk=true;setNotice('');render();
    if(s.cursor!==lastCursor){
      const data=await api('/api/events?limit=5');recent=data.events;lastCursor=s.cursor;registerKinds(recent);
      if(!replay && section()==='monitor')eventsRows('recent-rows',recent,'尚无事件');
      if(section()==='events' && pageBefore==null && !el('event-modal').open)loadPage();
    }
  }catch(e){liveOk=false;setNotice('数据连接中断 · '+e.message+'。页面将自动重试。');render();}
  finally{requestBusy=false;}
}
const taskId=()=>typeof crypto.randomUUID==='function'?crypto.randomUUID():'task-'+Date.now().toString(36)+'-'+crypto.getRandomValues(new Uint32Array(1))[0].toString(36);
function renderTask(task){
  taskRecord=task;el('task-panel').hidden=!!replay;
  const steps=Array.isArray(task.steps)?task.steps:[];
  const title=node('p','task-line'),id=node('code','',task.id),badge=node('span','badge',task.state);
  title.append(id,badge);
  const content=node('div','task-detail',[task.detail,task.provider?'规划来源 '+task.provider:''].filter(Boolean).join(' · '));
  const list=node('ol','task-steps');
  for(const step of steps){
    const row=node('li','',typeof step==='string'?step:[step.capability||step.tool,step.status||step.state,step.detail].filter(Boolean).join(' · '));
    if(step?.tool==='status' && step.evidence?.source && Number.isFinite(step.evidence.x) && Number.isFinite(step.evidence.y)){
      const e=step.evidence;
      row.append(node('p','task-detail',
        (e.frame||'未知坐标系')+' · 位置 ('+fmt(e.x)+', '+fmt(e.y)+') m · 朝向 '+fmt(e.yaw*180/Math.PI,1)+'° · 速度 '+fmt(e.speed,3)+' m/s'));
    }
    if(typeof step==='object' && (step.args||step.evidence)){const more=node('details');more.append(node('summary','','参数与证据'),node('pre','',JSON.stringify({args:step.args,evidence:step.evidence,operation_id:step.operation_id},null,2)));row.append(more);}
    list.append(row);
  }
  el('task-status').replaceChildren(title,node('p','',task.text||''),content,list);
  if(task.can_retry){
    const retry=node('button','','故障已处理，续办原步骤');
    retry.onclick=async()=>{
      if(replay || taskRecord?.id!==task.id)return;
      retry.disabled=true;
      try{
        renderTask(await api('/agent/retry',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({id:task.id})}));
      }catch(err){retry.disabled=false;setNotice('续办未确认 · '+err.message);}
    };
    el('task-status').append(retry);
  }
}
async function capabilities(){
  try{const r=await api('/agent/capabilities');
    el('cap-list').textContent='可用能力　'+(r.capabilities||[]).map(x=>x.id).join(' · ');
  }catch(e){el('cap-list').textContent='能力目录暂不可用 · '+e.message;}
}
el('open-task').onclick=()=>{if(replay)return;el('task-modal').showModal();el('task-text').focus();capabilities();};
el('close-task').onclick=()=>el('task-modal').close();
el('task-example').onclick=()=>{el('task-text').value='依次巡检 (0.8,0) 和 (0.8,0.7)，然后报告状态';el('task-text').focus();};
el('task-form').onsubmit=async e=>{
  e.preventDefault();if(submitBusy||replay)return;const text=el('task-text').value.trim();if(!text)return;
  if(!taskDraft||taskDraft.text!==text)taskDraft={text,id:taskId()};
  submitBusy=true;el('task-submit').disabled=true;el('task-feedback').textContent='正在提交…';
  try{
    const r=await api('/agent/tasks',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(taskDraft)});
    renderTask(r);localStorage.setItem('edge-last-task',r.id);el('task-modal').close();
    el('task-text').value='';el('task-feedback').textContent='';taskDraft=null;location.hash='monitor';
    el('task-panel').scrollIntoView({behavior:'smooth',block:'center'});
  }catch(err){el('task-feedback').textContent='下发未确认 · '+err.message+'。重试会沿用任务 ID '+taskDraft.id;}
  finally{submitBusy=false;el('task-submit').disabled=false;}
};
let refreshingTask=false;
async function refreshTask(id,silent=false){
  if(!id||refreshingTask)return;refreshingTask=true;
  try{const r=await api('/agent/tasks?id='+encodeURIComponent(id));renderTask(r);}
  catch(e){if(!silent)setNotice('无法查询任务 · '+e.message);}finally{refreshingTask=false;}
}
el('refresh-task').onclick=()=>refreshTask(taskRecord?.id);
el('trace-task').onclick=()=>{if(!taskRecord)return;clearFilterInputs();el('task-filter').value=taskRecord.id;pageBefore=null;applyFilter();location.hash='events';};
route();poll();refreshTask(localStorage.getItem('edge-last-task'),true);
setInterval(()=>{if(!document.hidden)poll();},1000);
setInterval(()=>{if(!document.hidden && !replay && taskRecord && !['SUCCEEDED','CANCELED','FAILED','REJECTED','BLOCKED','RECOVERY_REQUIRED'].includes(taskRecord.state))refreshTask(taskRecord.id,true);},1800);
