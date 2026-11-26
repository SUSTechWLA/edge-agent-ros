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
if (typeof module !== 'undefined') {
  module.exports=calibrationMotion;
  module.exports.resultRows=calibrationResultRows;
}
