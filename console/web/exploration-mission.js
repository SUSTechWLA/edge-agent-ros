const el = id => document.getElementById(id);
const simulation = environment => environment === 'simulation';
const point = value => Array.isArray(value) && value.length >= 2 && value.slice(0, 2).every(Number.isFinite);
const number = (value, digits = 2) => Number.isFinite(value) ? Number(value.toFixed(digits)).toFixed(digits) : '—';
const pair = value => point(value) ? `(${number(value[0])}, ${number(value[1])}) m` : '—';
const xy = pose => pose && [pose.x, pose.y];
const timestamp = value => {
  if (value === null || value === undefined) return '—';
  const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
  return Number.isFinite(date.valueOf()) ? date.toLocaleTimeString('zh-CN', {hour12: false}) : '—';
};

const statuses = {
  STARTING: ['正在准备', 'busy'], QUEUED: ['任务排队', 'busy'], PLANNING: ['正在规划', 'busy'],
  RUNNING: ['执行中', 'busy'], EXPLORING: ['自主探索中', 'busy'], NAVIGATING: ['导航中', 'busy'],
  WAITING: ['等候结果', 'busy'], WAITING_OPERATOR: ['等待人工复核', 'warning'],
  SUCCEEDED: ['任务完成', 'good'], COMPLETE: ['任务完成', 'good'], PARTIAL: ['部分覆盖', 'warning'],
  RECOVERY_REQUIRED: ['等待核验停稳', 'warning'], BLOCKED: ['任务受阻', 'warning'],
  CANCELED: ['已取消', 'neutral'], FAILED: ['任务失败', 'error'], REJECTED: ['任务被拒绝', 'error']
};
const phases = {
  starting: '准备观测和机体执行链', admitted: 'Agent 已绑定任务能力', blank_map: '从空白栅格开始',
  empty_map: '从空白栅格开始', observing: '读取最新占用栅格与位姿',
  selecting: '评估可达观察点', exploring: '沿可通行区域推进探索', moving: '机器人执行当前路段',
  refocus: '自主换角度补扫', rescan: '自主换角度补扫', review: '待复核区域已标记',
  waiting_operator: '暂停在当前安全位置', blocked: '任务受阻',
  resumed: '继续原建图任务', publishing: '保存带版本的地图与标注',
  map_published: '地图和语义已归档', navigation: '使用发布地图导航', navigating: '使用发布地图导航',
  dynamic_hold: '保持停驶，等待通道复查', dynamic_recheck: 'Agent 正在复查通道',
  rechecking_clearance: 'Agent 正在复查通道',
  dynamic_resumed: '使用原地图继续导航', dynamic_blocked: '持续堵塞，已核验停车',
  complete: '保留地图与停稳记录', arrived: '到达目标停靠位并已停稳'
};
const dynamicStates = {
  dynamic_hold: ['动态避障停车', '停车等待', 'warning'],
  dynamic_recheck: ['Agent 复查通道', '复查中', 'warning'],
  dynamic_resumed: ['继续原导航', '已续跑', 'good'],
  dynamic_blocked: ['通道持续堵塞', '已停车', 'warning']
};
const eventNames = {
  'task.created': '接收自然语言任务', 'agent.dispatched': 'Agent 派发能力',
  'exploration.started': '开始自主探索', 'exploration.blank_map': '初始化空白地图',
  'exploration.frontier': '选择可达前沿', 'exploration.dispatch': '执行短程目标',
  'exploration.receipt': '已核验到点', 'exploration.coverage': '更新观测覆盖',
  'exploration.refocus': '自主换角度补扫', 'exploration.anomaly': '发现观测异常',
  'exploration.intervention': '请求人工复核', 'exploration.resumed': '继续原任务',
  'exploration.published': '发布地图版本', 'semantic.confirmed': '确认语义地点',
  'navigation.route': '基于已发布地图规划', 'navigation.arrived': '核验到点和停稳'
};
Object.assign(eventNames, {
  task_admitted: '接收并绑定自然语言任务', empty_map: '初始化空白地图', observed: '更新观测覆盖', frontier_selected: '选择可达前沿',
  alternate_view: '自主换角度补扫', anomaly: '发现观测异常', intervention_requested: '请求人工复核',
  resumed: '继续原任务', semantic_confirmed: '确认语义地点', publishing: '发布地图版本',
  destination_resolved: '核对地图中的目标停靠位', route_planned: '生成已发布地图路线',
  motion_verified: '机体核验路段停稳', obstacle: '观测到障碍', finished: '收到任务终态',
  semantic_candidate: '发现候选门牌', refocus_unavailable: '没有安全的替代观察点', anomaly_resolved: '新观测解除疑问'
});
for (const [kind, [label]] of Object.entries(dynamicStates)) eventNames[kind] = label;
const pendingStates = new Set(['PLANNING', 'STARTING', 'QUEUED', 'RUNNING', 'EXPLORING', 'NAVIGATING',
  'WAITING', 'WAITING_OPERATOR', 'RECOVERY_REQUIRED']);
