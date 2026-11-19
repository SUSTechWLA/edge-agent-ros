'use strict';
// The task journal owns goals, current outcomes and steps. Archive seq numbers
// refer to observed facts and are the only IDs accepted by the replay dialog.
const RunReview = (() => {
  const get = id => document.getElementById(id);
  const stateNames = {
    PLANNING:'规划中', RUNNING:'执行中', WAITING:'等待结果', SUCCEEDED:'已成功', BLOCKED:'已受阻',
    RECOVERY_REQUIRED:'待恢复', FAILED:'已失败', CANCELED:'已取消', PENDING:'待执行', VERIFYING:'核验中'
  };
  const stateTone = state => state==='SUCCEEDED' ? 'ok' :
    ['BLOCKED','RECOVERY_REQUIRED','FAILED'].includes(state) ? 'warn' : '';
  const titleOf = {
    'agent.accepted':'受理任务', 'agent.planned':'生成执行计划', 'agent.dispatch':'记录工具执行意图',
    'agent.verified':'步骤证据已核验', 'agent.blocked':'任务受阻', 'agent.error':'任务需要恢复',
    'agent.retry':'准备重试', 'agent.retry_exhausted':'达到重试上限', 'agent.retry_requested':'请求续办',
    'agent.transition':'任务状态改变', 'motion.changed':'运动状态改变', 'perception.mode':'感知模式改变',
    'source.online':'数据源更新', 'source.stale':'数据源过期', 'mission.created':'创建 ROS 委托',
    'agent.planner.context':'准备规划上下文','agent.tool.error':'工具执行报错',
    'agent.tool.request':'工具调用请求','agent.tool.response':'收到工具回执',
    'agent.ros.request':'调用 ROS 接口','agent.ros.response':'收到 ROS 回执',
    'agent.adapter.request':'调用适配器','agent.adapter.response':'收到适配器回执',
    'agent.model.request':'发送模型请求','agent.model.response':'收到模型应答',
    'action.started':'动作开始','action.braking':'设备制动','action.result':'收到动作结果',
    'operation.changed':'运动许可变化','step.dispatch':'下发 ROS 航点','step.result':'收到航点结果',
    'step.verified':'核验实测航点','mission.detail':'ROS 委托进展','mission.transition':'ROS 委托状态改变',
    'scene.route_planned':'计算场景路线','ros.log':'ROS 节点日志'
  };
  const timeFormat = new Intl.DateTimeFormat('zh-CN',{
    year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false
  });
  const date = value => {
    if(value==null || value==='')return '—';
    const d = new Date(typeof value==='number' ? value*1000 : value);
    return Number.isFinite(d.valueOf()) ? timeFormat.format(d) : String(value);
  };
  const detailLink = id => '#review?'+new URLSearchParams({task:id});
  const pager = () => ({cursors:[null], next:null, ticket:0, pending:false});
  const list = pager(), history = pager();
  let open=false, taskId=null, task=null, taskFilter='all', historyView='changes';
  let ownerRequest=0, evidenceRequest=0, evidence=[], more=false;
  function notice(id,message='') {
    get(id).textContent=message; get(id).hidden=!message;
  }
  function empty(id,message) {
    get(id).replaceChildren(node('p','empty-state',message));
  }
  function liveStatus(s,connected) {
    const op=s?.sources?.operation, perception=s?.sources?.perception, odom=s?.sources?.odom;
    const velocity=odom?.fresh && Number.isFinite(odom.data?.v) && Number.isFinite(odom.data?.w)
      ? '里程计 '+fmt(odom.data.v,3)+' m/s · '+fmt(odom.data.w,3)+' rad/s' : '';
    const operation=s?.active_operation?'正在执行设备动作':op?.fresh?'当前无活动设备动作':'当前动作状态未知';
    get('review-operation').textContent=!connected ? (s?'实时连接中断':'等待观测服务') :
      (s.active_operation||!velocity)?operation:velocity;
    const inputs = [...Object.values(s?.sources||{}),...Object.values(s?.extra_sources||{})];
    const line = inputs.length ? '观测来源 '+inputs.filter(x=>x.fresh).length+' / '+inputs.length+' 项有新数据' : '等待来源状态';
    get('review-freshness').textContent=[s?.active_operation,connected && velocity?(s.active_operation?velocity:operation):'',
      line,!connected && s?'显示上次读取值':''].filter(Boolean).join(' · ');
    const mode=perception?.data?.mode;
    get('review-mode').textContent=mode ? ({wheel_imu:'轮速 + IMU',wheel_only:'仅轮式里程计'}[mode]||mode)+(perception.fresh?'':'（过期）') : '未知';
    get('review-now').textContent=date(s?.now);
  }
  function resetPage(p) { p.cursors=[null]; p.next=null; }
  function pageControls(prefix,p,loading=false) {
    get(prefix+'-prev').disabled=loading || p.cursors.length<2;
    get(prefix+'-next').disabled=loading || p.next==null;
    get(prefix+'-page').textContent='第 '+p.cursors.length+' 页';
  }
  function bindPager(p,prefix,load) {
    get(prefix+'-prev').onclick=()=>{if(p.cursors.length>1){p.cursors.pop();p.next=null;load();}};
    get(prefix+'-next').onclick=()=>{if(p.next!=null){p.cursors.push(p.next);p.next=null;load();}};
  }
  function entryOf(t) {
    const link=node('a','review-entry'); link.href=detailLink(t.id);
    const heading=node('div','review-entry-heading');
    heading.append(node('strong','',t.text||'未记录目标文本'),node('span','badge '+stateTone(t.state),stateNames[t.state]||t.state));
    link.append(heading,node('p','review-entry-detail',t.detail||'暂无结果说明'),
      node('small','review-meta',date(t.created)+' · '+t.id));
    return link;
  }
  async function loadTasks(background=false) {
    const ticket=++list.ticket, query=new URLSearchParams({state:taskFilter,limit:8});
    list.pending=true;
    get('review-task-list').setAttribute('aria-busy','true');
    if(!background){
      list.next=null;
      notice('review-list-notice');
      empty('review-task-list','正在读取此分类的任务…');
    }
    if(list.cursors.at(-1)!=null)query.set('before',list.cursors.at(-1));
    pageControls('review',list,!background);
    try {
      const r=await api('/api/review/tasks?'+query);
      if(ticket!==list.ticket)return;
      notice('review-list-notice');
      document.querySelectorAll('[data-review-state]').forEach(b=>{
        b.querySelector('strong').textContent=r.counts[b.dataset.reviewState];
      });
      if(r.tasks.length)get('review-task-list').replaceChildren(...r.tasks.map(entryOf));
      else empty('review-task-list','此分类暂无任务。');
      list.next=r.next_before; pageControls('review',list);
    } catch(e) {
      if(ticket!==list.ticket)return;
      list.next=null; pageControls('review',list);
      document.querySelectorAll('[data-review-state] strong').forEach(n=>n.textContent='—');
      get('review-task-list').replaceChildren();
      notice('review-list-notice','任务账本暂不可读 · '+e.message+'。点击任务分类可重新读取。');
    } finally {
      if(ticket===list.ticket){
        list.pending=false;
        get('review-task-list').setAttribute('aria-busy','false');
      }
    }
  }
  function eventDescription(ev) {
    const d=ev.data||{};
    if(ev.kind==='step.dispatch' && Number.isFinite(d.target_x) && Number.isFinite(d.target_y))
      return '目标位置 ('+fmt(d.target_x)+', '+fmt(d.target_y)+') m';
    if(ev.kind==='step.verified' && Number.isFinite(d.observed_x) && Number.isFinite(d.observed_y))
      return '核验位置 ('+fmt(d.observed_x)+', '+fmt(d.observed_y)+') m';
    if(ev.kind==='action.braking' && Number.isFinite(d.front_range_m))
      return '制动时记录的前向距离 '+fmt(d.front_range_m)+' m';
    if(ev.kind==='mission.transition' && d.prior_state && d.state)
      return (stateNames[d.prior_state]||d.prior_state)+' → '+(stateNames[d.state]||d.state);
    if(ev.code==='navigation.path_blocked' || ev.detail?.includes('[code:navigation.path_blocked]'))
      return '导航执行层报告路径受阻';
    if(ev.code==='navigation.arrived' || ev.detail?.includes('[code:navigation.arrived]'))
      return '导航执行层报告已到达目标';
    if(ev.detail===ev.kind || ev.detail==='accepted' || 'agent.'+ev.detail===ev.kind)return '';
    return ev.detail;
  }
  function eventButton(ev,trace=false) {
    const button=node('button','review-event');
    button.dataset.seq=ev.seq;
    const summary=node('span','review-event-body');
    const title=Object.hasOwn(titleOf,ev.kind)?titleOf[ev.kind]:ev.kind;
    summary.append(node('strong','',title));
    const description=eventDescription(ev);
    if(description && description!==title)summary.append(node('span','',description));
    const eventTask=ev.task_id||ev.data?.task_id||ev.data?.parent_task_id;
    const metadata=['#'+ev.seq, date(ev.utc), ev.module||ev.source];
    if(trace && eventTask!==taskId)metadata.push(eventTask?'关联任务 '+eventTask:'归档未注明所属任务');
    if(ev.code && ev.code!==ev.kind)metadata.push(ev.code);
    summary.append(node('small','review-meta',metadata.filter(Boolean).join(' · ')));
    button.append(node('i','review-mark '+ev.level),summary,node('span','arrow','›'));
    button.onclick=()=>showEvent(ev);
    return button;
  }
  async function loadHistory(background=false) {
    const ticket=++history.ticket, query=new URLSearchParams({view:historyView,limit:8});
    history.pending=true;
    get('review-history-rows').setAttribute('aria-busy','true');
    if(!background){
      history.next=null;
      notice('review-history-notice');
      empty('review-history-rows','正在读取此分类的历史记录…');
    }
    if(history.cursors.at(-1)!=null)query.set('before',history.cursors.at(-1));
    pageControls('history',history,!background);
    get('review-history-note').textContent=historyView==='issues'
      ? '曾经记录的告警、错误和故障。当前状态请核对实时观测和任务账本。'
      : '观测来源、模块、动作和任务的已归档事件。周期快照可在「事件记录」查看。';
    try {
      const r=await api('/api/events?'+query);
      if(ticket!==history.ticket)return;
      notice('review-history-notice');
      if(r.events.length)get('review-history-rows').replaceChildren(...r.events.map(ev=>eventButton(ev)));
      else empty('review-history-rows',historyView==='issues'?'暂无已归档的历史问题。':'暂无已归档的状态变化。');
      history.next=r.has_more?r.events.at(-1)?.seq:null; pageControls('history',history);
    } catch(e) {
      if(ticket!==history.ticket)return;
      history.next=null; pageControls('history',history);
      get('review-history-rows').replaceChildren(); notice('review-history-notice','历史记录暂不可读 · '+e.message);
    } finally {
      if(ticket===history.ticket){
        history.pending=false;
        get('review-history-rows').setAttribute('aria-busy','false');
      }
    }
  }
  function showSteps(steps) {
    const target=get('review-steps');
    if(!steps?.length){empty('review-steps','账本尚无已记录的执行步骤。');return;}
    const list=node('ol','review-step-list');
    steps.forEach((step,index)=>{
      const item=node('li'), details=node('details'), summary=node('summary');
      const tool=typeof step==='string'?step:step.tool||step.capability||'未记录工具';
      summary.append(node('strong','','步骤 '+(index+1)+' · '+tool));
      if(step?.status)summary.append(node('span','badge '+stateTone(step.status),'最后记录 · '+(stateNames[step.status]||step.status)));
      details.append(summary,node('pre','',JSON.stringify(step,null,2)));item.append(details);list.append(item);
    });
    target.replaceChildren(node('h3','','计划与步骤结果'),list);
  }
  function ownerDiagnostic(t) {
    const latest=t.events?.at(-1);
    let payload=latest?.payload;
    if(typeof payload==='string'){try{payload=JSON.parse(payload);}catch{return null;}}
    return payload?.diagnostic||payload?.trace?.diagnosis;
  }
  function showTask(t) {
    const badge=get('review-result');
    badge.className='badge '+stateTone(t.state); badge.textContent=(stateNames[t.state]||t.state)+' · '+t.state;
    get('review-task-goal').textContent=t.text||'账本未记录目标文本';
    const diag=ownerDiagnostic(t);
    const parts=[diag?.code==='navigation.path_blocked'?'导航执行层报告路径受阻':t.detail];
    if(diag){
      if(diag.message && diag.message!==t.detail)parts.push(diag.message);
      if(diag.code)parts.push('诊断码 '+diag.code);
      if(diag.catalog?.hint)parts.push(diag.catalog.hint);
    }
    get('review-task-outcome').textContent=parts.filter(Boolean).join(' · ')||'尚无结果说明';
    const facts=[['创建时间',date(t.created)],['最后更新',date(t.updated)],['任务 ID',t.id],['调用链 ID',t.trace_id||'未记录']];
    if(t.provider)facts.push(['规划来源',t.provider]);
    if(t.trigger)facts.push(['触发来源',[t.trigger.kind,t.trigger.source].filter(Boolean).join(' · ')]);
    fields(get('review-identities'),facts);
    if(t.trigger?.parent_task_id){
      const link=node('a','',t.trigger.parent_task_id);link.href=detailLink(t.trigger.parent_task_id);
      const dd=node('dd');dd.append(link);get('review-identities').append(node('dt','','父任务'),dd);
    }
    showSteps(t.steps);get('review-scope').querySelector('[value="trace_id"]').disabled=!t.trace_id;
  }
  async function loadOwner() {
    const ticket=++ownerRequest;
    try {
      const t=await readTask(taskId,1);
      if(ticket!==ownerRequest)return;
      task=t;showTask(t);
    } catch(e) {
      if(ticket!==ownerRequest)return;
      task=null; get('review-result').className='badge warn';get('review-result').textContent='账本详情不可用';
      get('review-task-goal').textContent=e.status===404?'任务账本未找到记录':'无法读取任务账本';
      get('review-task-outcome').textContent=e.message+'。仍可检查此任务 ID 的归档证据。';
      fields(get('review-identities'),[['任务 ID',taskId]]);
      get('review-steps').replaceChildren(); get('review-scope').querySelector('[value="trace_id"]').disabled=true;
      if(get('review-scope').value==='trace_id'){get('review-scope').value='task_id';loadEvidence();}
    }
  }
  function renderTimeline() {
    const target=get('review-timeline'), trace=get('review-scope').value==='trace_id';
    if(!evidence.length){empty('review-timeline','未找到此范围的归档事件。任务结果可在上方账本核对。');return;}
    const expanded=new Set(Array.from(target.querySelectorAll('details[open]'),d=>d.dataset.start));
    const fragment=document.createDocumentFragment();
    let exchanges=[];
    function flush() {
      if(!exchanges.length)return;
      const group=node('details','review-exchanges'),start=String(exchanges[0].seq);
      group.dataset.start=start;group.open=expanded.has(start);
      group.append(node('summary','','展开 '+exchanges.length+' 条底层调用记录 · #'+start+' – #'+exchanges.at(-1).seq));
      for(const ev of exchanges)group.append(eventButton(ev,trace));
      fragment.append(group);exchanges=[];
    }
    for(const ev of evidence){
      if(/^agent\.(tool|ros|model|adapter)\.(request|response)$/.test(ev.kind) && !['warn','error'].includes(ev.level))exchanges.push(ev);
      else {flush();fragment.append(eventButton(ev,trace));}
    }
    flush();target.replaceChildren(fragment);
  }
  async function loadEvidence(append=false) {
    const ticket=++evidenceRequest, scope=get('review-scope').value;
    const query=new URLSearchParams({[scope]:scope==='trace_id'?task?.trace_id:taskId});
    get('review-export').href='/api/export?'+query;
    if(!append){
      evidence=[];more=false;
      empty('review-timeline','正在读取归档事件…');
    }
    get('review-evidence-more').hidden=true;
    notice('review-evidence-notice');
    get('review-time-note').textContent=(scope==='trace_id'?'同一调用链可包含多个任务。':'仅展示本任务及其 ROS 委托。')+'按归档编号排列。来源报告的时间按浏览器当地时间显示。点击事件可核对前因或回放。';
    query.set('after',append&&evidence.length?evidence.at(-1).seq:0);query.set('limit',100);
    try {
      const r=await api('/api/events?'+query);
      if(ticket!==evidenceRequest)return;
      evidence=evidence.concat(r.events);more=r.has_more;
      renderTimeline();get('review-evidence-more').hidden=!more;
    } catch(e) {
      if(ticket!==evidenceRequest)return;
      if(!append)get('review-timeline').replaceChildren();
      notice('review-evidence-notice','归档读取失败 · '+e.message+'。可刷新结果后重试。');
      get('review-evidence-more').hidden=!append;
    }
  }
  function route(active) {
    open=active;
    // Invalidate late responses even when the same task is reopened.
    list.ticket++;history.ticket++;ownerRequest++;evidenceRequest++;
    if(!active)return;
    taskId=new URLSearchParams(location.hash.split('?')[1]).get('task')||null;
    get('review-home').hidden=!!taskId;get('review-detail').hidden=!taskId;
    if(!taskId){loadTasks();loadHistory();return;}
    task=null; get('review-task-goal').textContent='读取任务…';
    get('review-result').className='badge';get('review-result').textContent='读取中';
    get('review-task-outcome').textContent='';get('review-steps').replaceChildren();
    fields(get('review-identities'),[['任务 ID',taskId]]);get('review-scope').value='task_id';
    get('review-scope').querySelector('[value="trace_id"]').disabled=true;
    loadOwner();loadEvidence();
  }
  function refresh() {
    if(!open || get('event-modal').open || taskId)return;
    // Keep a reader's older pages stable while the first page follows the archive.
    if(!list.pending && list.cursors.length===1 && !get('review-task-list').contains(document.activeElement))loadTasks(true);
    if(!history.pending && history.cursors.length===1 && !get('review-history-rows').contains(document.activeElement))loadHistory(true);
  }
  bindPager(list,'review',loadTasks);
  bindPager(history,'history',loadHistory);
  document.querySelectorAll('[data-review-state]').forEach(btn=>btn.onclick=()=>{
    taskFilter=btn.dataset.reviewState;resetPage(list);
    document.querySelectorAll('[data-review-state]').forEach(b=>b.setAttribute('aria-pressed',String(b===btn)));
    loadTasks();
  });
  get('review-history-tabs').querySelectorAll('[data-view]').forEach(btn=>btn.onclick=()=>{
    historyView=btn.dataset.view;resetPage(history);
    get('review-history-tabs').querySelectorAll('[data-view]').forEach(b=>b.setAttribute('aria-pressed',String(b===btn)));
    loadHistory();
  });
  get('review-scope').onchange=()=>loadEvidence();
  get('review-evidence-more').onclick=()=>loadEvidence(true);
  get('review-detail-refresh').onclick=()=>{loadOwner();loadEvidence();};
  return {route,refresh,liveStatus};
})();
