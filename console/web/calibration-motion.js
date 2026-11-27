'use strict';

// Read the task owner's physical receipts. The observer and the browser issue no device operations.
function calibrationMotion(task, station, odom) {
  if (!task?.id || !Array.isArray(task.steps)) return null;
  const active = ['PLANNING','RUNNING','WAITING'].includes(task.state);
  const same = task.id === station.task_id;
  if (!same && (!active || (station.active_operation && station.state === 'CALIBRATING'))) return null;
  const steps = task.steps;
  const calibrationSteps=steps.filter(s=>['verify_sensor_extrinsics','verify_camera_intrinsics'].includes(s.feedback?.goal?.tool));
  const placementRequested=calibrationSteps.some(s=>s.feedback.placement_required === true);
  const declared = placementRequested || calibrationSteps.some(s=>s.feedback.required_motion_tools?.length > 0);
  if (!declared || (same && station.state === 'VERIFIED')) return null;
  const physical = steps.map((s,i)=>({s,op:task.id+'/step/'+(i+1)})).filter(({s,op})=>
    s.physical_cap?.effect === 'actuation' && s.physical_pin?.class === 'physical' &&
    s.physical_pin.reservation_id === op);
  const last = physical.at(-1);
  const e = last?.s.evidence, proof=e?.verification;
  const arrived = last?.s.status === 'SUCCEEDED' && last.s.operation_id === last.op && e?.outcome === 'arrived' &&
    proof?.operation_id === last.op && !!e.verifier_id && e.verifier_id === last.s.physical_pin.verifier_id;
  const result = {id:task.id, info:[], same, active};
  const frontConfirmed=steps.some((s,i)=>s.status === 'SUCCEEDED' && s.tool === 'inspect_calibration_view' &&
    s.operation_id === task.id+'/step/'+(i+1) && s.evidence?.front_placement_confirmed === true);
  if (placementRequested || frontConfirmed) result.info.push(['初始摆板',frontConfirmed ? '已确认正面板位' : '等待本任务的正面观测']);
  if (last) result.info.push(['换位操作',last.op]);
  if (arrived) {
    result.info.push(['到位见证', e.verifier_id]);
    if (Number.isFinite(proof.position_error_m)) result.info.push(['到点误差',proof.position_error_m.toFixed(3)+' 米']);
    if (Number.isFinite(proof.heading_error_rad)) result.info.push(['终朝向误差',(proof.heading_error_rad*180/Math.PI).toFixed(1)+'°']);
  }
  if (!active && !['SUCCEEDED','BLOCKED','RECOVERY_REQUIRED','FAILED','CANCELED'].includes(task.state)) return null;
  if (!active) {
    result.title = task.state === 'RECOVERY_REQUIRED' ? '等待核对原操作' : '查看本次任务结果';
    result.detail = task.detail || '请在任务会话中查看最后一次操作的结果。';
  } else if (placementRequested && !frontConfirmed) {
    result.title = '请将标定板立在车头正前方';
    result.detail = '板面正对车头，底边触地。请让四个方码和四个圆孔都在相机与前雷达的共同视野内，然后退到车侧。工位识别并确认正面板位后，Agent 会自动规划取景，板保持原位即可。';
  } else if (last && !arrived && task.steps.at(-1) === last.s) {
    const moving = odom?.fresh && (Math.abs(odom.data?.v)>.001 || Math.abs(odom.data?.w)>.001);
    result.title = moving ? '底盘正在自主换位' : '等待底盘换位回执';
    result.detail = '标定板保持原位。到位情况由独立运动见证确认。';
  } else if (same && station.phase==='WAITING_FOR_TILT') {
    result.title = '底盘已停稳，请按提示倾斜标定板';
    result.detail = station.capture_feedback?.guidance || station.message;
  } else if (same && ['CAMERA_VIEW_PLAN','CAMERA_COVERAGE'].includes(station.phase)) {
    result.title = station.phase==='CAMERA_VIEW_PLAN'?'Agent 正在选择标定视点':'Agent 正在检查 RGB 覆盖';
    result.detail = station.capture_feedback?.guidance || station.message;
  } else if (same && ['SOLVING','CAMERA_SOLVING','VERIFYING','RECORDING'].includes(station.phase)) {
    result.title = {SOLVING:'正在求解外参',CAMERA_SOLVING:'正在求解标定参数',VERIFYING:'正在独立验证',RECORDING:'正在录制稳态样本'}[station.phase];
    result.detail = '标定板保持原位。'+(station.phase==='RECORDING'?'工位正在核对图像、点云与姿态稳定性。':'正在处理本任务已保存的观测。');
  } else if (arrived) {
    result.title = '底盘已到位，Agent 正在复核取景';
    result.detail = '独立运动见证已确认上次换位结果。标定板可保持原位，后续采集由 Agent 根据传感器反馈决定。';
  } else if (frontConfirmed) {
    result.title = '已确认正面板位，Agent 正在规划取景';
    result.detail = '请将标定板留在原位。Agent 将根据本任务的相机和雷达观测选择采集位置。';
  } else {
    result.title = 'Agent 正在评估标定视角';
    result.detail = '标定板保持原位。Agent 正在读取现场能力和传感器反馈，选择下一步。';
  }
  return result;
}
function calibrationResultRows(task,status) {
  if (!task || task.id!==status.task_id || !status.revision || status.state!=='VERIFIED') return [];
  const record=task.steps?.find((s,i)=>s.tool==='verify_sensor_extrinsics' && s.status==='SUCCEEDED' &&
    s.operation_id===task.id+'/step/'+(i+1) && s.evidence?.verified===true && s.evidence.revision===status.revision);
  const rows=[];
  for (const tf of Object.values(record?.evidence.transforms || {})) {
    if (!tf?.parent_frame || !tf.child_frame || !Array.isArray(tf.translation_m) || tf.translation_m.length!==3 ||
        !Array.isArray(tf.rotation_xyzw) || tf.rotation_xyzw.length!==4 || ![...tf.translation_m,...tf.rotation_xyzw].every(Number.isFinite)) continue;
    rows.push(['坐标变换',tf.child_frame+' → '+tf.parent_frame],
      ['平移 x, y, z',tf.translation_m.map(v=>v.toFixed(4)).join('，')+' 米'],
      ['四元数 x, y, z, w',tf.rotation_xyzw.map(v=>v.toFixed(4)).join('，')]);
    const v=tf.validation || {};
    if (Number.isFinite(v.max_hole_error_m)) rows.push(['留出孔心最大误差',(1000*v.max_hole_error_m).toFixed(1)+' 毫米']);
    if (Number.isFinite(v.max_angle_deg)) rows.push(['留出法向最大夹角',v.max_angle_deg.toFixed(2)+'°']);
  }
  return rows;
}
function calibrationCorridorState(task, station, liveState) {
  if (!station?.method) return {visible: false};
  const sameTask = !task?.id || !station.task_id || task.id === station.task_id;
  const steps = Array.isArray(task?.steps) ? task.steps : [];
  const fb = (sameTask ? station.capture_feedback : null) || {};
  let stepPlan = null;
  let stepScene = null;
  let frontConfirmed = fb.front_placement_confirmed === true;
  let opticalCenter = null;
  const verifiedStops = [];
  let verifiedMoves = 0;
  let activeTargetXY = null;
  const knownOptionXY = new Map();

  for (let i = 0; i < steps.length; i++) {
    const s = steps[i];
    const op = task?.id ? `${task.id}/step/${i + 1}` : '';
    const ev = s?.evidence || {};
    if (s?.tool === 'inspect_calibration_view' && s.status === 'SUCCEEDED') {
      if (ev.front_placement_confirmed === true) frontConfirmed = true;
      if (ev.scene_slice) stepScene = ev.scene_slice;
      if (Array.isArray(ev.target_center_m)) opticalCenter = ev.target_center_m;
    }
    if (s?.tool === 'plan_capture_views' && s.status === 'SUCCEEDED' && ev) {
      stepPlan = ev;
      if (ev.scene_slice) stepScene = ev.scene_slice;
      for (const opt of ev.view_options || []) {
        if (opt?.ref && Array.isArray(opt.base_xy_yaw) && opt.base_xy_yaw.length >= 2) {
          knownOptionXY.set(opt.ref, [opt.base_xy_yaw[0], opt.base_xy_yaw[1]]);
        }
      }
    }
    if (s?.physical_cap?.effect === 'actuation' && s?.physical_pin?.class === 'physical') {
      const refXY = s.args?.goal_ref ? knownOptionXY.get(s.args.goal_ref) : null;
      const rawXY = Array.isArray(s.args?.xy) ? s.args.xy
        : (Number.isFinite(s.args?.x) && Number.isFinite(s.args?.y) ? [s.args.x, s.args.y] : refXY);
      if (s.status === 'SUCCEEDED' && ev.outcome === 'arrived' && ev.verification?.operation_id === op) {
        verifiedMoves++;
        if (rawXY) verifiedStops.push({op, xy: rawXY});
      } else if (['PENDING', 'RUNNING'].includes(s.status) && rawXY) {
        activeTargetXY = rawXY;
      }
    }
  }

  const livePlan = fb.view_plan || null;
  const plan = livePlan || stepPlan || null;
  const viewOptions = Array.isArray(plan?.view_options) ? plan.view_options
    : (Array.isArray(fb.capture_view_options) ? fb.capture_view_options : []);
  const navigationContext = plan?.navigation_context || fb.navigation_context || null;
  const sceneSlice = fb.scene_slice || plan?.scene_slice || stepScene || null;
  const targetNode = (sceneSlice?.nodes || []).find(n => n.id === 'target' && Array.isArray(n.position_m));
  const opticalTarget = (targetNode ? {center: targetNode.position_m, normal: targetNode.plane_normal}
    : (opticalCenter ? {center: opticalCenter, normal: null} : null));
  const hasGuardsEdge = Boolean((sceneSlice?.edges || []).some(e => Array.isArray(e) && e[1] === 'guards'));

  const acceptedSamples = Number.isInteger(station.samples) ? station.samples : 0;
  const requiredSamples = (Number(station.method.min_fit) || 0) + (Number(station.method.min_holdout) || 0);
  const verified = station.state === 'VERIFIED' || steps.some(s =>
    ['verify_sensor_extrinsics', 'verify_camera_intrinsics'].includes(s?.tool) &&
    s.status === 'SUCCEEDED' && s.evidence?.verified === true);
  const solving = ['SOLVING', 'CAMERA_SOLVING', 'VERIFYING'].includes(station.phase);
  const recording = ['RECORDING', 'ACQUIRING', 'RECORDED'].includes(station.phase) || acceptedSamples > 0;
  const moving = Boolean(activeTargetXY) || steps.some(s => s?.physical_cap?.effect === 'actuation' && ['PENDING', 'RUNNING'].includes(s.status));
  const planned = Boolean(plan) || viewOptions.length > 0;
  const observed = Boolean(sceneSlice || fb.camera?.marker_ids?.length || Object.keys(fb.lidars || {}).length || planned || acceptedSamples > 0);
  const configured = station.board_geometry ? station.board_geometry.confirmed === true : true;
  const placed = frontConfirmed || observed || verified;

  const stageFlags = [
    {key: 'place', label: '摆板确认', done: configured && placed, active: !placed && station.state === 'CALIBRATING'},
    {key: 'observe', label: '多模态观测', done: observed || verified, active: placed && !observed && !verified},
    {key: 'plan', label: 'ROAM 走廊选点', done: planned || verified, active: ['CAMERA_VIEW_PLAN', 'CAMERA_COVERAGE'].includes(station.phase)},
    {key: 'move', label: '自主换位', done: verifiedMoves > 0 || verifiedStops.length > 0 || (verified && planned), active: moving},
    {key: 'record', label: '稳态采样', done: (requiredSamples > 0 && acceptedSamples >= requiredSamples) || verified, active: !moving && !solving && !verified && (recording || station.state === 'CALIBRATING')},
    {key: 'verify', label: '求解与核验', done: verified, active: solving && !verified},
  ];
  const phases = stageFlags.map(item => ({
    key: item.key,
    label: item.label,
    status: item.done ? 'done' : item.active ? 'active' : 'pending',
  }));

  const coveredCells = fb.covered_cells || fb.captured_rgb_cells || plan?.captured_rgb_cells || [];
  const missingCells = fb.missing_cells || [];
  const lateralLeftM = fb.captured_lateral_left_m ?? plan?.captured_lateral_left_m;
  const lateralRightM = fb.captured_lateral_right_m ?? plan?.captured_lateral_right_m;
  const minimumLateralEachM = fb.minimum_lateral_each_m ?? plan?.minimum_lateral_each_m;

  const hasContent = Boolean(
    station.state === 'CALIBRATING' ||
    station.state === 'VERIFIED' ||
    navigationContext ||
    viewOptions.length ||
    sceneSlice ||
    opticalTarget ||
    acceptedSamples > 0 ||
    steps.length > 0
  );

  return {
    visible: hasContent,
    phases,
    plannerVersion: plan?.planner_version || null,
    navigationContext,
    viewOptions,
    rejected: plan?.rejected || {},
    sceneSlice,
    hasGuardsEdge,
    opticalTarget,
    verifiedStops,
    activeTargetXY,
    coveredCells,
    missingCells,
    lateralLeftM,
    lateralRightM,
    minimumLateralEachM,
    acceptedSamples,
    requiredSamples,
    livePose: liveState?.sources?.pose?.data || liveState?.sources?.odom?.data || null,
  };
}

calibrationMotion.resultRows = calibrationResultRows;
calibrationMotion.corridorState = calibrationCorridorState;
if (typeof window !== 'undefined') {
  window.calibrationCorridorState = calibrationCorridorState;
}
if (typeof module !== 'undefined') {
  module.exports=calibrationMotion;
}