const currentId = new URL(location.href).searchParams.get('task_id');
let requested = currentId || '';
let pending = null;
let knownTasks = [];
let displayed = null;
let draft = null;
let connected = false;
let submitting = false;
let deciding = false;
let reading = false;
let local = new URL(location.href).searchParams.get('view') !== 'global';
let campus3d = false;
let viewChosen = new URL(location.href).searchParams.has('view');
const campus = createCampusAtlas(() => paintMap(displayed));
let renderKey = '';
let anomalyKey = '';
let recoveryKey = '';
let agentState = null;
const trails = new Map();

const show = (id, text) => { el(id).textContent = text; };

async function api(path, body) {
  const signal = AbortSignal.timeout(5500);
  const response = await fetch(path, body ? {
    method: 'POST', signal, headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  } : {cache: 'no-store', signal});
  let data;
  try { data = await response.json(); }
  catch { throw new Error(`无法读取服务回执 · HTTP ${response.status}`); }
  if (!response.ok) throw new Error(data.error || data.detail || data.code || `HTTP ${response.status}`);
  return data;
}

function selectTask(id) {
  requested = id || '';
  pending = null;
  agentState = null;
  const next = new URL(location.href);
  if (requested) next.searchParams.set('task_id', requested);
  else next.searchParams.delete('task_id');
  history.replaceState(null, '', next);
  if (displayed?.id !== requested) paint(null);
}

function updateControls() {
  const active = knownTasks.some(task => pendingStates.has(String(task.status || '').toUpperCase())) ||
    pendingStates.has(String(displayed?.status || '').toUpperCase()) ||
    pendingStates.has(String(agentState?.state || '').toUpperCase());
  el('submit-task').disabled = !connected || submitting || active || Boolean(pending && pending.id === requested);
  for (const button of el('intervention-actions').querySelectorAll('button')) {
    button.disabled = !connected || deciding;
  }
}

function updateTasks() {
  const choices = new Map(knownTasks.filter(row => typeof row.id === 'string').map(row => [row.id, row]));
  if (pending) choices.set(pending.id, pending);
  if (displayed?.id && !choices.has(displayed.id)) choices.set(displayed.id, displayed);
  const selected = requested || displayed?.id;
  const select = el('task-select');
  const key = JSON.stringify([...choices].map(([id, row]) => [id, row.text]));
  if (select.dataset.key !== key) {
    select.replaceChildren(...[...choices.values()].map(row => {
      const option = document.createElement('option');
      option.value = row.id;
      option.textContent = row.text || row.id;
      return option;
    }));
    select.dataset.key = key;
  }
  select.hidden = choices.size < 2;
  if (choices.has(selected)) select.value = selected;
}

function paintEvents(mission) {
  const events = Array.isArray(mission?.events) ? mission.events : [];
  const runs = [];
  for (const event of events) {
    const previous = runs.at(-1);
    const sameObstacle = event.kind === 'obstacle' && previous?.kind === event.kind &&
      previous?.detail === event.detail && previous?.data?.operation_id === event.data?.operation_id;
    if (sameObstacle) runs[runs.length - 1] = {...event, repeats: (previous.repeats || 1) + 1};
    else runs.push(event);
  }
  const newest = runs.slice(-40).reverse();
  const signature = JSON.stringify([mission?.id, ...newest]);
  if (renderKey === signature) return;
  renderKey = signature;
  const entries = newest.map(event => {
    const li = document.createElement('li');
    li.dataset.kind = event.kind || '';
    li.dataset.seq = event.seq ?? '';
    const tone = Object.hasOwn(dynamicStates, event.kind) ? dynamicStates[event.kind][2] :
      /refocus|review|wait|anomal|interven|block|retry|fault|obstacle/i.test(event.kind) ? 'warning' : '';
    if (tone) li.dataset.tone = tone;
    const time = document.createElement('time');
    time.textContent = timestamp(event.stamp ?? event.timestamp ?? event.utc);
    const strong = document.createElement('strong');
    strong.textContent = event.label || eventNames[event.kind] || event.detail || event.kind || '任务更新';
    li.append(time, strong);
    if (event.detail && event.detail !== strong.textContent) {
      const detail = document.createElement('p');
      detail.textContent = event.repeats ? `${event.detail} · ${event.repeats} 次观测` : event.detail;
      li.append(detail);
    }
    return li;
  });
  if (!entries.length) {
    const placeholder = document.createElement('p');
    placeholder.className = 'subdued';
    placeholder.textContent = '等待任务证据';
    entries.push(placeholder);
  }
  el('events').replaceChildren(...entries);
  show('trace-info', events.length ? `${events.length} 条执行记录 · 最新在上` : '等待任务事件');
  show('event-details', events.length ? JSON.stringify(newest.slice(0, 10), null, 2) : '—');
}

function paintIntervention(mission) {
  const anomaly = mission?.anomaly;
  const actions = (Array.isArray(anomaly?.actions) ? anomaly.actions : [])
    .filter(item => item?.id === 'skip' || item?.id === 'retry');
  const waiting = actions.length > 0 &&
    !/resolved|dismissed|skipped|closed/i.test(anomaly.status || '') && Boolean(mission.id && anomaly.id);
  el('intervention').hidden = !waiting;
  if (!waiting) { anomalyKey = ''; return; }
  show('anomaly-detail', anomaly.reason || '等待人工选择处理方式');
  show('anomaly-position', pair(anomaly.xy));
  const key = JSON.stringify([mission.id, anomaly.id, actions]);
  if (key === anomalyKey) return;
  anomalyKey = key;
  show('decision-status', '');
  el('intervention-actions').replaceChildren(...actions.map(action => {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.decision = action.id;
    button.textContent = action.label || ({skip: '保留未知，继续任务', retry: '再次补扫'})[action.id];
    button.onclick = () => decide(mission.id, anomaly.id, action.id);
    return button;
  }));
}

