/* Render a pinned RGB–LiDAR layer. All filtering happens on the local snapshot. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const count = value => value.toLocaleString('zh-CN');
  const ref = value => typeof value==='string' && /^sha256:[0-9a-f]{64}$/.test(value);
  const labels = {
    behind_camera:'相机后方',outside_fov:'相机视野外',unknown_depth:'深度无效',
    occluded:'前景遮挡',depth_disagreement:'表面深度不符',depth_boundary:'深度边界'
  };
  let snapshot=null, observedPoints=null, visible=0, pending=false;
  const groups=new Map(), enabled=new Set();
  const viewer=window.createSpatialRenderer(el('cloud'), {
    invalidate:schedule, onPick:value=>{
      el('pick').textContent=value || '拖动旋转 · 滚轮缩放 · 点击点云查询坐标';
    }
  });
  function schedule() {
    if(pending || !snapshot)return;
    pending=true;
    requestAnimationFrame(()=>{
      pending=false;
      viewer.render(snapshot,{cloud:true,cloudAppearance:'registered_rgb',fitPoints:observedPoints});
      el('empty').hidden=visible>0;
      if(!visible)el('empty').textContent='当前筛选没有可见点';
    });
  }
  function applyFilter() {
    if(!snapshot)return;
    const sources=Object.create(null);
    visible=0;
    for(const [name,groupsForSource] of groups){
      if(!enabled.has(name))continue;
      sources[name]=el('unknown').checked ? groupsForSource.all : groupsForSource.colored;
      visible+=sources[name].points.length;
    }
    snapshot.cloud_sources=sources;
    el('shown').textContent='当前显示 '+count(visible)+' 点';
    el('shown').dataset.count=visible;
    viewer.clearPick();
    schedule();
  }
  function load(payload) {
    const {scene,descriptor,scene_ref:sceneRef,layer_ref:layerRef}=payload;
    if(!ref(sceneRef)||!ref(layerRef)||descriptor?.frame_id!==scene?.frame_id||
       !Array.isArray(scene?.points)||!Array.isArray(scene.colors)||!Array.isArray(scene.contributors)||
       scene.points.length!==scene.colors.length||scene.points.length!==scene.contributors.length){
      throw Error('地图修订、参考系或点与颜色数量不匹配');
    }
    let colored=0;
    for(let i=0;i<scene.points.length;i++){
      const p=scene.points[i], c=scene.colors[i], origin=scene.contributors[i];
      if(!Array.isArray(p)||p.length!==3||!p.every(Number.isFinite) ||
         (c!==null && (!Array.isArray(c)||c.length!==3||!c.every(n=>Number.isInteger(n)&&n>=0&&n<=255)))||
         !Array.isArray(origin)||typeof origin[0]!=='string'||!origin[0]||
         !Number.isInteger(origin[1])||origin[1]<0)throw Error('点、颜色或来源索引无效');
      const id=origin[0];
      if(!groups.has(id)){
        const cloud=()=>({name:id,points:[],colors:[]});
        groups.set(id,{all:cloud(),colored:cloud()});
      }
      const g=groups.get(id);
      g.all.points.push(p);g.all.colors.push(c);
      if(c!==null){g.colored.points.push(p);g.colored.colors.push(c);colored++;}
    }
    const n=scene.points.length;
    observedPoints=scene.points;
    if(scene.stats?.colored!==colored || scene.stats?.uncolored!==n-colored)throw Error('图层点数与融合统计不一致');
    for(const [id,g] of groups){
      enabled.add(id);
      const label=document.createElement('label'), input=document.createElement('input'), number=document.createElement('span');
      input.type='checkbox';input.checked=true;input.value=id;label.className='filter';
      input.addEventListener('change',()=>{if(input.checked)enabled.add(id);else enabled.delete(id);applyFilter();});
      number.textContent=count(g.all.points.length);
      label.append(input,document.createTextNode(id),number);
      el('sources').append(label);
    }
    const ratio=n ? colored/n : 0;
    el('colored').textContent=count(colored);el('uncolored').textContent=count(n-colored);
    el('percent').textContent=n ? (100*ratio).toFixed(1)+'%' : '—';el('bar').value=ratio;
    for(const [cause,qty] of Object.entries(scene.stats.rejections||{})){
      // Invalid XYZ never enters the displayed cloud.
      if(cause==='invalid_xyz'||!Number.isInteger(qty)||qty<=0)continue;
      const row=document.createElement('div'),dt=document.createElement('dt'),dd=document.createElement('dd');
      dt.textContent=labels[cause]||cause;dd.textContent=count(qty);
      row.append(dt,dd);el('reasons').append(row);
    }
    const source=scene.calibration_source==='gazebo_sdf'?'Gazebo 仿真标定':scene.calibration_source || '归档标定';
    const windows=Array.isArray(scene.windows)?scene.windows:[];
    const confirmed=windows.length>0 && windows.every(w=>w.pose_source==='gazebo_pose_info');
    const views=windows.length?' · '+count(windows.length)+(confirmed?' 个已知采集位姿':' 个采集窗口'):'';
    el('provenance').textContent=source+' · '+count(groups.size)+' 路激光点云与 RGB-D 观测'+views;
    el('frame').textContent=scene.frame_id+' · m';el('clock').textContent=scene.clock_id||'未知';
    if(Number.isFinite(scene.stats.acquisition_span_ms))el('skew').textContent=scene.stats.acquisition_span_ms.toFixed(2)+' ms';
    if(windows.length){
      el('clock').textContent=[...new Set(windows.map(w=>w.clock_id))].join(' / ') || '未知';
      el('skew-name').textContent='单站最大采样跨度';
      const spans=windows.map(w=>{
        if(w.acquisition?.length!==2 || !w.acquisition.every(s=>typeof s==='string' && /^\d+$/.test(s)))return NaN;
        return Number(BigInt(w.acquisition[1])-BigInt(w.acquisition[0]))/1e6;
      });
      if(spans.every(ms=>Number.isFinite(ms)&&ms>=0))el('skew').textContent=Math.max(...spans).toFixed(2)+' ms';
      for(const w of windows){
        const xyz=w.T_world_rig?.slice(0,3).map(row=>row[3]);
        if(xyz?.length!==3||!xyz.every(Number.isFinite))continue;
        const row=document.createElement('div'), dt=document.createElement('dt'), dd=document.createElement('dd');
        dt.textContent=w.id;dd.textContent=xyz.map(v=>v.toFixed(2)).join(', ');
        row.append(dt,dd);el('stations').append(row);
      }
      el('survey').hidden=!el('stations').childElementCount;
    }
    if(Number.isFinite(scene.depth_tolerance_m)){
      el('tolerance').textContent=scene.depth_tolerance_m+' m';el('tolerance-row').hidden=false;
    }
    el('gaps').hidden=!el('reasons').childElementCount;
    const link=el('download');link.href='/api/fusion/'+sceneRef.slice(7)+'/'+layerRef.slice(7)+'.ply';
    link.setAttribute('aria-disabled','false');link.download='rgb-lidar-map.ply';
    el('tools').hidden=false;el('unknown').disabled=false;
    el('status').textContent='只读地图已载入';el('status').dataset.tone='good';
    snapshot={frame:scene.frame_id};
    applyFilter();
  }
  el('top').onclick=()=>viewer.top();el('reset').onclick=()=>viewer.reset();
  el('unknown').onchange=applyFilter;
  new ResizeObserver(schedule).observe(el('cloud'));
  async function start(){
    const response=await fetch('/api/fusion/map',{cache:'no-store'});
    if(!response.ok)throw Error('服务返回 HTTP '+response.status);
    load(await response.json());
  }
  start().catch(error=>{
    el('status').textContent='地图不可用';
    el('empty').textContent=error.message;el('empty').hidden=false;
  });
})();
