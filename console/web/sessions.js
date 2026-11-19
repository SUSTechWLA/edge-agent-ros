'use strict';

// Human and model readers consume the same server-side evidence projection.
const SessionView = (() => {
  const get = id => document.getElementById(id);
  const labels = {PLANNING:'规划中',RUNNING:'执行中',WAITING:'等待结果',SUCCEEDED:'已完成',
    BLOCKED:'受阻',RECOVERY_REQUIRED:'待恢复',FAILED:'已失败',CANCELED:'已取消'};
  const roles = {user:'用户目标',agent:'Agent 记录',assistant:'Agent 记录',tool:'执行反馈',system:'系统记录'};
  const ongoing = state => ['PLANNING','RUNNING','WAITING'].includes(state);
  const stateLabel = state => Object.hasOwn(labels,state)?labels[state]:(state||'状态未记录');
  const tone = state => state==='SUCCEEDED'?'ok':['BLOCKED','RECOVERY_REQUIRED','FAILED'].includes(state)?'warn':'';
  const formatter = new Intl.DateTimeFormat('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',
    hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
  const date = value => {
    if(value==null||value==='')return '未记录时间';
    const d=new Date(typeof value==='number'?value*1000:value);
    return Number.isFinite(d.valueOf())?formatter.format(d):String(value);
  };
  const link = id => '#sessions?'+new URLSearchParams({task:id});
  let active=false, selectedId=null, projection=null, filter='all', cursors=[null], next=null;
  let listRequest=0, readerRequest=0, listBusy=false, readerBusy=false, lastRefresh=0;
  function notice(id,text='') { get(id).textContent=text;get(id).hidden=!text; }
  function liveStatus(s,connected) {
    const odom=s?.sources?.odom, op=s?.sources?.operation;
    let message=connected?'正在接收观测':'实时观测未连接';
    if(connected && odom?.fresh && Number.isFinite(odom.data?.v) && Number.isFinite(odom.data?.w)){
      message='实测速度 '+fmt(odom.data.v,3)+' m/s · '+fmt(odom.data.w,3)+' rad/s';
    }else if(connected && s?.active_operation)message='机器人报告有在途动作';
    else if(connected && op?.fresh)message='机器人报告无在途动作';
    get('session-motion').textContent=message;
    get('session-live-time').textContent=s?(connected?'采集于 ':'上次采集于 ')+date(s.now):'等待采集时间';
  }
  function controls() {
    get('session-prev').disabled=listBusy||cursors.length<2;
    get('session-next').disabled=listBusy||next==null;
    get('session-page').textContent='第 '+cursors.length+' 页';
  }
  function markSelected() {
    get('session-list').querySelectorAll('[data-task-id]').forEach(a=>{
      if(a.dataset.taskId===selectedId)a.setAttribute('aria-current','true');else a.removeAttribute('aria-current');
    });
  }
  function taskEntry(t) {
    const a=node('a','session-task');a.href=link(t.id);a.dataset.taskId=t.id;
    const meta=node('div','session-task-meta');
    meta.append(node('span','badge '+tone(t.state),stateLabel(t.state)),node('time','',date(t.created)));
    a.append(meta,node('strong','',t.text||'未记录任务原文'));
    return a;
  }
  function clearReader(title='正在整理任务会话…') {
    projection=null;readerRequest++;
    readerBusy=false;get('session-reader').setAttribute('aria-busy','false');
    get('session-refresh').disabled=!selectedId;
    get('session-state').className='badge';get('session-state').textContent='读取中';
    get('session-goal').textContent=title;get('session-headline').textContent='';get('session-dates').textContent='';
    get('session-provider').hidden=true;get('session-fallback').hidden=true;
    for(const id of ['session-messages','session-diagnosis','session-related'])get(id).replaceChildren();
    get('session-markdown').hidden=true;get('session-coverage').hidden=true;notice('session-notice');
  }
  async function loadList({chooseFirst=false,background=false}={}) {
    const ticket=++listRequest, query=new URLSearchParams({state:filter,limit:6});
    if(cursors.at(-1)!=null)query.set('before',cursors.at(-1));
    listBusy=true;get('session-list').setAttribute('aria-busy','true');controls();
    if(!background){get('session-list').replaceChildren(node('p','empty-state','正在读取任务…'));notice('session-list-notice');}
    try{
      const r=await api('/api/sessions?'+query);
      if(ticket!==listRequest)return;
      next=r.next_before;notice('session-list-notice');
      document.querySelectorAll('[data-session-state]').forEach(b=>b.querySelector('strong').textContent=r.counts[b.dataset.sessionState]);
      get('session-list').replaceChildren(...r.tasks.map(taskEntry));
      if(!r.tasks.length)get('session-list').append(node('p','empty-state','此分类暂无任务。'));
      if(chooseFirst){
        selectedId=r.tasks[0]?.id||null;
        history.replaceState(null,'',selectedId?link(selectedId):'#sessions');
        if(selectedId)loadSession();else{
          clearReader('此分类暂无任务');get('session-state').textContent='暂无任务';
          get('session-reader').setAttribute('aria-busy','false');get('session-refresh').disabled=true;
        }
      }
      markSelected();
    }catch(e){
      if(ticket!==listRequest)return;
      next=null;get('session-list').replaceChildren();
      document.querySelectorAll('[data-session-state] strong').forEach(n=>n.textContent='—');
      notice('session-list-notice','任务目录暂不可读 · '+e.message+'。点击分类可重试。');
      if(chooseFirst){clearReader('暂时无法读取任务目录');get('session-state').textContent='读取失败';}
    }finally{
      if(ticket===listRequest){listBusy=false;get('session-list').setAttribute('aria-busy','false');controls();}
    }
  }
  function references(refs) {
    const valid=(refs||[]).filter(r=>Number.isSafeInteger(r.seq)&&r.seq>0);
    if(!valid.length)return null;
    const details=node('details','session-refs');
    details.append(node('summary','','查看 '+valid.length+' 条证据'));
    const body=node('div','session-ref-buttons'),error=node('p','session-ref-error');
    error.setAttribute('role','status');
    for(const ref of valid){
      const button=node('button','','记录 #'+ref.seq);
      button.dataset.seq=ref.seq;button.title=ref.kind||'打开原始证据';
      button.onclick=async()=>{
        button.disabled=true;error.textContent='';
        const archive=projection?.coverage?.archive_id,taskAtClick=selectedId;
        try{
          const r=await api('/api/events?'+new URLSearchParams({before:ref.seq+1,limit:1}));
          if(!active||selectedId!==taskAtClick)return;
          if(archive && r.archive_id!==archive)throw new Error('归档身份已变化，请刷新会话后重新核对。');
          const event=r.events[0];
          if(event?.seq!==ref.seq)throw new Error('该条原始记录当前不可读取。');
          if(ref.event_id && event.event_id!==ref.event_id)throw new Error('证据身份不匹配，请刷新会话。');
          showEvent(event);
        }catch(e){error.textContent=e.message;}finally{button.disabled=false;}
      };
      body.append(button);
    }
    details.append(body,error);return details;
  }
  function messageCard(message) {
    const role=Object.hasOwn(roles,message.role)?message.role:'system';
    const card=node('article','session-message session-role-'+role);
    card.dataset.role=role;
    const header=node('div','session-message-heading');
    header.append(node('span','session-role',roles[role]),node('time','',date(message.utc)));
    card.append(header);
    if(message.title)card.append(node('h3','',message.title));
    const content=node('div','session-message-text');
    for(const paragraph of String(message.text||'').split('\n').filter(Boolean))content.append(node('p','',paragraph));
    card.append(content);
    const refs=references(message.refs);if(refs)card.append(refs);
    else if(message.source==='task_journal')card.append(node('small','session-source','依据任务账本'));
    return card;
  }
  function renderSession(r) {
    projection=r;const t=r.task, coverage=r.coverage||{};
    const service=r.service;
    get('session-provider').hidden=!service?.plugin_id;
    get('session-provider').textContent=service?.plugin_id
      ? 'Session 插件 '+service.plugin_id+' · v'+service.plugin_version : '';
    get('session-fallback').hidden=true;
    get('session-goal').textContent=t.text||'未记录任务原文';
    get('session-state').className='badge '+tone(t.state);get('session-state').textContent=stateLabel(t.state);
    get('session-headline').textContent=r.headline||'';
    get('session-dates').textContent='创建于 '+date(t.created)+' · 最后更新 '+date(t.updated);
    get('session-messages').replaceChildren(...(r.messages||[]).map(messageCard));
    if(!r.messages?.length)get('session-messages').append(node('p','empty-state','当前尚无可整理的会话过程。'));
    get('session-diagnosis').replaceChildren();
    const diagnosis=r.diagnosis;
    if(diagnosis && (diagnosis.summary||diagnosis.hint)){
      const box=node('section','session-diagnosis'+(diagnosis.historical?' session-diagnosis-history':''));
      box.append(node('h3','',diagnosis.historical?'历史问题与记录中的建议':'诊断与后续处理'));
      if(diagnosis.summary)box.append(node('p','',diagnosis.summary));
      if(diagnosis.hint)box.append(node('p','',diagnosis.hint));
      const refs=references(diagnosis.refs);if(refs)box.append(refs);
      get('session-diagnosis').append(box);
    }
    get('session-related').replaceChildren();
    if(r.related_tasks?.length){
      const related=node('section','session-related');
      related.append(node('h3','','同一条工作链上的相关任务'));
      for(const other of r.related_tasks){
        if(other.id===selectedId)continue;
        const a=node('a','',other.text||other.id);a.href=link(other.id);
        a.append(node('span','badge '+tone(other.state),stateLabel(other.state)));related.append(a);
      }
      get('session-related').append(related);
    }
    const query=new URLSearchParams({task_id:selectedId,format:'markdown'});
    get('session-markdown').href='/api/session?'+query;get('session-markdown').hidden=false;
    get('session-advanced').href='#review?'+new URLSearchParams({task:selectedId});
    get('session-coverage').hidden=false;
    get('session-coverage-text').textContent='根据 '+(coverage.archived_events??'未知数量的')+' 条归档事件整理，'+
      (coverage.collapsed_events??'未知数量的')+' 条细节已折叠。时间按浏览器当地时区显示。';
    get('session-coverage-notes').replaceChildren(...(coverage.notes||[]).map(n=>node('li','',n)));
    const identities=[['任务 ID',t.id||selectedId],['调用链 ID',r.scope?.trace_id||'未记录'],
      ['归档截止编号',coverage.through_seq??'未记录']];
    if(service?.binding)identities.push(['Session 插件绑定',service.binding]);
    fields(get('session-identities'),identities);
    const limitations=[];
    if(coverage.truncated)limitations.push('摘要涵盖部分已读取的证据。完整过程请继续查看执行时间线。');
    if(coverage.task_available===false)limitations.push('任务账本当前不可读，以下内容以可用归档为依据。');
    if(coverage.archived_events===0)limitations.push('此任务尚无可用归档，当前结果来自任务账本。');
    notice('session-notice',limitations.join(' '));
  }
  async function loadSession(background=false) {
    if(!selectedId)return;
    if(!background)clearReader();
    const ticket=++readerRequest,id=selectedId;
    readerBusy=true;get('session-reader').setAttribute('aria-busy','true');get('session-refresh').disabled=true;
    try{
      const r=await api('/api/session?'+new URLSearchParams({task_id:id}));
      if(ticket!==readerRequest || id!==selectedId)return;
      renderSession(r);
    }catch(e){
      if(ticket!==readerRequest)return;
      const pluginUnavailable=e.status===503 && typeof e.code==='string' && e.code.startsWith('session.');
      if(!background){
        get('session-state').className='badge warn';
        get('session-state').textContent=pluginUnavailable?'插件不可用':'读取失败';
        get('session-goal').textContent=e.status===404?'未找到这项任务':pluginUnavailable?'会话插件暂不可用':'任务会话暂不可读';
      }
      const message=pluginUnavailable?'会话插件不可用，任务和原始事件仍可查看。':'本次读取失败。';
      notice('session-notice',message+e.message+'。可点击刷新重试。'+(background?'当前正文为上次读取的会话。':''));
      get('session-markdown').hidden=true;
      get('session-fallback-review').href='#review?'+new URLSearchParams({task:id});
      get('session-fallback').hidden=false;
      if(e.observeOnly && !projection){
        try{
          const record=await api('/api/task-context?'+new URLSearchParams({task_id:id}));
          if(ticket!==readerRequest || id!==selectedId)return;
          const t=record.task;
          if(!t){
            get('session-goal').textContent=record.notes?.length?'任务记录暂不可读':'未找到这项任务';
            notice('session-notice',(record.notes||[]).join(' '));
            return;
          }
          get('session-goal').textContent=t.text||'未记录任务原文';
          get('session-state').textContent=stateLabel(t.state);
          get('session-state').className='badge '+tone(t.state);
          get('session-headline').textContent=t.detail||'';
          get('session-dates').textContent='创建于 '+date(t.created)+' · 最后更新 '+date(t.updated);
          notice('session-notice','当前显示只读任务记录。会话摘要暂不可用。');
        }catch(readError){
          if(ticket!==readerRequest || id!==selectedId)return;
          get('session-goal').textContent=readError.status===404?'未找到这项任务':'任务记录暂不可读';
          notice('session-notice',readError.message);
        }
      }
    }finally{
      if(ticket===readerRequest){readerBusy=false;get('session-reader').setAttribute('aria-busy','false');get('session-refresh').disabled=false;}
    }
  }
  function route(visible) {
    const wasActive=active;active=visible;
    if(!visible){listRequest++;readerRequest++;listBusy=readerBusy=false;return;}
    const requested=new URLSearchParams(location.hash.split('?')[1]).get('task');
    const changed=requested!==selectedId;
    selectedId=requested;
    if(!wasActive || !requested)loadList({chooseFirst:!requested});
    if(requested && (!wasActive || changed))loadSession();
    markSelected();
  }
  function refresh() {
    if(!active || Date.now()-lastRefresh<4000)return;
    lastRefresh=Date.now();
    if(!listBusy && cursors.length===1 && !get('session-list').contains(document.activeElement))loadList({background:true});
    if(!readerBusy && ongoing(projection?.task?.state) && !get('event-modal').open &&
      !get('session-reader').querySelector('details[open]') && !get('session-reader').contains(document.activeElement))loadSession(true);
  }
  document.querySelectorAll('[data-session-state]').forEach(button=>button.onclick=()=>{
    filter=button.dataset.sessionState;cursors=[null];next=null;selectedId=null;
    clearReader('正在读取此分类的任务…');
    document.querySelectorAll('[data-session-state]').forEach(b=>b.setAttribute('aria-pressed',String(b===button)));
    loadList({chooseFirst:true});
  });
  get('session-prev').onclick=()=>{
    if(cursors.length>1){cursors.pop();next=null;selectedId=null;clearReader('正在读取上一页任务…');loadList({chooseFirst:true});}
  };
  get('session-next').onclick=()=>{
    if(next!=null){cursors.push(next);next=null;selectedId=null;clearReader('正在读取下一页任务…');loadList({chooseFirst:true});}
  };
  get('session-refresh').onclick=()=>loadSession();
  get('session-switch-task').onclick=()=>{
    get('session-list').scrollIntoView({block:'start',behavior:'smooth'});
    get('session-list').querySelector('a')?.focus({preventScroll:true});
  };
  return {route,refresh,liveStatus};
})();