const environmentNames = {indoor:'室内', outdoor:'室外', transition:'室内外过渡区'};

function poseZone(mission) {
  const scene = mission?.scene_geometry, pose = mission?.pose, ref = mission?.map_ref;
  if (!ref?.revision || scene?.map_ref?.id !== ref.id || scene?.map_ref?.revision !== ref.revision ||
      !pose || pose.frame !== ref.frame || scene.frame !== pose.frame || !point(xy(pose)) ||
      !Array.isArray(scene.zones)) return '';
  const matches = scene.zones.filter(zone => {
    const b = zone?.bounds;
    return Array.isArray(b) && b.length===4 && b.every(Number.isFinite) &&
      pose.x>=b[0] && pose.y>=b[1] && pose.x<b[2] && pose.y<b[3];
  });
  if (matches.length !== 1) return '';
  return [environmentNames[matches[0].kind], matches[0].label].filter(Boolean).join(' · ');
}

function paintDestination(mission) {
  const data = mission?.destination;
  const panel = el('destination-panel');
  panel.hidden = !data?.state;
  if (panel.hidden) return;
  const states = {resolved:'已确认', ambiguous:'需要区分地点', unknown:'未找到地点',
    unconfirmed:'地点待核验', unavailable:'地点暂不可用'};
  show('destination-state', states[data.state] || '等待核验');
  el('destination-state').dataset.tone = data.state === 'resolved' ? 'good' : 'warning';
  const sources = {simulation_layout_node:'校园布局登记', simulation_site_annotation:'仿真场地标注',
    gazebo_rgbd_fiducial:'仿真 RGB-D 标记观测'};
  const location = place => [environmentNames[place.environment], place.zone_label].filter(Boolean).join(' · ');
  show('destination-name', data.state === 'resolved' ? data.name : data.query);
  const messages = {ambiguous:'请在指令中补充区域或使用完整地点名。', unknown:'此地图尚未登记该地点。',
    unconfirmed:'需要核实地点和停靠位后才能规划。', unavailable:'当前地图或定位证据不足，无法确认停靠位。'};
  const details = [location(data), sources[data.source_kind] || data.source_kind,
    point(data.entrance_pose) && '停靠位 '+pair(data.entrance_pose)];
  show('destination-detail', data.state==='resolved' ? details.filter(Boolean).join(' · ') : messages[data.state] || '');
  const choices = Array.isArray(data.candidates) && data.state==='ambiguous' ? data.candidates : [];
  const list = el('destination-choices');
  list.hidden = !choices.length;
  const labels = choices.map(place => [place.name,location(place)].filter(Boolean).join(' · '));
  const key = JSON.stringify(labels);
  if (list.dataset.key!==key) {
    list.replaceChildren(...labels.map(label => {
      const item=document.createElement('li'); item.textContent=label; return item;
    }));
    list.dataset.key=key;
  }
}

function paintDynamicRecovery(mission) {
  const card = el('dynamic-recovery');
  const event = mission?.kind === 'navigate' && Array.isArray(mission.events) &&
    mission.events.findLast(item => Object.hasOwn(dynamicStates, item.kind));
  card.hidden = !event;
  if (!event) {
    recoveryKey = '';
    delete card.dataset.kind;
    return;
  }
  const key = JSON.stringify([mission.id, mission.map_ref, event]);
  if (key === recoveryKey) return;
  recoveryKey = key;
  const [label, state, tone] = dynamicStates[event.kind];
  const data = event.data || {};
  card.dataset.kind = event.kind;
  card.dataset.seq = event.seq ?? '';
  card.dataset.tone = tone;
  el('dynamic-state').dataset.tone = tone;
  show('dynamic-state', state);
  show('dynamic-detail', event.detail || label);
  const revision = mission.map_ref?.revision;
  const facts = [
    ['原导航', mission.operation_id, mission.operation_id],
    ['固定地图版本', typeof revision === 'string' ? revision.slice(0, 12) : '', revision],
    ['执行路段', data.operation_id, data.operation_id],
    ['网关实例', typeof data.gateway_boot_id === 'string' ? data.gateway_boot_id.slice(0, 12) : '', data.gateway_boot_id],
    ['通道净距', Number.isFinite(data.clearance_m) ? `${number(data.clearance_m, 3)} m` : ''],
    ['要求净距', Number.isFinite(data.required_clearance_m) ? `${number(data.required_clearance_m, 3)} m` : ''],
    ['连续清空采样', Number.isInteger(data.clear_samples) ? [data.clear_samples, data.required_samples].filter(Number.isInteger).join(' / ') : '']
  ];
  el('dynamic-facts').replaceChildren(...facts.filter(([, value]) => value).map(([name, value, full]) => {
    const row = document.createElement('div');
    const title = document.createElement('dt');
    const evidence = document.createElement('dd');
    title.textContent = name;
    evidence.textContent = value;
    if (full) evidence.title = full;
    row.append(title, evidence);
    return row;
  }));
}

