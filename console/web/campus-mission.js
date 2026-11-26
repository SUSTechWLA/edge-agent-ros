/* Read-only campus projection. Waypoints, measured endpoints and current sensors have separate owners. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const set = (id,value) => { el(id).textContent=value; };
  const node = (tag,text) => { const e=document.createElement(tag); if(text!=null)e.textContent=String(text); return e; };
  const pair = p => Array.isArray(p) && p.length>=2 && p.slice(0,2).every(Number.isFinite);
  const fmt = (v,n=2) => Number.isFinite(v)?(Math.abs(v)<.5*10**(-n)?0:v).toFixed(n):'—';
  const coord = p => pair(p)?'('+p.slice(0,2).map(v=>fmt(v)).join(', ')+') m':'—';
  const clock = value => {const d=new Date(value);return value!=null && Number.isFinite(d.getTime())?d.toLocaleTimeString('zh-CN',{hour12:false}):'—';};
  const states={SUCCEEDED:'已完成',RUNNING:'执行中',PLANNING:'规划中',ACCEPTED:'已受理',PENDING:'待处理',WAITING:'等待中',QUEUED:'已排队',BLOCKED:'受阻',REJECTED:'已拒绝',FAILED:'失败',CANCELED:'已取消',RECOVERY_REQUIRED:'待核查'};
  const terminal=new Set(['SUCCEEDED','BLOCKED','REJECTED','FAILED','CANCELED','RECOVERY_REQUIRED']);
  const tools={navigate_place:'前往登记地点',plan_route:'计算路线',navigate:'导航',status:'读取机器人状态',inspect:'检查传感器'};
  const eventNames={'task.created':'提交任务','task.accepted':'任务已受理','plan.created':'生成计划','step.started':'开始步骤','step.verified':'核验步骤','adapter.request':'调用工具','adapter.response':'取得工具回执','task.succeeded':'任务已完成','session.query_requested':'查询任务记录','session.query_completed':'返回任务记录'};
  const healthVocabulary={fresh:['新鲜观测','good'],stale:['观测过期','warn'],none:['等待数据',''],offline:['连接中断','warn']};
  let live=null, online=false, rows=[], chosen=new URLSearchParams(location.search).get('task_id')?.slice(0,256)||'', task=null, taskEvents=[], detailsError='', eventError='', debug=false;
  let viewConfig={}, viewsFetched=0, listFetched=0, detailFetched=0, detailKey=0, timer, listSignature='', disposed=false;
  let routeEvents=[], localPreview=null, local3d=false, activeSeen=null, pinnedTask=Boolean(chosen);
  function chip(id,text,tone=''){set(id,text);el(id).dataset.tone=tone;}
  async function read(path){
    const r=await fetch(path,{cache:'no-store',credentials:'same-origin',signal:AbortSignal.timeout(4500)});
    if(!r.ok){const error=new Error('HTTP '+r.status);error.status=r.status;throw error;}
    return r.json();
  }
  function inputStatus(source){
    if(!online)return 'offline';
    if(source?.fresh===true && source?.available===true)return 'fresh';
    if(source?.data || source?.available || source?.status==='stale')return 'stale';
    return 'none';
  }
  function dataHealth(id,s){chip(id,...healthVocabulary[inputStatus(s)]);}
  function sourceText(s){return [s?.topic,s?.data?.frame||s?.data?.frame_id,Number.isFinite(s?.age_s)?'样本年龄 '+fmt(s.age_s,1)+' s':''].filter(Boolean).join(' · ')||'暂无采样';}
  function eventsForTask(list){return (Array.isArray(list)?list:[]).filter(e=>e && (e.task_id===chosen || (!e.task_id && e.trace_id===chosen)));}
  function infoList(id,entries){el(id).replaceChildren(...entries.map(([key,value])=>{const row=node('div');row.append(node('dt',key),node('dd',value));return row;}));}
  function stepEvidence(){
    return task?.steps?.map(s=>s?.evidence).filter(e=>e && typeof e==='object')||[];
  }
  function matchesScene(ev,sem){
    return !!(sem?.id && sem.frame && sem.revision && ev?.scene===sem.id && ev.frame===sem.frame &&
      ev.revision===sem.revision && (!sem.digest || ev.digest===sem.digest));
  }
  const position = p => Array.isArray(p) && [2,3].includes(p.length) && p.every(Number.isFinite);
  function globalPlan(){
    const sem=live?.sources?.semantics?.data, op=live?.active_operation;
    const owner=typeof op==='string'?op.replace(/\/waypoint\/\d+$/,''):null;
    const records=eventsForTask(routeEvents);
    const attempts=owner ? records.filter(e=>e.operation_id===owner):records;
    const routes=[...(attempts.length?attempts:records).map(e=>({...e.data,mission_id:e.operation_id})),...stepEvidence()];
    return routes.find(e=>matchesScene(e,sem) && Array.isArray(e.waypoints) && e.waypoints.length &&
      e.waypoints.every(p=>position(p)&&p.length===e.waypoints[0].length))||null;
  }
  function routeWindow(plan,horizon=80){
    const points=plan.waypoints;
    const leg=/^(.+)\/waypoint\/(\d+)$/.exec(live?.active_operation||'');
    const index=leg&&leg[1]===plan.mission_id?Math.max(0,Math.min(points.length-1,Number(leg[2])-2)):0;
    const total=points.reduce((acc,p,i)=>i?acc+Math.hypot(...p.map((v,j)=>v-points[i-1][j])):0,0);
    const head=total>horizon||points.length>120?index:0;
    const shown=[points[head]];
    let remaining=horizon;
    for(let i=head+1;i<points.length&&shown.length<120;i++){
      const a=points[i-1], b=points[i], delta=b.map((v,j)=>v-a[j]), length=Math.hypot(...delta);
      if(length>remaining){shown.push(a.map((v,j)=>v+delta[j]*remaining/length));return {points:shown,truncated:true};}
      shown.push(b);remaining-=length;
      if(remaining<=0)break;
    }
    return {points:shown,truncated:head>0||shown.length<points.length};
  }
  function currentLocal(){
    const l=localPreview, sem=live?.sources?.semantics?.data, pose=robotPose();
    if(online && l?.status==='planned' && l.task_id===chosen && l.operation_id===live?.active_operation &&
      l.scene_id===sem?.id && l.scene_revision===sem?.revision && l.frame===sem?.frame &&
      inputStatus(live?.sources?.map)==='fresh' && inputStatus(pose)==='fresh' &&
      Number.isFinite(l.source_stamp_s)&&Number.isFinite(pose.stamp_s)&&Math.abs(pose.stamp_s-l.source_stamp_s)<=2 &&
      l.motion_authorized===false && Array.isArray(l.points) && l.points.length>1 && l.points.every(position))return l;
    return null;
  }
  async function fetchLocal(){
    const id=chosen, op=live?.active_operation;
    if(!online||!id||!op){localPreview=null;return;}
    try{
      const result=await read('/api/campus-mission/local-preview?'+new URLSearchParams({task_id:id}));
      if(id===chosen&&op===live?.active_operation)localPreview=result;
    }catch{if(id===chosen)localPreview={status:'unavailable'};}
  }
  function reachedWitness(step){
    const mission=step?.evidence?.mission_id && live?.missions?.find(m=>m.mission_id===step.evidence.mission_id && m.mission_id===step.operation_id);
    if(!mission)return null;
    return mission.steps?.filter(s=>Number.isFinite(s.evidence_x) && Number.isFinite(s.evidence_y) &&
      s.odom_stamp_ns>0 && typeof s.action_result==='string' && /stationary odometry confirmed/i.test(s.action_result)).at(-1);
  }
  function robotPose(){
    const s=live?.sources||{};
    return inputStatus(s.pose)==='fresh' || (inputStatus(s.odom)!=='fresh' && s.pose?.data)?s.pose:s.odom;
  }
  function timestamp(value){return typeof value==='number'?clock(value*1000):clock(value);}
  function displayTaskState(record){
    const state=record?.state;
    if(state!=='WAITING'||record.id!==chosen||!online)return state;
    const op=/^(.+)\/waypoint\/\d+$/.exec(live?.active_operation||'');
    const scene=live?.sources?.semantics?.data;
    const running=op&&eventsForTask(routeEvents).some(e=>e.operation_id===op[1]&&matchesScene(e.data,scene));
    return running?'RUNNING':state;
  }
  function taskSummary(){
    const sel=rows.find(r=>r.id===chosen), current=task||sel;
    const state=displayTaskState(current);
    set('task-text',current?.text||(!chosen?'暂无任务':'正在读取任务详情'));
    chip('task-status',states[state]||state||(chosen?'等待读取':'暂无任务'),state==='SUCCEEDED'?'good':terminal.has(state)?'warn':'');
    const steps=Array.isArray(task?.steps)?task.steps:[];
    const pending=steps.find(s=>s?.status && s.status!=='SUCCEEDED');
    const observed=stepEvidence().find(e=>pair(e.observed_xy));
    const display=detailsError || (state==='SUCCEEDED' && observed?'任务记录的终点 '+coord(observed.observed_xy):
      pending && !terminal.has(state)?'当前步骤 · '+(tools[pending.tool]||pending.tool||pending.status):
      current?.detail||(!chosen?'等待校园任务记录':'正在取得步骤回执'));
    set('task-progress',display);
    set('step-count',task?steps.length+' 个步骤 · '+(current?.updated?'记录更新于 '+timestamp(current.updated):'已取得任务记录'):'暂无步骤详情');
    const items=[];
    if(current?.text){const li=node('li');li.append(node('strong','提交自然语言任务'),node('small',current.created?timestamp(current.created):'已登记任务原文'));items.push(li);}
    steps.forEach((s,i)=>{
      const li=node('li'), evidence=s?.evidence||{}, name=tools[s?.tool]||s?.tool||s?.capability||(typeof s==='string'?s:'步骤 '+(i+1));
      li.append(node('strong',name+' · '+(states[s?.status]||s?.status||'已列入计划')));
      const lines=[s?.detail,evidence.place,Number.isFinite(evidence.distance_m)?'路线 '+fmt(evidence.distance_m)+' m':'',
        Number.isInteger(evidence.verified_count)?'已核验 '+evidence.verified_count+' 个航点':''].filter(Boolean);
      if(lines.length)li.append(node('small',lines.join(' · ')));
      const stop=reachedWitness(s);
      if(stop)li.append(node('small','停稳见证 · 里程计 '+coord([stop.evidence_x,stop.evidence_y])));
      items.push(li);
    });
    if(!items.length)items.push(node('li','暂无可核验的步骤'));
    el('steps').replaceChildren(...items);
    const events=eventsForTask(taskEvents);
    el('events').replaceChildren(...(events.length?events.slice(0,12).map(e=>{
      const li=node('li');li.append(node('strong',eventNames[e.kind]||e.kind||'任务事件'),node('small',[timestamp(e.utc||e.timestamp),e.source||e.module,e.detail!==e.kind?e.detail:''].filter(Boolean).join(' · ')));return li;
    }):[node('li',eventError||'暂无此任务的事件')]));
  }
  function robotFacts(){
    const s=live?.sources||{}, pose=robotPose(), info=pose?.data||{};
    const xy=[info.x,info.y], ps=inputStatus(pose), type=healthVocabulary[ps][0];
    const odom=s.odom, o=odom?.data||{}, freshOdom=inputStatus(odom)==='fresh';
    const v=freshOdom && Number.isFinite(o.v)?o.v:null;
    const omega=freshOdom && Number.isFinite(o.w)?o.w:null;
    const motion=v===null?'等待新鲜里程计':Math.abs(v)>.01 || (omega!==null && Math.abs(omega)>.02)?'运动中':omega!==null?'静止':'线速度接近零';
    set('motion-summary',v===null?motion:motion+' · '+fmt(v,3)+' m/s');
    const op=inputStatus(s.operation)==='fresh'?s.operation?.data:null;
    set('robot-name',live?.robot_id||'—');
    infoList('robot-facts', [['位姿',type],['平面位置',ps==='fresh'?coord(xy):'—'],
      ['航向',ps==='fresh'&&Number.isFinite(info.yaw)?fmt(info.yaw*180/Math.PI,1)+'°':'—'],
      ['线速度',v===null?'—':fmt(v,3)+' m/s'],['运动状态',motion],['活动操作',op?.operation_id||op?.id||op?.state||'暂无新鲜操作记录'],
      ['IMU',healthVocabulary[inputStatus(s.imu)][0]],
      ['激光',healthVocabulary[inputStatus(s.scan)][0]],['环境',({simulation:'Gazebo 仿真',hardware:'实机'})[live?.environment]||'待确认']]);
    set('position-info',sourceText(pose));
    dataHealth('scan-status',s.scan);dataHealth('grid-status',s.map);
    set('scan-info',sourceText(s.scan));set('grid-info',sourceText(s.map));
  }
  function diagnostics(){
    if(!debug)return;
    const sections=[
      ['任务与工具绑定',task||rows.find(r=>r.id===chosen)||null],
      ['ROS 观测来源与时间',Object.fromEntries(Object.entries(live?.sources||{}).map(([k,v])=>[k,{topic:v?.topic,frame:v?.data?.frame,fresh:v?.fresh,available:v?.available,stamp_s:v?.stamp_s,age_s:v?.age_s,ttl_s:v?.ttl_s}]))],
      ['选中任务原始事件',eventsForTask(taskEvents)],['地图查看器',viewConfig]
    ];
    const container=el('diagnostics');
    for(const [label,value] of sections){
      let fold=Array.from(container.children).find(e=>e.dataset.label===label);
      if(!fold){fold=node('details');fold.dataset.label=label;fold.append(node('summary',label),node('pre'));container.append(fold);}
      fold.querySelector('pre').textContent=JSON.stringify(value,null,2);
    }
  }
  function surface(id){
    const c=el(id), rect=c.getBoundingClientRect(), w=rect.width,h=rect.height,dpr=Math.min(window.devicePixelRatio||1,2);
    if(!w||!h)return null;
    if(c.width!==Math.round(w*dpr)||c.height!==Math.round(h*dpr)){c.width=Math.round(w*dpr);c.height=Math.round(h*dpr);}
    const ctx=c.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    return {c,ctx,w,h};
  }
  function label(ctx,t,x,y,color){ctx.font='12px "Microsoft YaHei", sans-serif';ctx.fillStyle=color;ctx.fillText(t,x,y);}
  function line(ctx,pts,color,width=1){
    if(pts.length<2)return;
    ctx.strokeStyle=color;ctx.lineWidth=width;ctx.lineCap='round';ctx.lineJoin='round';ctx.beginPath();pts.forEach(([x,y],i)=>i?ctx.lineTo(x,y):ctx.moveTo(x,y));ctx.stroke();
  }
  function circle(ctx,x,y,r,color){ctx.fillStyle=color;ctx.beginPath();ctx.arc(x,y,r,0,2*Math.PI);ctx.fill();}
  function empty(id,text){set(id,text);el(id).hidden=!text;}
  function topology(){
    const s=surface('route-map');if(!s)return;
    const {c,ctx,w,h}=s, sem=live?.sources?.semantics?.data;
    const nodes=Array.isArray(sem?.nodes)?sem.nodes.filter(n=>pair(n?.xy)):[];
    const ev=globalPlan(), stop=stepEvidence().find(e=>matchesScene(e,sem)&&pair(e.observed_xy));
    const visible=ev?routeWindow(ev):{points:[],truncated:false};
    const points=visible.points, measured=stop?.observed_xy, local=currentLocal()?.points||[];
    const pose=robotPose(), current=pose?.data;
    const here=inputStatus(pose)==='fresh' && current?.frame===sem?.frame && pair([current?.x,current?.y])?[current.x,current.y]:null;
    c.dataset.routePoints=String(points.length);c.dataset.locations=String(nodes.length);c.dataset.observedEnd=String(!!measured);
    c.dataset.localRoutePoints=String(local.length);
    c.dataset.robotPosition=nodes.length&&here?JSON.stringify(here):'';
    set('route-frame',sem?.frame?sem.frame+' · m':'米');
    set('route-note',!nodes.length?'等待已登记的语义地点':points.length?(visible.truncated?'当前路线窗口 · ':'全局路线已核对 · ')+points.length+' 个路点':
      '地点拓扑 · 当前任务暂无与场景同版本的路线');
    empty('route-empty',nodes.length?'':'暂无语义场景');
    if(!nodes.length)return;
    const all=[...nodes.map(n=>n.xy),...points,...local,...(measured?[measured]:[]),...(here?[here]:[])], xs=all.map(p=>p[0]),ys=all.map(p=>p[1]);
    const ax=Math.min(...xs),bx=Math.max(...xs),ay=Math.min(...ys),by=Math.max(...ys);
    const scale=Math.min(Math.max(1,w-90)/Math.max(2,bx-ax+.4),Math.max(1,h-74)/Math.max(2,by-ay+.4));
    const project=([x,y])=>[w/2+(x-(ax+bx)/2)*scale,h/2-(y-(ay+by)/2)*scale];
    const spacing=Math.pow(10,Math.ceil(Math.log10(26/Math.max(scale,.001))))*scale;
    ctx.strokeStyle='#29423c';ctx.lineWidth=1;ctx.beginPath();
    for(let x=project([0,0])[0]%spacing;x<w;x+=spacing){ctx.moveTo(x,0);ctx.lineTo(x,h);}
    for(let y=project([0,0])[1]%spacing;y<h;y+=spacing){ctx.moveTo(0,y);ctx.lineTo(w,y);}ctx.stroke();
    for(const z of (sem.zones||[])){
      const b=z.bounds;if(!Array.isArray(b)||b.length!==4||!b.every(Number.isFinite))continue;
      const [x,y]=project([b[0],b[3]]),[x1,y1]=project([b[2],b[1]]);ctx.fillStyle=z.kind==='indoor'?'#7eaaea18':'#a1d38c15';ctx.fillRect(x,y,x1-x,y1-y);
    }
    if(!points.length){
      const byId=new Map(nodes.map(n=>[n.id,n]));
      ctx.setLineDash([5,6]);for(const edge of sem.edges||[]){const [a,b]=Array.isArray(edge)?edge:[edge.from,edge.to];if(byId.has(a)&&byId.has(b))line(ctx,[project(byId.get(a).xy),project(byId.get(b).xy)],'#9cb4aa',2);}ctx.setLineDash([]);
    }
    line(ctx,points.map(project),'#a4f1bf',5);
    line(ctx,local.map(project),'#ff9b54',4);
    nodes.forEach((n,i)=>{
      const [x,y]=project(n.xy);circle(ctx,x,y,5,'#d9e8de');circle(ctx,x,y,2,'#365f52');
      const name=n.label||n.id;ctx.font='12px "Microsoft YaHei", sans-serif';const width=ctx.measureText(name).width;
      label(ctx,name,Math.max(6,Math.min(w-width-6,x-width/2)),y+(i%2?25:-15),'#e5f4e9');
    });
    if(measured){const [x,y]=project(measured);circle(ctx,x,y,6,'#f4bb72');circle(ctx,x,y,2,'#293f37');}
    if(here){
      const [x,y]=project(here);
      ctx.strokeStyle='#81d8fa';ctx.lineWidth=2;ctx.beginPath();ctx.arc(x,y,10,0,Math.PI*2);ctx.stroke();
      if(Number.isFinite(current.yaw)){
        const dx=Math.cos(current.yaw),dy=-Math.sin(current.yaw);
        line(ctx,[[x+10*dx,y+10*dy],[x+19*dx,y+19*dy]],'#81d8fa',2);
      }
    }
    label(ctx,'X →   Y ↑',12,h-12,'#9db8aa');
  }
  function scan(){
    const s=surface('scan-map');if(!s)return;
    const {ctx,w,h}=s,d=live?.sources?.scan?.data, ok=Array.isArray(d?.ranges)&&Number.isFinite(d.angle_min)&&Number.isFinite(d.angle_increment)&&d.range_max>0;
    empty('scan-empty',ok?'':'等待激光扫描');if(!ok)return;
    const r=Math.max(1,Math.min(w,h)/2-12), x=w/2,y=h/2;
    ctx.strokeStyle='#35534a';for(let i=1;i<=3;i++){ctx.beginPath();ctx.arc(x,y,r*i/3,0,Math.PI*2);ctx.stroke();}
    line(ctx,[[x,6],[x,h-6]],'#47645a');line(ctx,[[x-r,y],[x+r,y]],'#47645a');circle(ctx,x,y,3,'#ffc274');
    let hits=0;d.ranges.forEach((v,i)=>{if(!Number.isFinite(v)||v<d.range_min||v>=d.range_max)return;const a=d.angle_min+i*d.angle_increment;circle(ctx,x-Math.sin(a)*v*r/d.range_max,y-Math.cos(a)*v*r/d.range_max,1.6,'#9cedcb');hits++;});
    label(ctx,hits+' 个回波',9,h-9,'#bed9ca');
  }
  function grid(){
    const local=currentLocal(), status=localPreview?.status, active=live?.active_operation;
    const reason={'planning.no_active_waypoint':'等待活动航点','plugin.service_missing':'局部规划插件未安装',
      'planning.observation_unsynchronized':'栅格与位姿暂未对齐','start_footprint_unknown':'车体附近观测不足',
      'target_footprint_unknown':'前视区观测不足','local_path_unreachable':'前视区暂时无通路'};
    set('local-plan-info',local3d?'XYZ 点云视图 · 当前轮式候选沿地面显示':local?'橙色 · 实时局部规划建议':
      active?(reason[localPreview?.code]||(status==='blocked'?'暂时无可用局部路径':'等待局部规划')):'暂无正在执行的航点');
    const s=surface('grid-map');if(!s)return;
    const {c,ctx,w,h}=s,g=live?.sources?.map?.data,valid=Number.isInteger(g?.width)&&Number.isInteger(g.height)&&g.width>0&&g.height>0&&g.width*g.height<=2097152&&Array.isArray(g.cells)&&g.cells.length===g.width*g.height;
    c.dataset.localRoutePoints=String(local?.points.length||0);
    empty('grid-empty',valid?'':'等待局部占用栅格');if(!valid)return;
    const tile=document.createElement('canvas');tile.width=g.width;tile.height=g.height;
    const tc=tile.getContext('2d'),im=tc.createImageData(g.width,g.height);
    g.cells.forEach((v,i)=>{const base=((g.height-1-Math.floor(i/g.width))*g.width+i%g.width)*4;const f=Math.min(1,Math.max(0,v/100));const rgb=!Number.isFinite(v)||v<0?[41,61,55]:[62+172*f,103+124*f,91+116*f];im.data.set([...rgb,255],base);});
    tc.putImageData(im,0,0);ctx.imageSmoothingEnabled=false;
    const pose=robotPose()?.data, origin=[g.origin_x,g.origin_y,g.origin_yaw];
    const matched=inputStatus(robotPose())==='fresh' && pose?.frame===g.frame && origin.every(Number.isFinite) &&
      Number.isFinite(g.resolution)&&g.resolution>0&&pair([pose.x,pose.y]);
    const cos=Math.cos(g.origin_yaw),sin=Math.sin(g.origin_yaw);
    const cells=([x,y])=>[(cos*(x-g.origin_x)+sin*(y-g.origin_y))/g.resolution,
      g.height-(-sin*(x-g.origin_x)+cos*(y-g.origin_y))/g.resolution];
    const center=matched?cells([pose.x,pose.y]):[g.width/2,g.height/2];
    const scale=matched?Math.min(w,h)*g.resolution/4:Math.min((w-18)/g.width,(h-16)/g.height);
    const left=w/2-center[0]*scale, top=h/2-center[1]*scale;
    ctx.drawImage(tile,left,top,g.width*scale,g.height*scale);
    if(matched){
      const project=p=>{const [x,y]=cells(p);return [left+x*scale,top+y*scale];};
      if(local?.frame===g.frame)line(ctx,local.points.map(project),'#ff9b54',4);
      circle(ctx,w/2,h/2,4,'#81d8fa');
    }
    set('grid-info',sourceText(live.sources.map)+' · '+g.width+' × '+g.height+' · '+fmt(g.resolution,3)+' m/格');
  }
  function render(){taskSummary();fillChoices();robotFacts();topology();scan();grid();shareRoutes();diagnostics();}
  function fillChoices(){
    const opts=[...rows];if(chosen&&!opts.some(v=>v.id===chosen))opts.unshift({id:chosen,text:task?.text||chosen});
    const entries=opts.map(t=>[t.id,t.text,displayTaskState(t.id===chosen&&task?task:t)]);
    const sig=JSON.stringify(entries);if(sig!==listSignature){const select=el('task-choice');select.replaceChildren(...entries.map(([id,label,state])=>new Option(label+' · '+(states[state]||state||'读取中'),id)));if(!entries.length)select.add(new Option('暂无任务',''));listSignature=sig;}
    el('task-choice').value=chosen;
  }
  function selectTask(id){
    chosen=id;task=null;taskEvents=[];routeEvents=[];localPreview=null;detailsError='';eventError='';detailFetched=0;detailKey++;
    mapFrames();render();
  }
  async function getTask(){
    const id=chosen, key=++detailKey;if(!id)return;
    const query=new URLSearchParams({id,limit:'60'}), context=new URLSearchParams({task_id:id,limit:'60'});
    const load=async()=>{try{return await read('/api/task-detail?'+query);}catch(error){if(error.status!==404)throw error;const r=await read('/api/task-context?'+context);if(!r.task)throw Error('任务详情不可用');return r.task;}};
    const [d,events,routes]=await Promise.allSettled([load(),read('/api/events?'+context),
      read('/api/events?'+new URLSearchParams({task_id:id,limit:'16',kind:'scene.route_planned'}))]);
    if(key!==detailKey||id!==chosen||disposed)return;
    if(d.status==='fulfilled'&&d.value?.id===id){task=d.value;detailsError='';}else detailsError='任务详情暂不可用，保留已读取记录';
    if(events.status==='fulfilled'){taskEvents=events.value.events;eventError='';}else{eventError='事件服务暂不可用';}
    if(routes.status==='fulfilled')routeEvents=routes.value.events;
    detailFetched=Date.now();render();
  }
  function localView(value){
    if(typeof value!=='string'||!value)return null;
    try{const u=new URL(value,location.href),loop=h=>['localhost','127.0.0.1','[::1]'].includes(h);
      if(u.username||u.password||!['http:','https:'].includes(u.protocol)||(u.hostname!==location.hostname && !(loop(u.hostname)&&loop(location.hostname))))return null;
      if(loop(u.hostname)&&loop(location.hostname))u.hostname=location.hostname;
      return u.href;
    }catch{return null;}
  }
  function embed(prefix,href,title){
    const host=el(prefix+'-host'), a=el(prefix+'-link');
    const url=href?new URL(href):null;
    if(url&&chosen)url.searchParams.set('task',chosen);
    if(url&&prefix==='local3')url.searchParams.set('local','1');
    if(a && url){a.href=url.href;a.onclick=e=>{
      if(host.requestFullscreen&&!e.ctrlKey&&!e.metaKey&&!e.shiftKey){e.preventDefault();host.requestFullscreen().catch(()=>location.assign(a.href));}
    };}
    if(url)url.searchParams.set('embed','1');
    const key=url?.href||'unavailable';
    if(host.dataset.href===key)return;
    host.dataset.href=key;
    if(!url){host.replaceChildren(node('span','此站未提供该地图查看器'));a?.removeAttribute('href');return;}
    const f=node('iframe');f.title=title;f.loading='lazy';f.setAttribute('sandbox','allow-scripts allow-same-origin');f.referrerPolicy='no-referrer';
    f.addEventListener('load',shareRoutes);
    f.src=url.href;host.replaceChildren(f);
  }
  function mapFrames(){
    embed('map2',localView(viewConfig.campus_2d),'二维校园地图');
    embed('map3',localView(viewConfig.campus_3d),'三维校园地图');
    if(local3d)embed('local3',localView(viewConfig.campus_3d),'局部三维点云');
    el('local3-link').hidden=el('local3-hint').hidden=!local3d||!el('local3-link').hasAttribute('href');
  }
  function shareRoutes(){
    const g=globalPlan(), l=currentLocal();
    const global=g?{scene_id:g.scene,scene_revision:g.revision,frame:g.frame,points:routeWindow(g).points}:null;
    const local=l?{scene_id:l.scene_id,scene_revision:l.scene_revision,frame:l.frame,points:l.points}:null;
    for(const key of ['map2','map3','local3']){
      const f=el(key+'-host').querySelector('iframe');
      if(f&&chosen)f.contentWindow?.postMessage({type:'edge.campus.task-routes',version:1,task_id:chosen,
        global:key==='local3'?null:global,local},new URL(f.src).origin);
    }
  }
  async function views(){
    let v={};try{v=await read('/api/campus-mission/views');}catch(e){
      if(e.status===404&&location.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(location.hostname)){
        const root='http://'+location.hostname;v={campus_2d:root+':8773/?view=2d',campus_3d:root+':8773/?view=3d&place=campus',overview:root+':8780/'};
      }
    }
    if(disposed)return;viewConfig=v||{};viewsFetched=Date.now();
    mapFrames();
    const links=[];for(const [key,title] of [['overview','Gazebo 场景总览 ↗'],['recording','运行复盘 ↗']]){const u=localView(viewConfig[key]);if(u){const a=node('a',title);a.href=u;a.rel='noopener';a.target='_blank';links.push(a);}}el('view-links').replaceChildren(...links);diagnostics();
  }
  async function refresh(){
    if(disposed)return;
    if(!document.hidden){
      const listDue=Date.now()-listFetched>3500, viewDue=Date.now()-viewsFetched>60000;
      const [state,list]=await Promise.allSettled([read('/api/state'),listDue?read('/api/review/tasks?limit=50'):Promise.resolve(null),viewDue?views():Promise.resolve()]);
      if(disposed)return;
      if(state.status==='fulfilled'&&state.value?.sources){
        live=state.value;online=true;
        const dynamic=[live.sources.scan,live.sources.imu,live.sources.odom].map(inputStatus);
        const fresh=dynamic.filter(h=>h==='fresh').length;
        const signal=fresh===dynamic.length?['实时观测更新','good']:fresh?['部分观测更新','warn']:
          dynamic.includes('stale')?['实时观测过期','warn']:['等待实时观测',''];
        chip('connection',...signal);
        set('freshness',[live.environment==='simulation'?'Gazebo 仿真':'现场观测',live.robot_id,'状态更新 '+timestamp(live.now)].filter(Boolean).join(' · '));
      }
      else{online=false;chip('connection','连接中断','warn');set('freshness','无法取得当前状态 · '+(live?'显示上次取得的数据':'等待重连'));}
      if(listDue&&list.status==='fulfilled'&&Array.isArray(list.value?.tasks)){
        listFetched=Date.now();rows=list.value.tasks;
        if(!pinnedTask){const pick=rows.find(r=>r.state&&!terminal.has(r.state))||rows.find(r=>r.state==='SUCCEEDED')||rows[0];
          if(pick&&pick.id!==chosen)selectTask(pick.id);}
      }
      const operation=live?.active_operation||null;
      render();
      const updates=[fetchLocal()];
      if(chosen && (operation!==activeSeen||Date.now()-detailFetched>3000))updates.push(getTask());
      activeSeen=operation;
      await Promise.all(updates);if(!disposed)render();
    }
    if(!disposed)timer=setTimeout(refresh,1100);
  }
  el('task-choice').onchange=()=>{pinnedTask=true;selectTask(el('task-choice').value);const u=new URL(location.href);u.searchParams.set('task_id',chosen);history.replaceState(null,'',u);getTask();};
  for(const [id,is3d] of [['local-2d',false],['local-3d',true]])el(id).onclick=()=>{
    local3d=is3d;el('local-2d').setAttribute('aria-pressed',String(!is3d));el('local-3d').setAttribute('aria-pressed',String(is3d));
    el('grid-wrap').hidden=is3d;el('local3-host').hidden=!is3d;mapFrames();grid();shareRoutes();
  };
  window.addEventListener('message',event=>{
    if(event.data?.type!=='edge.campus.map-ready'||event.data.version!==1||event.data.task_id!==chosen)return;
    for(const key of ['map2','map3','local3']){
      const f=el(key+'-host').querySelector('iframe');
      if(f&&event.source===f.contentWindow&&event.origin===new URL(f.src).origin){shareRoutes();break;}
    }
  });
  for(const [id,val] of [['operator',false],['developer',true]])el(id).onclick=()=>{debug=val;el('operator').setAttribute('aria-pressed',String(!val));el('developer').setAttribute('aria-pressed',String(val));el('debug').hidden=!val;diagnostics();if(val)el('debug').scrollIntoView({block:'nearest'});};
  el('copy-example').onclick=async()=>{try{await navigator.clipboard.writeText('前往教学楼门外');set('copy-note','已复制「前往教学楼门外」');}catch{set('copy-note','任务示例 · 前往教学楼门外');}};
  const resize=new ResizeObserver(()=>{topology();scan();grid();});['route-map','scan-map','grid-map'].forEach(id=>resize.observe(el(id)));
  window.addEventListener('pagehide',e=>{if(!e.persisted){disposed=true;clearTimeout(timer);resize.disconnect();}});
  refresh();
})();
