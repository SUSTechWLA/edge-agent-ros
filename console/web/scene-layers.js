/* Browser client for the archive-only scene workbench. Every state change and
   query is served by the local runtime. The two viewports have independent frames. */
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const set = (id, value) => { el(id).textContent = value; };
  const hidden = (id, value) => { el(id).hidden = value; };
  const tone = (id, value) => { el(id).dataset.tone = value; };
  const number = new Intl.NumberFormat('zh-CN');
  const time = new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', fractionalSecondDigits: 3,
    timeZone: 'Asia/Shanghai', hourCycle: 'h23'
  });
  const caseNames = {field_hold: 'Jetson 近场', centered: 'Gazebo 中央箱体', clear: 'Gazebo 空场'};
  const viewers = Object.fromEntries(['occupancy', 'texture', 'plan'].map(name =>
    [name, window.FieldScene.create(el(name+'-map'),name==='texture' ?
      {requireTextureMapping:true,onTextureState:textureState} : {})]));
  viewers.texture.setLayer('rgbGrid', false);
  const observations = {occupancy: null, texture: null};
  const previousPlans = new Map();
  let status = null, opened = null, busy = false, inspectingPlan = false, entries = 0;
  let rgbState='none', requestedRGB=true;

  class ApiError extends Error {
    constructor(httpStatus, payload) {
      const body = payload?.error ?? payload ?? {};
      const code = body.code ?? payload?.code ?? 'HTTP '+httpStatus;
      super(body.message ?? payload?.message ?? code);
      this.code = code;
      this.body = payload;
    }
  }
  async function api(route, body) {
    const opts = {cache: 'no-store'};
    if (body !== undefined) {
      opts.method = 'POST';
      opts.headers = {'Content-Type': 'application/json'};
      opts.body = JSON.stringify(body);
    }
    const response = await fetch('/api/'+route, opts);
    const data = await response.json();
    if (!response.ok) throw new ApiError(response.status, data);
    return data;
  }

  function issueText(error) {
    return error.code === error.message || !error.code ? error.message : error.code+' · '+error.message;
  }
  function log(title, note, color='good') {
    if (!entries) el('history').replaceChildren();
    const row = document.createElement('li');
    const timestamp = document.createElement('time'), action = document.createElement('strong');
    const detail = document.createElement('span');
    timestamp.dateTime = new Date().toISOString();
    timestamp.textContent = time.format(new Date()).slice(0, 8);
    action.textContent = title;
    action.dataset.tone = color;
    detail.textContent = note;
    detail.className = 'note';
    row.append(timestamp, action, detail);
    el('history').prepend(row);
    el('history').scrollTop = 0;
    if (el('history').children.length > 60) el('history').lastElementChild.remove();
    set('history-count', ++entries+' 次操作 · 本机时间（上海）');
  }
  function controls() {
    el('archive').disabled = !status || busy;
    el('open-archive').disabled = !status || busy;
    el('toggle-texture').disabled = !status || busy;
    el('check-registration').disabled = !opened || !status || busy;
    const planAvailable = status?.cases?.includes(el('plan-case').value);
    el('plan-case').disabled = !planAvailable || busy;
    el('run-plan').disabled = !planAvailable || busy;
  }
  async function action(fn) {
    if (busy) return;
    busy = true;
    controls();
    hidden('error', true);
    try {
      await fn();
    } catch (error) {
      const message = issueText(error);
      set('error', message);
      hidden('error', false);
      log('请求未完成', message, 'warn');
    } finally {
      busy = false;
      controls();
    }
  }
  function clearLayer(kind, message) {
    observations[kind] = null;
    viewers[kind].setData(null, null);
    if (kind === 'texture') {
      hidden('texture-photo',true);
      el('texture-image').removeAttribute('src');
      textureState('none');
    }
    if (kind === 'occupancy') restoreObservation();
    for (const suffix of ['frame','time','value']) set(kind+'-'+suffix, '—');
    set(kind+'-empty', message);
    hidden(kind+'-empty', false);
    hidden(kind+'-controls', true);
    set(kind+'-caption', kind === 'occupancy' ? '雷达占用图层' : 'RGB-D 纹理图层');
  }
  async function refresh() {
    try {
      const result = await api('state');
      if (typeof result.texture_enabled !== 'boolean' || !result.archive?.id) {
        throw new Error('插件状态回执缺少归档或启用状态');
      }
      if (el('archive').value !== result.archive.id) {
        el('archive').replaceChildren(new Option(result.archive.label || result.archive.id, result.archive.id));
      }
      if (Array.isArray(result.cases)) {
        const select = el('plan-case');
        for (const option of select.options) {
          option.hidden = option.disabled = !result.cases.includes(option.value);
        }
        if (select.selectedOptions[0]?.disabled) {
          select.value = [...select.options].find(option => !option.disabled)?.value || '';
        }
      }
      if (status && opened && opened !== result.archive.id) {
        opened = null;
        clearLayer('occupancy', '归档已切换，请重新读取');
        clearLayer('texture', '归档已切换，请重新读取');
      }
      const previouslyEnabled = status?.texture_enabled;
      status = result;
      set('connection','DSH Runtime 在线 · 归档回放');
      tone('connection','good');
      set('texture-state',result.texture_enabled?'已启用':'已停用');
      tone('texture-state',result.texture_enabled?'good':'warn');
      set('toggle-texture',result.texture_enabled?'停用纹理插件':'启用纹理插件');
      if (!result.texture_enabled && (previouslyEnabled !== false || observations.texture)) {
        clearLayer('texture','纹理插件已停用');
      }
      if (result.texture_enabled && previouslyEnabled === false && !observations.texture) {
        clearLayer('texture','纹理插件已启用，待读取图层');
      }
    } catch (error) {
      status = null;
      set('texture-state','状态未知');
      tone('texture-state','muted');
      set('connection','Runtime 状态不可用');
      tone('connection','warn');
      throw error;
    }
    controls();
  }
  function stamp(ns, simulation=false) {
    if (!/^\d+$/.test(String(ns))) return '—';
    if (simulation) return (Number(ns)/1e9).toFixed(3)+' s · 仿真';
    const date = new Date(Number(BigInt(ns)/1000000n));
    return Number.isNaN(+date) ? '—' : time.format(date);
  }
  function reference(id) {
    return typeof id === 'string' && id.startsWith('sha256:') ? id.slice(7,19) : '';
  }
  function metrics(kind, scene) {
    set(kind+'-frame', scene.frame_id);
    set(kind+'-time', stamp(scene.stamp_ns));
    el(kind+'-time').title = scene.stamp_ns+' ns · Asia/Shanghai';
    const occupancy = kind === 'occupancy';
    if (occupancy) set('occupancy-metric','占用单元');
    set(kind+'-value',number.format(occupancy?scene.voxels.length:scene.triangles.length));
  }
  function restoreObservation() {
    inspectingPlan = false;
    viewers.plan.setData(null, null);
    hidden('plan-map', true);
    hidden('occupancy-map', false);
    hidden('restore-observation', true);
    set('occupancy-heading','近场占用');
    set('occupancy-source','双 MID‑360');
    const item = observations.occupancy;
    if (item) {
      metrics('occupancy', item.scene);
      set('occupancy-caption', layerCaption('occupancy', item));
      hidden('occupancy-controls',false);
      hidden('occupancy-empty',true);
    }
  }
  function layerCaption(kind, item) {
    const ref = reference(item.layer_ref);
    const detail = kind === 'texture'
      ? item.scene.image_width+' × '+item.scene.image_height+' px · 同帧 RGB-D'
      : Math.round(item.scene.voxel_size_m*100)+' cm 体素 · 双雷达';
    return detail+(ref?' · '+ref:'');
  }
  function imageCoverage(scene) {
    const {grid_width:cols,grid_height:rows,stride_px:step,image_width:w,image_height:h,texture_mapping:m}=scene;
    if(!Number.isInteger(step)||step<1||cols!==Math.ceil(w/step)||rows!==Math.ceil(h/step)||
        m?.coordinates!=='normalized_pixel_centers'||m.origin!=='top_left'||
        m.image_ref!==scene.source_evidence?.color?.artifact_ref||m.uv?.length!==cols*rows)return null;
    // On this sampling lattice each cell has one shared diagonal and two distinct
    // half-cells. Prove non-overlap before summing their projected image areas.
    const seen=new Set();
    for(const tri of scene.triangles){
      if(!Array.isArray(tri)||tri.length!==3||!tri.every(i=>Number.isInteger(i)&&i>=0&&i<cols*rows))return null;
      const [lo,mid,hi]=[...tri].sort((a,b)=>a-b);
      const first=lo%cols<cols-1 && mid===lo+1 && hi===lo+cols;
      const second=lo%cols>0 && mid===lo+cols-1 && hi===lo+cols;
      const key=lo+','+mid+','+hi;
      if((!first&&!second)||seen.has(key))return null;
      seen.add(key);
      for(const i of tri){
        const uv=m.uv[i];
        if(!Array.isArray(uv)||uv.length!==2||!uv.every(Number.isFinite)||
          Math.abs(uv[0]*w-.5-i%cols*step)>.0001||Math.abs(uv[1]*h-.5-Math.floor(i/cols)*step)>.0001)return null;
      }
    }
    return seen.size*step*step/(2*w*h);
  }
  function textureControls() {
    const item=observations.texture, ready=rgbState==='ready';
    const showingRGB=ready && requestedRGB;
    viewers.texture.setLayer('sourceRGB',showingRGB);
    el('texture-map').dataset.appearance=item?(showingRGB?'rgb':'geometry'):'none';
    el('surface-rgb').disabled=!ready;
    el('surface-rgb').setAttribute('aria-pressed',String(showingRGB));
    el('surface-geometry').setAttribute('aria-pressed',String(Boolean(item) && !showingRGB));
    const link=el('download-texture-map');
    const isRef=v=>typeof v==='string' && /^sha256:[0-9a-f]{64}$/.test(v);
    const printable=ready && isRef(item?.scene_ref) && isRef(item?.layer_ref);
    if(printable)link.href='/api/maps/'+item.scene_ref.slice(7)+'/'+item.layer_ref.slice(7)+'.glb';
    else link.removeAttribute('href');
    link.setAttribute('aria-disabled',String(!printable));
    link.tabIndex=printable?0:-1;
    const coverage=item?.imageCoverage;
    hidden('texture-coverage',!Number.isFinite(coverage));
    if(Number.isFinite(coverage))set('texture-coverage','覆盖原图 '+(coverage*100).toFixed(1)+'% · 深度不连续处留空');
    if(!item)return;
    const labels={loading:'正在加载同帧 RGB',unavailable:'纹理或像素映射不可用 · 显示几何'};
    set('texture-caption',labels[rgbState] || (showingRGB?'RGB 已贴合深度表面':'深度几何')+
      ' · '+item.scene.image_width+' × '+item.scene.image_height+' px');
  }
  function textureState(state,image) {
    rgbState=state;
    if(state==='ready' && image){
      const old=el('texture-image');
      if(old!==image){
        image.id='texture-image';
        image.alt=old.alt;
        old.replaceWith(image);
      }
    }
    hidden('texture-photo',state!=='ready');
    textureControls();
  }
  async function readLayer(kind) {
    const item = await api('layers', {archive: opened, layer: kind});
    const scene = item.scene;
    const valid = kind === 'texture' ? scene?.registration === 'camera_only' && Array.isArray(scene.triangles)
      : scene?.registration !== 'camera_only' && Array.isArray(scene?.voxels);
    if (!valid || !scene.frame_id) throw new Error('图层返回的坐标或表示不匹配');
    if(kind==='texture')item.imageCoverage=imageCoverage(scene);
    observations[kind] = item;
    if (kind === 'occupancy') restoreObservation();
    viewers[kind].setData(scene, null);
    viewers[kind].reset();
    metrics(kind, scene);
    if(kind==='texture')textureControls();
    else set(kind+'-caption', layerCaption(kind, item));
    el(kind+'-caption').title = item.layer_ref || '';
    hidden(kind+'-empty', true);
    hidden(kind+'-controls', false);
    return item;
  }
  async function openArchive() {
    opened = el('archive').value;
    const kinds = ['occupancy','texture'];
    for (const kind of kinds) clearLayer(kind,'正在读取图层');
    set('query-title','等待查询');
    set('query-detail','联合读取需要可验证的时间和坐标关系。');
    tone('query-result','muted');
    const results = await Promise.allSettled(kinds.map(readLayer));
    results.forEach((result, index) => {
      const kind = kinds[index], name = kind === 'occupancy' ? '雷达占用' : 'RGB-D 纹理';
      if (result.status === 'fulfilled') {
        log('读取'+name, result.value.scene.frame_id+' · '+stamp(result.value.scene.stamp_ns));
      } else {
        clearLayer(kind,kind === 'texture' && !status?.texture_enabled?'纹理插件已停用':'图层读取失败');
        log(name+'不可用',issueText(result.reason),'warn');
      }
    });
  }

  function duration(ms) {
    const seconds = Math.round(ms/1000);
    return Math.floor(seconds/60)+' 分 '+seconds%60+' 秒';
  }
  function rejection(error) {
    const payload = error.body || {};
    const reasons = payload.reasons || payload.error?.reasons || [];
    const codes = reasons.map(v => typeof v === 'string' ? v : v.code);
    const messages = [];
    if (codes.includes('scene.missing_registration')) messages.push('缺少两份观测之间的配准变换');
    if (codes.includes('scene.time_skew')) {
      const delta = payload.time_span_ms ?? payload.error?.time_span_ms;
      const diff = Number.isFinite(delta) ? '，相差 '+duration(delta) : '';
      messages.push('采样时间超出窗口'+diff);
    }
    const other = codes.filter(c => c !== 'scene.time_skew' && c !== 'scene.missing_registration');
    return {codes, detail:[...messages,...other].join('。') || issueText(error)};
  }
  async function compose() {
    try {
      const item = await api('compose', {archive:opened, layers:['occupancy','texture']});
      if (!item.scene_ref) throw new Error('联合查询回执缺少场景引用');
      tone('query-result','good');
      set('query-title','已取得联合场景');
      set('query-detail','场景摘要 '+item.scene_ref);
      log('取得联合场景',item.scene_ref);
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      const {codes,detail} = rejection(error);
      const title = codes.includes('scene.missing_registration') ? '拒绝合并' : '联合场景不可用';
      set('query-title',title);
      set('query-detail',detail);
      tone('query-result','warn');
      log(title,detail,'warn');
    }
  }
  async function toggleTexture() {
    const enabled = !status.texture_enabled;
    try {
      await api('texture', {enabled});
    } finally {
      await refresh();
    }
    if (status.texture_enabled !== enabled) throw new Error('Runtime 返回的纹理状态与请求不一致');
    log(enabled?'启用纹理插件':'停用纹理插件','已从 DSH Runtime 重新读取插件状态', enabled?'good':'warn');
    if (!opened) return;
    if (enabled) {
      await readLayer('texture');
      return;
    }
    try {
      await api('layers',{archive:opened,layer:'texture'});
    } catch (error) {
      if (!(error instanceof ApiError)) throw error;
      set('texture-caption','新读取已拒绝 · '+error.code);
      log('复查纹理读取','新请求返回 '+error.code,'warn');
      return;
    }
    throw new Error('纹理停用后，新读取仍成功，请检查运行时');
  }

  function describePlan(plan) {
    const points = plan.path_body_m.length;
    if (plan.status === 'blocked') {
      const clearance = plan.present_min_clearance_m, margin = plan.margin_m;
      const detail = plan.reason === 'current_footprint_below_margin' && Number.isFinite(clearance) && Number.isFinite(margin)
        ? '车身净距 '+clearance.toFixed(3)+' m，要求 '+margin.toFixed(3)+' m'
        : plan.reason;
      return {title:'保持原位', detail, points, tone:'warn'};
    }
    const covered = Number.isFinite(plan.coverage_fraction_on_path)
      ? ' · 观测覆盖 '+(100*plan.coverage_fraction_on_path).toFixed(1)+'%' : '';
    return {title:plan.status === 'partial'?'部分可见的候选路线':'已生成候选路线',
      detail:points+' 个路径点'+covered, points, tone:'good'};
  }
  function drawPlan(item, name, result) {
    inspectingPlan = true;
    const isField = name === 'field_hold';
    const observation = observations.occupancy?.scene;
    const sameSnapshot = isField && item.snapshot_ref && item.snapshot_ref === observation?.source_evidence?.snapshot_ref;
    const backdrop = item.scene ?? (sameSnapshot ? observation : null);
    const scene = backdrop?.frame_id === item.plan.base_frame ? backdrop : null;
    viewers.plan.setData(scene, item.plan);
    viewers.plan.reset();
    hidden('occupancy-map',true);
    hidden('plan-map',false);
    hidden('occupancy-empty',true);
    hidden('occupancy-controls',false);
    hidden('restore-observation',!observations.occupancy);
    set('occupancy-heading','局部避障预览');
    set('occupancy-source',caseNames[name]);
    set('occupancy-frame',item.plan.base_frame);
    set('occupancy-time',stamp(item.source_stamp_ns,!isField));
    el('occupancy-time').title = item.source_stamp_ns+' ns';
    set('occupancy-metric','候选路径点');
    set('occupancy-value',result.points);
    set('occupancy-caption',result.title+' · '+caseNames[name]+(scene?'':' · 仅显示车体坐标与规划结果'));
  }
  async function runPlan() {
    const name = el('plan-case').value;
    const item = await api('plan',{case:name});
    if (item.case !== name || !item.request_id || item.plan?.motion_authorized !== false || !item.plan.base_frame ||
        !['planned','partial','blocked'].includes(item.plan.status) || !Array.isArray(item.plan.path_body_m)) {
      throw new Error('规划回执的输入、状态或只读范围不匹配');
    }
    const result = describePlan(item.plan);
    const old = previousPlans.get(name);
    const fingerprint = JSON.stringify([item.plan.status,item.plan.reason,item.plan.path_body_m,
      item.plan.margin_m,item.plan.present_min_clearance_m,item.plan.coverage_fraction_on_path]);
    const sameInput = item.snapshot_ref && old?.snapshot === item.snapshot_ref;
    const comparison = sameInput && old.request !== item.request_id
      ? (old.fingerprint === fingerprint ? ' · 与上次同输入结果一致' : ' · 同输入结果有变化') : '';
    previousPlans.set(name,{snapshot:item.snapshot_ref, fingerprint, request:item.request_id});
    drawPlan(item,name,result);
    set('plan-title',result.title);
    set('plan-detail',result.detail+comparison);
    el('plan-detail').title = item.request_id;
    tone('plan-result',result.tone);
    log('规划回执 · '+caseNames[name],result.detail+comparison+' · 请求 '+String(item.request_id).slice(-10));
  }

  el('open-archive').onclick = () => action(openArchive);
  el('check-registration').onclick = () => action(compose);
  el('toggle-texture').onclick = () => action(toggleTexture);
  el('run-plan').onclick = () => action(runPlan);
  el('view-top').onclick = () => viewers[inspectingPlan?'plan':'occupancy'].setView('top');
  el('view-reset').onclick = () => viewers[inspectingPlan?'plan':'occupancy'].reset();
  el('restore-observation').onclick = restoreObservation;
  el('texture-grid').onclick = event => {
    const next = event.target.getAttribute('aria-pressed') !== 'true';
    event.target.setAttribute('aria-pressed',String(next));
    viewers.texture.setLayer('rgbGrid',next);
  };
  el('texture-reset').onclick = () => viewers.texture.reset();
  for(const [id,rgb] of [['surface-rgb',true],['surface-geometry',false]]){
    el(id).onclick=()=>{
      requestedRGB=rgb;
      textureControls();
    };
  }
  const card=el('texture-card');
  el('texture-expand').hidden=!document.fullscreenEnabled;
  el('texture-expand').onclick=()=>action(async()=>{
    if(document.fullscreenElement===card)await document.exitFullscreen();
    else await card.requestFullscreen();
  });
  document.addEventListener('fullscreenchange',()=>{
    const expanded=document.fullscreenElement===card;
    el('texture-expand').setAttribute('aria-pressed',String(expanded));
    set('texture-expand',expanded?'收起':'放大');
  });
  window.addEventListener('focus', () => { if (!busy) action(refresh); });
  action(async()=>{
    await refresh();
    if(new URLSearchParams(location.search).get('view')==='texture'){
      await openArchive();
      card.scrollIntoView({block:'nearest'});
    }
  });
})();