function drawTrail(mission) {
  const position = xy(mission?.pose);
  if (!mission?.id || !point(position)) return [];
  if (!trails.has(mission.id)) {
    if (trails.size >= 16) trails.delete(trails.keys().next().value);
    trails.set(mission.id, []);
  }
  const trail = trails.get(mission.id);
  const previous = trail.at(-1);
  if (!previous || Math.hypot(position[0] - previous[0], position[1] - previous[1]) > .035) {
    trail.push(position);
    if (trail.length > 500) trail.shift();
  }
  return trail;
}

function mapGeometry(grid) {
  if (!grid || !Number.isInteger(grid.width) || !Number.isInteger(grid.height) ||
      grid.width <= 0 || grid.height <= 0 || grid.width * grid.height > 262144 ||
      !Array.isArray(grid.cells) || grid.cells.length !== grid.width * grid.height ||
      !Number.isFinite(grid.resolution) || grid.resolution <= 0) return null;
  const origin = Array.isArray(grid.origin) ? grid.origin : [grid.origin_x, grid.origin_y, grid.origin_yaw || 0];
  if (origin.length < 3 || !origin.every(Number.isFinite)) return null;
  const [x, y, yaw] = origin;
  const cos = Math.cos(yaw);
  const sin = Math.sin(yaw);
  return {
    toWorld: (u, v) => [x + grid.resolution * (cos * u - sin * v), y + grid.resolution * (sin * u + cos * v)],
    toGrid: ([px, py]) => [(cos * (px - x) + sin * (py - y)) / grid.resolution,
      (-sin * (px - x) + cos * (py - y)) / grid.resolution]
  };
}

function pathLine(ctx, points, project, color, width, dash = []) {
  const path = (Array.isArray(points) ? points : []).filter(point);
  if (path.length < 2) return;
  ctx.beginPath();
  path.forEach((pointValue, index) => {
    const [x, y] = project(pointValue);
    if (index === 0) ctx.moveTo(x, y);
    else ctx.lineTo(x, y);
  });
  ctx.lineWidth = width;
  ctx.strokeStyle = color;
  ctx.lineJoin = ctx.lineCap = 'round';
  ctx.setLineDash(dash);
  ctx.stroke();
  ctx.setLineDash([]);
}

function routeLine(ctx, curve, frame, project) {
  if (!curve || curve.frame !== frame || !point(curve.start) ||
      !Array.isArray(curve.segments) || !curve.segments.length ||
      !curve.segments.every(segment => point(segment?.end) &&
        (segment.control === undefined || point(segment.control)))) return false;

  ctx.beginPath();
  ctx.moveTo(...project(curve.start));
  for (const segment of curve.segments) {
    const end = project(segment.end);
    if (segment.control) ctx.quadraticCurveTo(...project(segment.control), ...end);
    else ctx.lineTo(...end);
  }
  ctx.strokeStyle = '#a5f1b8';
  ctx.lineWidth = 3;
  ctx.lineJoin = ctx.lineCap = 'round';
  ctx.stroke();
  return true;
}

function referencePath(mission) {
  const frame = mission.grid.frame;
  const previous = mission.last_navigation_path;
  const archived = mission.kind === 'navigate' && previous?.frame === frame &&
    typeof previous.operation_id === 'string' && Array.isArray(previous.points) && previous.points.length > 1 &&
    (!mission.path?.length || ['RECHECKING_CLEARANCE', 'DYNAMIC_HOLD', 'DYNAMIC_RECHECK'].includes(mission.phase));
  const points=archived ? previous.points : mission.path;
  return {archived,points:Array.isArray(points)?points:[]};
}

function globalNavigationPath(mission,plan) {
  const ref=mission?.map_ref, binding=plan?.map_ref;
  if(mission?.kind!=='navigate' || !ref?.digest || !ref.id || !ref.revision || !ref.frame || !binding ||
     typeof plan.operation_id!=='string' || !plan.operation_id.startsWith(mission.id+'/') ||
     plan.frame!==ref.frame || plan.frame!==mission.grid?.frame ||
     !['id','revision','digest','frame'].every(key=>binding[key]===ref[key]) ||
     !Array.isArray(plan.points) || plan.points.length<2 ||
     !plan.points.every(p=>point(p) && p.length===2))return null;
  return plan.points;
}

function campusRouteLayers(mission) {
  const first=globalNavigationPath(mission,mission?.initial_global_path);
  const latest=globalNavigationPath(mission,mission?.global_path);
  const live=mission?.status==='RUNNING';
  const current=live ? latest || first : first || latest;
  return {current,first,
    original:live && first && latest && mission.initial_global_path.operation_id!==mission.global_path.operation_id ? first : null,
    leg:live && mission.phase==='NAVIGATING' && mission.path?.length>1 ? mission.path : null,
    title:!live && first ? '出发时的全局规划' : '全局规划路线'};
}

