'use strict';
(() => {
  const el = (id) => document.getElementById(id);
  const set = (id, value) => { el(id).textContent = value; };
  const finite = Number.isFinite;
  const fmt = (n, digits=2) => finite(n) ? n.toFixed(digits) : '—';
  const meters = (n) => finite(n) ? fmt(n, 3) + ' m' : '—';
  const ended = new Set(['SUCCEEDED','FAILED','BLOCKED','REFUSED','CANCELED','CANCELLED','RECOVERY_REQUIRED']);
  const labels = {PLANNING:'规划中',RUNNING:'运行中',WAITING:'等待回执',VERIFYING:'核验中',SUCCEEDED:'任务完成',
    FAILED:'失败',BLOCKED:'受阻',REFUSED:'已拒绝',CANCELED:'已取消',CANCELLED:'已取消',RECOVERY_REQUIRED:'需要对账'};
  const milestones = {accepted:'Agent 受理任务', 'planner.context':'读取可用能力目录', 'model.request':'请求模型选择能力',
    'model.response':'收到模型回复', planned:'计划已校验', dispatch:'派发已选工具', 'tool.request':'开始工具调用',
    'tool.response':'收到工具回执', 'tool.error':'工具报告错误', verified:'步骤核验完成', blocked:'任务受阻'};
  const decisions={planned:'已生成局部建议路径',partial:'几何绕行候选',blocked:'建议保持原位'};
  const reasons={current_footprint_below_margin:'当前车身与实测表面的间距低于规划裕度',no_observed_bypass:'未找到符合净距要求的通道',
    no_surface_or_ray_evidence:'没有可验证的表面或射线观测',point_budget_exceeded:'观测点数量超出规划器预算',
    observed_local_preview:'候选路径位于已观测区域',candidate_with_unknown_coverage:'部分通道覆盖待确认'};
  let current=null, evidence=null, waiting=false, began=0, requestId='', lastEvents='';
  let poseWindow=null, terminalAt=0;
  let cameraURL='', pendingCamera=false;
  let mode='field', fieldScene=null, fieldRef='', fieldIssue='', sceneRequest=0, mesh=null, example=null;
  const query=new URLSearchParams(location.search);
  const renderer=FieldScene.create(el('map'),{onChange(s){
    for(const [id,view] of [['view-orbit','orbit'],['view-top','top']])el(id).setAttribute('aria-pressed',String(s.view===view));
  }});
  const digest=v=>typeof v==='string'&&/^sha256:[0-9a-f]{64}$/.test(v);
  function localTime(ns){
    if(!/^\d{10,19}$/.test(String(ns)))return '';
    return new Date(Number(BigInt(ns)/1000000n)).toLocaleTimeString('zh-CN',{hour12:false});
  }
  function taskCamera(task,plan) {
    const frame=plan?.source_evidence?.camera||task?.source_evidence?.camera;
    if(!frame||typeof frame.ref!=='string'||!/^sha256:[0-9a-f]{64}$/.test(frame.ref))return null;
    const url='/camera/'+frame.ref.slice(7)+'.png';
    return frame.url===url ? frame:null;
  }
  async function camera(){
    if(pendingCamera||mode!=='field')return;
    const frame=taskCamera(current,evidence);
    const finished=current&&ended.has(current.state);
    if(finished&&!frame){
      cameraURL='';el('rgb').hidden=true;el('rgb-empty').hidden=false;set('rgb-empty','本次任务没有冻结的彩色帧');set('rgb-source','无任务相机回执');return;
    }
    if(frame&&cameraURL===frame.url)return;
    pendingCamera=true;
    const url=frame?.url||'/camera/latest.png?t='+Date.now();
    const pic=new Image();pic.src=url;
    try{await pic.decode();
      if(mode!=='field'||(current&&ended.has(current.state)&&taskCamera(current,evidence)?.url!==frame?.url))return;
      el('rgb').src=url;el('rgb').hidden=false;el('rgb-empty').hidden=true;cameraURL=url;
      set('rgb-source',frame?('任务采样 · '+(frame.stamp_ns||frame.ref.slice(7,19))):'实时预览 · 独立彩色观测');
    }catch{if(mode!=='field')return;el('rgb').hidden=true;el('rgb-empty').hidden=false;set('rgb-empty','彩色帧暂不可用');set('rgb-source',frame?'任务采样读取失败':'D435 实时源暂无画面');}
    finally{pendingCamera=false;}
  }
  function alertUser(value) { el('error').hidden=!value; set('error',value || ''); }
  async function api(path, data) {
    const r=await fetch(path,{method:data?'POST':'GET',credentials:'same-origin',cache:'no-store',
      headers:data?{'Content-Type':'application/json'}:{},body:data?JSON.stringify(data):undefined,signal:AbortSignal.timeout(12000)});
    if(!r.ok){let msg=''; try {const b=await r.json();msg=b.detail||b.error||'';}catch{}
      throw Error('HTTP '+r.status+(msg?' · '+String(msg).slice(0,160):''));}
    return r.json();
  }
  function connected(ok) {
    set('connection',ok?'Agent 已连接':'Agent 暂未连接');
    el('connection').className='connection '+(ok?'online':'offline');
  }
  async function checkConnection(){try{await api('/agent/capabilities');connected(true);}catch{connected(false);}}
  function planEvidence(task) {
    for (const step of Array.isArray(task.steps)?task.steps:[]) {
      const e=step.evidence;
      for (const p of [e,e?.result,e?.output,e?.plan]) {
        if(p && p.motion_authorized===false && Array.isArray(p.path_body_m) && p.status) return p;
      }
    }
    return null;
  }
  function metric(label,key,value){
    set(label+'-label',key);set(label,value);
  }
  function empty(message){
    set('map-empty',message);el('map-empty').hidden=!message;
  }
  function showPlan(scene,plan){
    renderer.setData(scene,plan);
    el('map').dataset.ready=scene?'true':'false';
    set('route-label',plan?.status==='blocked'?'停驻标记':plan?.status==='partial'?'候选轨迹':'规划轨迹');
    metric('range','车头到前方表面',meters(plan?.front_clearance_to_surface_m));
    const partial=plan?.status==='partial'&&finite(plan.coverage_fraction_on_path);
    metric('clearance',partial?'候选路径观测覆盖':'车身到可见障碍',partial?fmt(100*plan.coverage_fraction_on_path,1)+'%':meters(plan?.present_min_clearance_m));
    metric('coverage','要求的车身净距',meters(plan?.margin_m));
  }
  function fieldMap(plan){
    const ref=plan?.source_evidence?.snapshot_ref||'';
    if(ref!==fieldRef){
      fieldRef=ref;fieldScene=null;fieldIssue='';
      if(digest(ref))api('/scene/'+ref.slice(7)+'.json').then(s=>{
        if(ref!==fieldRef)return;
        if(s.frame_id!==plan.base_frame||String(s.stamp_ns)!==String(plan.scan_stamp_ns))throw Error('场景和规划坐标系或时刻不匹配');
        fieldScene=s;if(mode==='field')fieldMap(evidence);
      }).catch(e=>{if(ref===fieldRef){fieldIssue=e.message;if(mode==='field')fieldMap(evidence);}});
    }
    if(mode!=='field')return;
    showPlan(fieldScene,plan);
    empty(fieldScene?'':fieldIssue?'三维观测不可用 · '+fieldIssue:plan?'正在读取任务点云':'等待本次任务的传感器回执');
    set('scene-title','车身周围的占用网格');
    set('scene-meta',fieldScene?'双 MID-360 · 10 cm 网格 · '+localTime(fieldScene.stamp_ns)+' 任务采样':'任务采样 · 车体坐标系');
  }
  function sceneURL(extra){
    const u=new URL(location.href);
    if(mode==='field')u.searchParams.delete('scene');else u.searchParams.set('scene',mode);
    if(extra&&digest(extra))u.searchParams.set('capture',extra.slice(7));else u.searchParams.delete('capture');
    history.replaceState(null,'',u);
  }
  async function choose(next,refresh=false,capture=null){
    mode=next;const seq=++sceneRequest;
    document.body.dataset.scene=next;
    for(const [id,key] of [['scene-field','field'],['view-rgbd','rgbd'],['scene-example','example']]){
      el(id).setAttribute('aria-pressed',String(key===next));
    }
    el('pose-panel').hidden=el('task-panel').hidden=next!=='field';
    el('other-panel').hidden=next==='field';document.querySelector('.camera').hidden=next==='example';
    document.querySelector('.layers').hidden=next==='rgbd';el('refresh-rgb').hidden=el('rgb-grid').hidden=next!=='rgbd';
    renderer.setData(null,null);renderer.reset();el('map').dataset.ready='false';
    if(next==='field'){
      sceneURL();set('rgb-title','任务相机画面');cameraURL='';fieldMap(evidence);camera();return;
    }
    el('rgb').hidden=true;el('rgb-empty').hidden=false;empty('正在读取三维观测');
    if(next==='example'){
      sceneURL();set('scene-title','中央箱体绕行');set('scene-meta','Gazebo 仿真回放');
      set('other-title','仿真规划');set('other-decision','正在读取规划记录');set('other-info','');set('other-source','');
      try{
        example=example||await api('/plan-example.json');if(seq!==sceneRequest)return;
        showPlan(example.scene,example.plan);empty('');
        set('scene-meta','Gazebo · 中央箱体 · 单帧激光规划');
        const lateral=example.plan.path_body_m.reduce((peak,p)=>Math.abs(p[1])>Math.abs(peak)?p[1]:peak,0);
        set('other-decision',example.plan.status==='blocked'?'规划器建议保持原位':
          Math.abs(lateral)<.05?'候选路线沿车头方向前进':'候选路线从车体'+(lateral<0?'右侧':'左侧')+'绕行');
        const curve=example.plan.trajectory;
        const geometry=curve?.continuity==='C2'?'沿曲线绕行，朝向与曲率连续。':'沿规划器给出的航点绕行。';
        const covered=example.plan.status==='partial'?'虚线表示部分通道仍待观测。':'';
        set('other-info',geometry+covered+' 箱体尺寸来自仿真碰撞模型。');
        set('other-source','输入 · Gazebo LaserScan　／　算法 · rover_preview　／　采样时刻 '+example.provenance.scan_stamp_ns+' ns');
      }catch(e){if(seq===sceneRequest)empty('仿真数据不可用 · '+e.message);}
      return;
    }
    set('scene-title','相机彩色三维表面');set('scene-meta','RealSense D435 · 相机光学坐标系');
    set('rgb-title','同帧 RGB 纹理');set('rgb-source','等待配准彩色帧');set('rgb-empty','等待读取 RGB-D');
    set('other-title','RGB-D 采样');set('other-decision','正在读取配准深度与彩色帧');set('other-info','');set('other-source','');
    metric('range','深度命中的 10 cm 单元','—');metric('clearance','纹理三角面','—');metric('coverage','RGB 与深度时差','—');
    try{
      const pinned=typeof capture==='string'&&/^[0-9a-f]{64}$/.test(capture)?capture:null;
      const data=!refresh&&mesh&&(!pinned||mesh.scene_ref==='sha256:'+pinned)?mesh:await api('/scene/rgbd/'+(pinned||'latest')+'.json');
      if(seq!==sceneRequest)return;
      if(data.registration!=='camera_only'||!digest(data.scene_ref)||(pinned&&data.scene_ref!=='sha256:'+pinned))throw Error('RGB-D 采样标识不一致');
      mesh=data;sceneURL(data.scene_ref);renderer.setData(data,null);el('map').dataset.ready='true';empty('');
      const time=localTime(data.stamp_ns);
      set('scene-meta','RealSense D435 · '+time+' 独立采样 · 相机坐标系');
      set('other-decision','10 cm 占用网格与同帧纹理');set('other-info','网格按相机坐标量化实测表面。关掉网格可以单看原图纹理，拖动视角可以查看桌椅和障碍的前后关系。深度缺失处保留空隙。');
      set('other-source',data.frame_id+' · '+data.vertices.filter(Boolean).length+' 个实测顶点 · '+data.stamp_ns+' ns');
      metric('range','深度命中的 10 cm 单元',el('map').dataset.colorCells||'—');
      metric('clearance','纹理三角面',String(data.triangles.length));metric('coverage','RGB 与深度时差',data.skew_ms+' ms');
      const ref=data.source_evidence?.color?.artifact_ref;
      if(digest(ref)&&data.texture_url==='/camera/'+ref.slice(7)+'.png'){
        el('rgb').src=data.texture_url;el('rgb').hidden=false;el('rgb-empty').hidden=true;set('rgb-source',time+' · 同帧纹理');
      }
    }catch(e){if(seq===sceneRequest){empty('彩色三维暂不可用 · '+e.message);set('other-decision','这次没有取得可配准的 RGB-D');}}
  }
  for(const [id,key] of [['scene-field','field'],['view-rgbd','rgbd'],['scene-example','example']]){
    el(id).onclick=()=>choose(key,false,key==='rgbd'&&!mesh?query.get('capture'):null);
  }
  el('refresh-rgb').onclick=()=>choose('rgbd',true);
  el('view-orbit').onclick=()=>renderer.setView('orbit');el('view-top').onclick=()=>renderer.setView('top');
  el('zoom-in').onclick=()=>renderer.zoomBy(1.25);el('zoom-out').onclick=()=>renderer.zoomBy(.8);el('reset-camera').onclick=()=>renderer.reset();
  for(const button of document.querySelectorAll('[data-layer]'))button.onclick=()=>{
    const enabled=button.getAttribute('aria-pressed')!=='true';
    button.setAttribute('aria-pressed',String(enabled));renderer.setLayer(button.dataset.layer,enabled);
  };
  function pose(prefix,value,goal,sampler) {
    if(!value||!finite(value.x_m)||!finite(value.y_m)){set(prefix+'-value','—');set(prefix+'-meta',current&&ended.has(current.state)?'回执中无数据':'等待'+(goal?'规划回执':'采样'));return;}
    set(prefix+'-value','('+fmt(value.x_m,3)+', '+fmt(value.y_m,3)+') m　'+(finite(value.yaw_rad)?fmt(value.yaw_rad*180/Math.PI,1)+'°':'航向未知'));
    const ns=String(value.stamp_ns||'');
    const utc=/^\d{10,19}$/.test(ns)?new Date(Number(BigInt(ns)/1000000n)).toISOString().slice(11,23)+' UTC':null;
    set(prefix+'-meta',[value.frame_id||'未知坐标系',goal?'规划坐标':sampler,goal?'':(utc||'无采样时间')].filter(Boolean).join(' · '));
    el(prefix+'-meta').title=ns?ns+' ns':'';
  }
  function observed(sample) {
    if(!sample?.valid||!sample.pose)return null;
    return {...sample,...sample.pose};
  }
  function presentPoses(plan){
    const start=observed(poseWindow?.start_pose), end=observed(poseWindow?.end_pose), delta=poseWindow?.delta;
    pose('start',start||plan?.initial_pose,false,start?'任务开始':'工具采样');
    pose('end',end||plan?.end_observed_pose,false,end?'任务结束':'工具采样');
    pose('goal',plan?.planned_endpoint,true);
    if(finite(delta?.planar_displacement_m)){
      const yaw=finite(delta.yaw_delta_rad)?' / '+fmt(delta.yaw_delta_rad*180/Math.PI,2)+'°':'';
      set('motion','任务首尾位姿变化 '+meters(delta.planar_displacement_m)+yaw);
    }else if(poseWindow?.status==='incomparable')set('motion','任务首尾位姿无法在同一坐标系比较');
    else if(start&&!end)set('motion','已取任务起始里程计，等待结束采样');
    else set('motion',plan?(plan.stationary?.verified?'工具采样期间实测静止':'工具采样期间静止状态未获核验'):'等待位姿观测');
  }
  async function observePoseWindow(id){
    while(id===requestId){
      const remaining=terminalAt?terminalAt+5000-Date.now():5000;
      if(remaining<=0)break;
      try{
        const r=await fetch('/field/pose-window?'+new URLSearchParams({id}),{credentials:'same-origin',cache:'no-store',signal:AbortSignal.timeout(Math.min(900,remaining))});
        if(r.ok){
          const data=await r.json();
          if(id!==requestId)return;
          if(data&&['pending','completed','incomparable'].includes(data.status)){
            poseWindow=data;presentPoses(evidence);
            if(data.end_pose){el('request').dataset.poseWindow='complete';return;}
          }
        }
      }catch{}
      await new Promise(r=>setTimeout(r,Math.min(500,terminalAt?Math.max(0,terminalAt+5000-Date.now()):500)));
    }
    if(id===requestId)el('request').dataset.poseWindow='closed';
  }
  function present(plan) {
    set('plot-frame',plan?.base_frame||'等待坐标系');
    presentPoses(plan);
    set('source',plan?(plan.source_id||'未知')+' · '+localTime(plan.scan_stamp_ns):'等待传感器回执');
    set('anchor',plan?(plan.map_anchor?'已记录全局地图变换':'全局地图变换未观测'):'全局地图变换 · 待核对');
    fieldMap(plan);
  }
  function paintEvents(task){
    const raw=Array.isArray(task.events)?task.events:[];
    const visible=raw.filter(x=>milestones[x.kind]||x.kind==='transition'&&x.detail&&!['WAITING','RUNNING'].includes(x.detail));
    const hash=visible.map(x=>String(x.seq)).join(',');
    if(hash===lastEvents)return;
    lastEvents=hash;const list=el('events');list.replaceChildren();
    const origin=Date.parse(task.created);
    for(const [i,e] of visible.entries()){
      const row=document.createElement('li'),t=document.createElement('time'),s=document.createElement('span');
      const seconds=(Date.parse(e.utc)-origin)/1000;
      t.textContent=finite(seconds)?'+'+Math.max(0,seconds).toFixed(1)+' s':'—';
      s.textContent=milestones[e.kind]||e.detail;row.title=[e.kind,e.detail,e.event_id].filter(Boolean).join(' · ');
      if(i===visible.length-1)row.className='latest';row.append(t,s);list.append(row);
    }
    list.scrollTop=list.scrollHeight;
  }
  function render(task){
    current=task; evidence=planEvidence(task);const terminal=ended.has(task.state);const tools=(task.steps||[]).map(s=>s.tool).filter(Boolean);
    if(!el('task-text').value && typeof task.text==='string')el('task-text').value=task.text;
    if(!began && finite(Date.parse(task.created)))began=Date.parse(task.created);
    set('state',labels[task.state]||task.state||'待提交');set('task-id',task.id);
    const reason=evidence?.reason;
    const pathCoverage=evidence?.coverage_fraction_on_path;
    const decision=evidence&&[decisions[evidence.status]||evidence.status,reasons[reason]||reason,
      finite(pathCoverage)?'路径覆盖 '+fmt(100*pathCoverage,0)+'%':''].filter(Boolean).join(' · ');
    set('message',decision||task.detail||task.text||'读取任务中');
    set('model','模型 · '+(task.provider||'等待回执'));set('tool','工具 · '+(tools.join(' → ')||'等待回执'));
    el('download').disabled=!terminal;el('submit').disabled=!terminal;
    el('request').dataset.taskState=task.state;el('request').dataset.taskId=task.id;
    if(terminal){if(!terminalAt)terminalAt=Date.now();const seconds=(Date.parse(task.updated)-Date.parse(task.created))/1000;set('elapsed',finite(seconds)?Math.max(0,seconds).toFixed(1)+' s':'已结束');}
    paintEvents(task);present(evidence);if(terminal)camera();
  }
  async function observe(id){
    while(id===requestId){
      try{
        const task=await api('/agent/tasks?'+new URLSearchParams({id,limit:'320'}));
        if(id!==requestId)return;
        if(task.id!==id)throw Error('任务标识与当前提交不一致');
        connected(true);alertUser('');render(task);if(ended.has(task.state))return;
      }catch(e){connected(false);alertUser('读取本次任务回执失败 · '+e.message);}
      await new Promise(r=>setTimeout(r,650));
    }
  }
  const resume=query.get('task');
  el('request').addEventListener('submit',async ev=>{
    ev.preventDefault();if(waiting||el('submit').disabled)return;
    const text=el('task-text').value.trim();if(!text)return;
    if(mode!=='field')choose('field');waiting=true;el('submit').disabled=true;alertUser('');began=Date.now();lastEvents='';current=null;evidence=null;poseWindow=null;terminalAt=0;el('request').dataset.poseWindow='pending';el('events').replaceChildren();present(null);
    const id='plan-'+Array.from(crypto.getRandomValues(new Uint8Array(12)),n=>n.toString(16).padStart(2,'0')).join('');
    requestId=id;set('message',text);set('task-id',id);set('state','提交中');el('download').disabled=true;
    let submitted;
    try{submitted=await api('/agent/tasks',{id,text});}
    catch(e){
      alertUser('提交响应异常 · '+e.message);set('state','核对任务');
      try{submitted=await api('/agent/tasks?'+new URLSearchParams({id}));}
      catch{set('state','提交未确认');el('submit').disabled=false;waiting=false;return;}
    }
    if(submitted.id!==id){set('state','任务标识异常');waiting=false;return;}
    const u=new URL(location.href);u.searchParams.set('task',id);history.replaceState(null,'',u);
    render(submitted);waiting=false;observePoseWindow(id);observe(id);
  });
  el('download').addEventListener('click',()=>{if(!current)return;const link=document.createElement('a');link.download=current.id+'.json';
    const url=URL.createObjectURL(new Blob([JSON.stringify({task:current,pose_window:poseWindow},null,2)],{type:'application/json'}));link.href=url;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});

  setInterval(()=>{if(began&&(!current||!ended.has(current.state)))set('elapsed',Math.max(0,(Date.now()-began)/1000).toFixed(1)+' s');},250);
  checkConnection();
  camera();setInterval(camera,2500);
  (async()=>{
    const active=['rgbd','example'].includes(query.get('scene'))?query.get('scene'):'field';
    choose(active,false,query.get('capture'));
    const initial=requestId;let id=resume;
    if(!id){try{id=(await api('/field/recent-task')).id;}catch{}}
    if(!id||!/^[\w.-]{1,96}$/.test(id)||requestId!==initial||waiting)return;
    requestId=id;el('submit').disabled=true;observePoseWindow(id);observe(id);
  })();
})();