function paintReferenceLine(ctx, mission, project, globalView=false) {
  const frame = mission.grid.frame;
  const {archived,points} = referencePath(mission);
  const layers=globalView && campusRouteLayers(mission);
  if(layers?.current){
    el('initial-legend').hidden=!layers.original;
    el('leg-legend').hidden=!layers.leg;
    el('route-legend').hidden=false;
    show('route-legend',layers.title);
    if(layers.original)pathLine(ctx,layers.original,project,'#a7c99d',3,[7,5]);
    pathLine(ctx,layers.current,project,'#a5f1b8',5);
    if(layers.leg)pathLine(ctx,layers.leg,project,'#f6b36e',2.5);
    return;
  }
  el('leg-legend').hidden=true;
  show('route-legend', archived ? '上次派发路线 · 历史参考' : '规划参考线');
  el('route-legend').hidden = !archived && !(mission.route_curve?.frame===frame && mission.route_curve.segments?.length) &&
    !(Array.isArray(mission.path) && mission.path.length>1);
  if (archived) pathLine(ctx,points,project,'#a7c99d',2.5,[9,6]);
  else if (!routeLine(ctx,mission.route_curve,frame,project)) pathLine(ctx,mission.path,project,'#a5f1b8',3);
}

function marker(ctx, position, project, {color, radius = 6, label = '', dashed = false, yaw, rightEdge = ctx.canvas.clientWidth}) {
  if (!point(position)) return;
  const [x, y] = project(position);
  ctx.beginPath();
  ctx.arc(x, y, radius, 0, Math.PI * 2);
  ctx.fillStyle = '#0f2118';
  ctx.strokeStyle = color;
  ctx.lineWidth = 2;
  if (dashed) ctx.setLineDash([4, 3]);
  else ctx.fill();
  ctx.stroke();
  ctx.setLineDash([]);
  if (Number.isFinite(yaw)) {
    const front = [x + 17 * Math.cos(yaw), y - 17 * Math.sin(yaw)];
    pathLine(ctx, [[x, y], front], p => p, color, 2);
  }
  if (label) {
    ctx.font = '600 13px "Microsoft YaHei", sans-serif';
    ctx.textBaseline = 'middle';
    const textWidth = ctx.measureText(label).width;
    const beside = x + radius + 6;
    const left = beside + textWidth < rightEdge - 6 ? beside : x - radius - textWidth - 6;
    ctx.strokeStyle = '#0a1813';
    ctx.lineWidth = 4;
    ctx.strokeText(label, left, y);
    ctx.fillStyle = color;
    ctx.fillText(label, left, y);
  }
}

function paintMap(mission) {
  const canvas = el('map');
  el('route-inset').hidden=true;
  el('leg-legend').hidden=true;
  el('initial-legend').hidden=true;
  const pack = campus.prepare(mission);
  if (!viewChosen && campus.info) local=false;
  const model=el('campus-model');
  el('view-campus-3d').hidden=!pack;
  el('view-task').hidden=local || !pack || !campus.registered || !validBounds(missionFocusBounds(mission));
  el('campus-caption').hidden=!pack || local;
  if(pack && !local){
    const [a,b,c,d]=pack.bounds;
    show('campus-extent',`全校 ${c-a} × ${d-b} m · ${campus.registered?'任务地图已对齐':'仅场景底图'}`);
  }
  model.hidden=!campus3d;
  canvas.hidden=campus3d;
  canvas.dataset.view = local ? 'local' : 'global';
  el('view-global').setAttribute('aria-pressed', String(!local && !campus3d));
  el('view-campus-3d').setAttribute('aria-pressed',String(campus3d));
  el('view-local').setAttribute('aria-pressed', String(local));
  const rotate=el('view-rotate');
  rotate.hidden=!local;
  document.querySelector('.map-panel h2').textContent=campus3d?'校园三维场景':local?'局部三维感知':campus.info?'全局校园地图':'任务占用地图';
  if(campus3d && pack){
    el('local-hud').hidden=el('map-empty').hidden=true;
    el('local-hint').hidden=false;
    show('local-hint','拖动旋转 · 右拖平移 · 滚轮缩放');
    campus.model(model,mission,pack);
    return;
  }
  for(const k of ['atlasKey','atlasBounds','overlayRegistered','siteOrientation'])delete canvas.dataset[k];
  const dpr = window.devicePixelRatio || 1;
  const width = canvas.clientWidth;
  const height = canvas.clientHeight;
  if (canvas.width !== Math.round(width * dpr)) canvas.width = Math.round(width * dpr);
  if (canvas.height !== Math.round(height * dpr)) canvas.height = Math.round(height * dpr);
  const ctx = canvas.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.fillStyle = '#0b1813';
  ctx.fillRect(0, 0, width, height);
  const grid = mission?.grid;
  const transform = mapGeometry(grid);
  el('local-hud').hidden = !local || !transform;
  el('local-hint').hidden = !transform;
  el('view-reset').disabled = rotate.disabled = !transform;
  const rotating = local && rotate.getAttribute('aria-pressed')==='true';
  const touch = window.matchMedia('(pointer: coarse)').matches;
  const dragHint = rotating ? '拖动旋转' : local && !touch ? '左拖平移 · 右拖旋转' : '拖动平移';
  let focusHint = '';
  if (local && cameras.local.center) focusHint = '自由视角 · ';
  else if (local && mission?.pose && mission.pose.frame===grid?.frame && point(xy(mission.pose))) focusHint = '跟随小车 · ';
  show('local-hint', focusHint + dragHint + (touch ? ' · 双指缩放' : ' · 滚轮缩放'));
  if (!local || !transform) {
    for (const key of ['depthPoints','scanPoints','sceneFeatures','observationStamp']) delete canvas.dataset[key];
  }
  const draftEmpty = mission?.coverage?.known === 0;
  if(!local && pack && width && height){
    const aligned=campus.registered && transform;
    el('local-hud').hidden=el('map-empty').hidden=true;
    el('local-hint').hidden=false;
    el('view-reset').disabled=false;
    if(!aligned)el('route-legend').hidden=true;
    drawCampusAtlas(ctx,width,height,pack,aligned?mission:null,aligned?transform:null);
    return;
  }
  if(!local && campus.info && !pack && !transform){
    el('map-empty').hidden=false; show('map-empty','正在读取完整校园纹理');return;
  }
  el('map-empty').hidden = Boolean(transform && !draftEmpty);
  show('map-empty', mission ? (draftEmpty ? '空图 · 等待首帧传感器观测' : '等待有效地图观测') : '等待探索任务开始');
  if (!transform || width<=0 || height<=0) {
    cameraView=null;
    for (const key of ['cameraX','cameraY','cameraYaw','cameraTilt','cameraZoom']) delete canvas.dataset[key];
    el('route-legend').hidden=true;
    return;
  }
  if (local) {
    renderLocalMap(ctx, width, height, mission, transform, gridTiles(grid));
    return;
  }
  const bounds = mission.bounds || mission.roi;
  const cellMin = Infinity;
  let limits = [cellMin, cellMin, -cellMin, -cellMin];
  const include = ([x, y]) => {
    limits = [Math.min(limits[0], x), Math.min(limits[1], y), Math.max(limits[2], x), Math.max(limits[3], y)];
  };
  if (Array.isArray(bounds) && bounds.length === 4 && bounds.every(Number.isFinite) &&
      bounds[0] < bounds[2] && bounds[1] < bounds[3]) {
    limits = bounds.slice();
  } else {
    [transform.toWorld(0, 0), transform.toWorld(grid.width, 0),
      transform.toWorld(grid.width, grid.height), transform.toWorld(0, grid.height)].forEach(include);
  }
  const [x0, y0, x1, y1] = limits;
  const baseScale = Math.min((width - 48) / (x1 - x0), (height - 48) / (y1 - y0));
  if (!Number.isFinite(baseScale) || baseScale <= 0) { cameraView=null; return; }
  const {scale,project} = setCameraView(canvas,'global',[(x0+x1)/2,(y0+y1)/2],baseScale,
    ([x,y])=>[clampCamera(x,x0,x1),clampCamera(y,y0,y1)]);
  ctx.save();
  const viewport = project([x0, y1]);
  ctx.beginPath();
  ctx.rect(viewport[0], viewport[1], (x1 - x0) * scale, (y1 - y0) * scale);
  ctx.clip();
  paintTaskOverlay(ctx,mission,transform,project,scale,project([x1,y1])[0]);
  ctx.restore();
  const scaleMeters = scale > 150 ? .5 : 1;
  pathLine(ctx, [[24, height - 12], [24 + scale * scaleMeters, height - 12]], p => p, '#aec1b6', 1.2);
  ctx.fillStyle = '#aec1b6';
  ctx.font = '11px system-ui';
  ctx.textBaseline = 'middle';
  ctx.fillText(`${scaleMeters} m`, 31 + scale * scaleMeters, height - 12);
}

function drawTaskGrid(ctx,grid,transform,project,opacity) {
  const p0=project(transform.toWorld(0,0)),pu=project(transform.toWorld(1,0)),pv=project(transform.toWorld(0,1));
  ctx.save();
  ctx.imageSmoothingEnabled = false;
  ctx.globalAlpha=opacity;
  ctx.transform(pu[0] - p0[0], pu[1] - p0[1], pv[0] - p0[0], pv[1] - p0[1], p0[0], p0[1]);
  ctx.drawImage(gridTiles(grid), 0, 0);
  ctx.restore();
}

function paintTaskOverlay(ctx,mission,transform,project,scale,rightEdge,gridOpacity=1) {
  const grid=mission.grid;
  const position=mission.pose?.frame===grid.frame ? xy(mission.pose) : null;
  drawTaskGrid(ctx,grid,transform,project,gridOpacity);
  const storedTrail = Array.isArray(mission.trajectory) ? mission.trajectory.filter(point) : [];
  paintReferenceLine(ctx, mission, project, true);
  pathLine(ctx, storedTrail.length ? storedTrail : drawTrail(mission), project, '#69d4e3', 2, [5, 4]);
  const issue = mission.anomaly?.xy;
  for (const poi of Array.isArray(mission.markers) ? mission.markers : []) {
    const overlapped = point(issue) && Math.hypot(issue[0] - poi.x, issue[1] - poi.y) * scale < 24;
    marker(ctx, [poi.x, poi.y], project, {color: '#ffe2a1', label: overlapped ? '' : poi.label || poi.name, rightEdge,
      dashed: !/confirm|verified/i.test(poi.status || '')});
  }
  const isNav = mission.kind === 'navigate';
  if (!isNav || !point(mission.goal) || !point(mission.frontier) ||
      Math.hypot(mission.goal[0] - mission.frontier[0], mission.goal[1] - mission.frontier[1]) * scale > 24) {
    marker(ctx, mission.frontier, project, {color: '#a5f1b8', label: isNav ? '当前局部目标' : '下一观察点', dashed: true, rightEdge});
  }
  marker(ctx, mission.goal, project, {color: '#c7edac', label: '目标停靠位', dashed: true, radius: 8, rightEdge});
  for (const review of Array.isArray(mission.history) ? mission.history : []) {
    if (review.decision === 'skip' && review.id !== mission.anomaly?.id) {
      marker(ctx, review.xy, project, {color: '#e2a070', dashed: true, radius: 9});
    }
  }
  marker(ctx, issue, project, {color: '#f2a16a', label: '待复核', dashed: true, radius: 12, rightEdge});
  marker(ctx, position, project, {color: '#85d9ee', radius: 8, yaw: mission.pose?.yaw-cameras.global.yaw});
}

function paint(mission) {
  if (displayed?.id!==mission?.id || displayed?.grid?.frame!==mission?.grid?.frame ||
      displayed?.map_ref?.id!==mission?.map_ref?.id ||
      displayed?.map_ref?.revision!==mission?.map_ref?.revision) {
    endMapGesture();
    Object.assign(cameras.local,initialCamera(true));
    Object.assign(cameras.global,initialCamera(false));
    el('view-rotate').setAttribute('aria-pressed','false');
  }
  displayed = mission;
  document.body.dataset.taskId = mission?.id || pending?.id || '';
  document.body.dataset.taskState = mission?.status || agentState?.state || '';
  document.body.dataset.mapRevision = mission?.map_ref?.revision || '';
  const rawStatus = String(mission?.status || agentState?.state || '').toUpperCase();
  const status = mission?.evidence?.map_status === 'PARTIAL' ? ['部分地图已发布', 'warning'] :
    statuses[rawStatus] || [rawStatus || '等待下发', 'neutral'];
  show('mission-status', status[0]);
  el('mission-status').dataset.tone = status[1];
  show('mission-text', mission?.text || pending?.text || agentState?.text || '等待任务');
  show('mission-id', mission?.id || pending?.id || '');
  const phase = mission?.phase || '';
  show('mission-phase', phase ? phases[phase.toLowerCase()] || phase : agentState?.detail || '连接仿真现场');
  show('clock', timestamp(mission?.updated));
  const revision = mission?.map_ref?.revision;
  show('map-source', [mission?.grid?.frame ? `坐标系 ${mission.grid.frame}` : '', revision ? `任务地图 ${revision.slice(0,12)}` : ''].filter(Boolean).join(' · ') || '等待观测帧');
  el('map-source').title = revision || '';
  const coverage = mission?.coverage;
  const known = coverage?.known;
  const total = coverage?.total;
  show('coverage', Number.isFinite(known) && total > 0 ? `任务区栅格 ${known.toLocaleString('zh-CN')} / ${total.toLocaleString('zh-CN')} 格 · ${number(100 * known / total, 1)}%` : '—');
  el('coverage').dataset.known = Number.isFinite(known) ? String(known) : '';
  show('pose', pair(xy(mission?.pose)));
  const zone = poseZone(mission);
  show('pose-zone', zone);
  el('pose-zone').hidden = !zone;
  show('speed', Number.isFinite(mission?.pose?.speed) ? `${number(mission.pose.speed, 3)} m/s` : '—');
  paintSpeedTrace(mission?.measured_speeds);
  const tracking = mission?.tracking;
  el('tracking-facts').hidden = !tracking;
  if (tracking) {
    show('tracking-mode', ({regulated_pursuit: '曲线跟踪', heading_alignment: '航向对齐'})[tracking.mode] || tracking.mode);
    show('tracking-v', `${number(tracking.speed, 2)} m/s`);
    show('tracking-w', `${number(tracking.yaw_rate, 2)} rad/s`);
    show('measured-w', Number.isFinite(mission.pose?.yaw_rate) ? `${number(mission.pose.yaw_rate, 2)} rad/s` : '—');
  }
  const isNav = mission?.kind === 'navigate';
  show('frontier-label', isNav ? '目标停靠位' : '下一观察点');
  show('frontier', pair(isNav ? mission?.goal : mission?.frontier));
  el('goal-legend').hidden = !point(mission?.goal);
  const names = (Array.isArray(mission?.markers) ? mission.markers : []).map(landmark => landmark.label || landmark.name).filter(Boolean);
  show('landmarks', [...new Set(names)].join('、') || '—');
  paintMap(mission);
  paintEvents(mission);
  paintDestination(mission);
  paintDynamicRecovery(mission);
  paintIntervention(mission);
  updateTasks();
  updateControls();
}

async function read() {
  if (reading) return;
  reading = true;
  const id = requested;
  try {
    const suffix = id ? `?task_id=${encodeURIComponent(id)}` : '';
    let snapshot;
    try { snapshot = await api('/api/exploration-mission' + suffix); }
    catch (error) {
      if (!id || id !== pending?.id) throw error;
      // During Agent planning, the provider may know the operation only after dispatch.
      snapshot = await api('/api/exploration-mission');
    }
    if (id !== requested) return;
    connected = simulation(snapshot.environment);
    show('connection', connected ? '实时仿真' : '仿真不可用');
    el('connection').dataset.tone = connected ? 'good' : 'warning';
    knownTasks = Array.isArray(snapshot.tasks) ? snapshot.tasks : [];
    const actual = snapshot.mission?.id ? snapshot.mission : null;
    const place = actual?.markers?.find(marker => marker.status === 'confirmed');
    const destination = place?.label || place?.name;
    show('simulation-area', ['Gazebo', snapshot.area || 'Rover'].join(' · '));
    for (const [id, example] of [['explore-example', snapshot.area && `探索${snapshot.area}并建图`],
      ['navigate-example', destination && `前往${destination}`]]) {
      show(id, example || '');
      el(id).hidden = !example;
    }
    const mission = connected && (!id || actual?.id === id) ? actual : null;
    if (pending && mission?.id === pending.id) {
      pending = null;
      agentState = null;
    } else if (connected && pending && !mission) {
      try {
        agentState = await api(`/agent/tasks?id=${encodeURIComponent(pending.id)}&limit=12`);
        if (!pendingStates.has(String(agentState.state || '').toUpperCase())) pending = null;
      } catch { agentState = null; }
    }
    if (id === requested) paint(mission);
  } catch (error) {
    if (id !== requested) return;
    connected = false;
    el('connection').dataset.tone = 'warning';
    show('connection', '观测连接中断');
    show('request-status', error.message);
    paintMap(displayed);
    updateControls();
  } finally { reading = false; }
}

el('task-form').onsubmit = async event => {
  event.preventDefault();
  if (el('submit-task').disabled) return;
  const text = el('task-text').value.trim();
  if (!text) return;
  const supplied = el('task-key').value;
  if (supplied && !/^[A-Za-z0-9_.-]{1,96}$/.test(supplied)) return show('request-status', '任务 ID 格式无效');
  if (!draft || draft.text !== text) draft = {text, id: supplied || `survey-${crypto.randomUUID()}`};
  submitting = true;
  updateControls();
  show('request-status', '正在提交自然语言任务…');
  try {
    const result = await api('/agent/tasks', draft);
    selectTask(result.id);
    pending = {...draft, id: result.id};
    draft = null;
    agentState = result;
    paint(null);
    el('task-text').value = '';
    el('task-key').value = '';
    show('request-status', 'Agent 已接收任务');
    read();
  } catch (error) {
    show('request-status', `下发未确认 · ${error.message}`);
  } finally { submitting = false; updateControls(); }
};

async function decide(taskId, anomalyId, decision) {
  if (!connected || deciding || displayed?.id !== taskId) return;
  deciding = true;
  updateControls();
  try {
    await api('/api/exploration-mission/intervene', {task_id: taskId, anomaly_id: anomalyId, decision});
    show('decision-status', '处理已确认，等待原任务续跑');
    read();
  } catch (error) { show('decision-status', `处理未确认 · ${error.message}`); }
  finally { deciding = false; updateControls(); }
}

el('task-select').onchange = event => { selectTask(event.target.value); read(); };
for (const button of ['explore-example', 'navigate-example']) {
  el(button).onclick = () => { el('task-text').value = el(button).textContent; el('task-text').focus(); };
}
for (const [button, localView] of [['view-global', false], ['view-local', true]]) {
  el(button).onclick = () => {
    endMapGesture();
    campus3d=false;
    viewChosen=true;
    local = localView;
    el('view-global').setAttribute('aria-pressed', String(!local));
    el('view-local').setAttribute('aria-pressed', String(local));
    paintMap(displayed);
  };
}
const endMapGesture=bindMapCamera(el('map'), () => paintMap(displayed));
el('view-campus-3d').onclick=()=>{
  endMapGesture();viewChosen=true;local=false;campus3d=true;paintMap(displayed);
};
el('view-task').onclick=()=>{
  const bounds=missionFocusBounds(displayed);
  if(!campus.registered || !validBounds(bounds))return;
  const [x,y,u,v]=bounds;
  if(campus3d){campus.focusModel(bounds);return;}
  const canvas=el('map'), [w,s,e,n]=campus.info.bounds, sideways=canvas.dataset.siteOrientation==='north-right';
  const fit=(dx,dy)=>Math.min((canvas.clientWidth-48)/(sideways?dy:dx),(canvas.clientHeight-48)/(sideways?dx:dy));
  cameras.global.center=[(x+u)/2,(y+v)/2];
  cameras.global.zoom=clampCamera(fit(u-x,v-y)/fit(e-w,n-s),.55,256);
  paintMap(displayed);
};
el('route-inset').onclick=()=>el('view-task').click();
el('view-reset').addEventListener('click',()=>{if(campus3d)campus.resetModel();});
el('show-details').onclick = () => {
  el('details').hidden = !el('details').hidden;
  el('show-details').setAttribute('aria-expanded', String(!el('details').hidden));
};
new ResizeObserver(() => paintMap(displayed)).observe(el('map'));
paint(null);
read();
setInterval(read, 600);
