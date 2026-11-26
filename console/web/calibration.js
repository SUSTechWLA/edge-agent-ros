'use strict';

const CalibrationGuide = (() => {
  const byId = id => document.getElementById(id);
  const form = byId('cal-geometry-form');
  const inputs = form.elements;
  let profileToken = null;
  let writable = false, saving = false, pausedTask = null, resuming = false;
  let reference = {};
  const text = (id, value) => {
    const element = byId(id);
    if (element.textContent !== value) element.textContent = value;
  };


  const fields = {
    markerEdge:['marker_size_m'], markerX:['marker_spacing_m',0], markerY:['marker_spacing_m',1],
    holeDiameter:['hole_diameter_m'], holeX:['hole_spacing_m',0], holeY:['hole_spacing_m',1]
  };
  function renderGeometry(board) {
    const panel = byId('cal-board');
    panel.hidden = !board?.profile_sha256;
    if (panel.hidden) return;
    reference = {};
    for (const [name,[key,index]] of Object.entries(fields)) {
      const value = board.suggested_metrics?.[key];
      const suggested = index === undefined ? value : value?.[index];
      if (Number.isFinite(suggested) && suggested>0) reference[name] = Number((suggested*1000).toFixed(4));
    }
    const hasSuggestion = !board.confirmed && !!board.suggestion_source && Object.keys(reference).length>0;
    const source = byId('cal-geometry-source');
    source.hidden = !hasSuggestion;
    text('cal-geometry-source', hasSuggestion ? '参考规格来自 ' + board.suggestion_source +
      '。请核对板的版本；选择填入后可逐项修改。缺少的尺寸请用尺测量。' : '');
    byId('cal-fill-reference').hidden = !hasSuggestion || !writable;
    for (const name of Object.keys(fields)) {
      inputs[name].placeholder = hasSuggestion && reference[name] !== undefined ? '参考 ' + reference[name] : '';
    }
    if (profileToken !== board.profile_sha256) {
      profileToken = board.profile_sha256;
      byId('cal-board-id').value = board.board_id || '';
      for (const [name,[key,index]] of Object.entries(fields)) {
        const value = index === undefined ? board.metrics?.[key] : board.metrics?.[key]?.[index];
        inputs[name].value = board.confirmed && Number.isFinite(value) ? Number((value*1000).toFixed(6)) : '';
      }
      byId('cal-confirm-measure').checked = false;
      panel.open = !board.confirmed;
      text('cal-form-note', '');
    }
    text('cal-board-status', board.confirmed ? '已登记 · ' + board.board_id : '采集前需要填写');
    for (const input of inputs) input.disabled = !writable || saving;
  }
  byId('cal-fill-reference').onclick = () => {
    if (!writable || saving || byId('cal-fill-reference').hidden) return;
    for (const [name,value] of Object.entries(reference)) {
      if (!inputs[name].value) inputs[name].value = value;
    }
    byId('cal-confirm-measure').checked = false;
    text('cal-form-note','参考规格已填入空白项。请核对各项与板编号，再确认保存。');
  };
  form.addEventListener('input', event => {
    if(event.target !== byId('cal-confirm-measure')) byId('cal-confirm-measure').checked = false;
  });

  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (!writable || saving || !profileToken || !form.reportValidity()) return;
    const id = byId('cal-board-id').value.trim();
    if (!id) { text('cal-form-note', '请填写标定板编号。'); return; }
    const m = name => inputs[name].valueAsNumber / 1000;
    const metrics = {marker_size_m:m('markerEdge'),marker_spacing_m:[m('markerX'),m('markerY')],
      hole_spacing_m:[m('holeX'),m('holeY')],hole_diameter_m:m('holeDiameter')};
    saving = true;
    byId('cal-save-board').disabled = true;
    try {
      await api('/api/calibration-board-geometry', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({board_id:id,expected_profile_sha256:profileToken,metrics,confirmed:true})});
      await refreshCalibration(true);
      text('cal-form-note', '实测尺寸已保存。');
    } catch (error) {
      const known = {overlapping_board_features:'间距必须大于对应特征的尺寸。',
        sensor_profile_changed_reload_station:'板档案已变化。请重新核对实测值。',
        board_profile_in_use_by_existing_samples:'当前任务已归档样本，请结束此次任务后再修改板档案。',
        board_profile_in_use:'当前采集中，请在本次任务结束后修改板档案。',
        invalid_four_hole_board_geometry:'请复查孔径、码边长和两组中心距。'};
      text('cal-form-note', error.status === 409 ? '工位状态已变化。请核对最新状态后重新保存。' :
        '保存失败。' + (known[error.message] || error.message));
      await refreshCalibration(true);
    } finally {
      saving = false;
      for (const input of inputs) input.disabled = !writable;
    }
  });

  byId('cal-resume').onclick = async () => {
    if (!pausedTask || !writable || resuming || !taskWritesEnabled()) return;
    const id = pausedTask;
    resuming = true;
    byId('cal-resume').disabled = true;
    try {
      const task = await readTask(id);
      if (task.id !== id || !task.can_retry) throw new Error('原任务尚未进入可续办状态。');
      const result = await api('/agent/retry', {method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({id})});
      renderTask(result);
      text('cal-resume-note', '已向原任务提交续办请求。');
      await refreshCalibration(true);
    } catch (error) { text('cal-resume-note', error.message); }
    finally { resuming=false; byId('cal-resume').disabled=false; }
  };

  function readings(pairs) {
    const list = byId('cal-readings'), elements = [];
    for (const [name,value,needsAttention] of pairs) {
      const term = document.createElement('dt'), detail = document.createElement('dd');
      if(needsAttention)detail.className='cal-adjust';
      term.textContent = name; detail.textContent = value; elements.push(term,detail);
    }
    list.replaceChildren(...elements);
  }
  const conditions = {
    board_not_visible:'未识别完整标定板', target_absent:'等待标定板入镜', no_synchronized_observation:'等待同步观测',
    repeated_pose:'姿态与已保存样本重复', new_sample_recorded:'本组已保存', distinct_pose:'检测到新姿态',
    insufficient_points:'点云支持不足', board_plane_not_found:'未检测到板平面',
    board_observed:'四孔已识别', board_missing_in_lidar:'未识别完整四孔',
    partial_markers:'本帧仅识别到部分方码', marker_geometry_mismatch:'投影与登记尺寸不符',
    board_geometry_required:'等待登记板的实测尺寸', board_moved:'检测到板姿变化，需重新举稳',
    hold_still:'正在采集稳态数据', target_visible:'已检测到标定板'
  };
  function render(status, canWrite, station, motion=null, resultRows=[]) {
    if (motion && !motion.same) status={...status,task_id:motion.id,samples:0,capture_feedback:null,revision:null,
      state:'NEEDS_CALIBRATION',phase:'STARTUP'};
    const method = status.method;
    const fourHoles = method?.geometry_type === 'four_holes' || method?.target === 'four_hole_board';
    if (fourHoles) CalibrationPreflight.refresh(station);
    else CalibrationPreflight.stop();
    const guided = fourHoles || method?.guided_capture === true;
    byId('cal-station').hidden = !guided;
    byId('cal-help').hidden = !fourHoles || !!motion;
    writable = canWrite && guided;
    const nextPaused = status.phase === 'PAUSED_FOR_OPERATOR' || status.state === 'PAUSED_FOR_OPERATOR';
    if (pausedTask !== (nextPaused ? status.task_id : null)) text('cal-resume-note','');
    pausedTask = nextPaused ? status.task_id : null;
    byId('cal-resume').hidden = !(pausedTask && writable);
    if (!guided) { CalibrationCamera.stop(); SessionView.preferCalibration(null); return; }
    CalibrationCamera.connect(station);
    renderGeometry(fourHoles ? status.board_geometry : null);
    text('cal-method', method.label || method.id);
    const taskLink = byId('cal-active-task');
    const ongoing = motion?.active || ['CALIBRATING','PAUSED_FOR_OPERATOR','RECOVERY_REQUIRED'].includes(status.state);
    SessionView.preferCalibration(ongoing ? status.task_id : null);
    taskLink.hidden = !status.task_id;
    if (status.task_id) {
      taskLink.textContent = ongoing ? '查看当前标定任务' : '查看最近标定任务';
      taskLink.href = '#sessions?' + new URLSearchParams({task:status.task_id});
    } else taskLink.removeAttribute('href');
    const feedback = status.capture_feedback || {};
    const accepted = Number.isInteger(status.samples) ? status.samples : 0;
    const configured = !fourHoles || status.board_geometry?.confirmed === true;
    const titles = {
      VERIFIED:'独立验证通过', RECOVERY_REQUIRED:'请复核此次标定',
      PAUSED_FOR_OPERATOR:'等待现场配合后继续',
      NEEDS_CALIBRATION:'准备开始采集', CALIBRATING:'请在共同视野内举稳标定板'
    };
    const phases = {RECORDED:'本组板姿已保存', ADJUST_BOARD:'调整板姿后重新采集', RECORDING:'请保持当前姿态',
      MORE_VIEWS:'请改变标定板的位置和角度', SOLVING:'正在求解外参', VERIFYING:'正在独立验证',
      CAMERA_COVERAGE:'Agent 正在检查 RGB 覆盖', WAITING_FOR_TILT:'请按提示调整标定板',
      CAMERA_SOLVING:'正在求解相机标定参数'};
    text('cal-instruction', nextPaused ? '等待现场配合后继续' : !configured ? '先登记这块标定板' :
      status.state === 'CALIBRATING' ? phases[status.phase] || titles.CALIBRATING : titles[status.state] || '读取工位状态');
    let instruction = feedback.guidance || status.message || '';
    if (!configured) instruction = '请核对板编号，按实物或该板加工图填写下方的方码和圆孔尺寸。';
    else if (status.state === 'NEEDS_CALIBRATION') instruction = '将车停稳。提交“请帮我标定当前传感器”，按取景反馈摆放板子。';
    else if (status.state === 'VERIFIED') instruction = '本次采集已完成，可以收起标定板。可下载已保存的片段复核取景。';
    else if (status.state === 'RECOVERY_REQUIRED') instruction = status.message;
    if (nextPaused) instruction = (configured ? '请按上次取景提示摆放标定板。' : '请先填写实测尺寸，将板放入共同视野。') +
      '板就位后点“续办原步骤”，已保存样本将继续使用。';
    if (motion) text('cal-instruction',motion.title);
    text('cal-guidance', motion?.detail || instruction);
    const info = motion ? [...motion.info] : [...resultRows];
    if (Array.isArray(feedback.covered_cells) && Array.isArray(feedback.missing_cells)) {
      const labels = {top:'上',middle:'中',bottom:'下',left:'左',center:'中',right:'右'};
      const tile = name => name.split('_').map(part=>labels[part]).join('');
      info.push(['RGB 九宫格', (feedback.covered_cells.length || 0)+' / 9 格']);
      if (feedback.missing_cells.length) info.push(['待覆盖区域', feedback.missing_cells.map(tile).join('、')]);
      if (Array.isArray(feedback.pitch_deg)) info.push(['已采俯仰角', feedback.pitch_deg.join('° 至 ')+'°']);
      if (Array.isArray(feedback.roll_deg)) info.push(['已采滚转角', feedback.roll_deg.join('° 至 ')+'°']);
      info.push(['训练与独立留出', feedback.train_count+' 组训练 · '+feedback.holdout_count+' 组留出']);
    }
    const reachability = feedback.capture_reachability;
    if (Number.isInteger(reachability?.useful_safe_views)) {
      info.push(['安全取景候选', reachability.evaluated_safe_views + ' 个 · 可补当前缺项 ' + reachability.useful_safe_views + ' 个']);
    }
    const bestView = feedback.capture_view_options?.[0];
    if (Number.isFinite(bestView?.intrinsic_information_gain)) {
      info.push(['首选候选的内参信息增益', bestView.intrinsic_information_gain.toFixed(3)]);
    }
    const fitted = status.camera_result;
    if (fitted?.k && fitted?.holdout_rms_px !== undefined) {
      const k = fitted.k;
      info.push(['焦距 fx / fy', k[0][0].toFixed(2)+' / '+k[1][1].toFixed(2)+' px'],
        ['主点 cx / cy', k[0][2].toFixed(2)+' / '+k[1][2].toFixed(2)+' px'],
        ['内参留出角点 RMS', fitted.holdout_rms_px.toFixed(3)+' px']);
      const front = fitted.transforms?.front;
      if (front?.validation) info.push(['外参留出孔位最大误差', (1000*front.validation.max_hole_error_m).toFixed(2)+' mm']);
    }
    if (Number.isInteger(method.min_fit) && Number.isInteger(method.min_holdout)) {
      info.push(['有效板姿', accepted + ' / 至少 ' + (method.min_fit + method.min_holdout) + ' 组（求解 ' +
        method.min_fit + '，独立验证 ' + method.min_holdout + '）']);
    }
    const camera = feedback.camera || {};
    if (Array.isArray(camera.marker_ids)) info.push(['相机方码', camera.marker_ids.length + (fourHoles ? ' / 4' : ' 个') +
      (camera.marker_ids.length ? '　ID ' + camera.marker_ids.join('、') : '')]);
    else if (Number.isInteger(camera.marker_count)) info.push(['相机方码', camera.marker_count + (fourHoles ? ' / 4' : ' 个')]);
    if (Number.isFinite(camera.reprojection_rms_px)) info.push(['角点重投影误差', camera.reprojection_rms_px.toFixed(2) + ' px']);
    const sources = feedback.streams || {};
    for (const [name,frame] of Object.entries(status.sensor_frames || {})) {
      if (!sources[name] && !feedback.lidars?.[name]) info.push([name, '已登记 ' + frame +
        (status.state === 'VERIFIED' ? '' : ' · 待取样')]);
    }
    for (const [name,stream] of Object.entries(sources)) {
      if (feedback.lidars?.[name]) continue;
      const readable = {camera:'相机图像',info:'相机内参'}[name] || name;
      const age = Number.isFinite(stream.age_s) ? ' · ' + stream.age_s.toFixed(1) + ' 秒前' : '';
      const state = stream.detail || (stream.fresh ? '已收到' : stream.received ? '采样延迟' : '尚未收到');
      info.push([readable, state + age, !stream.fresh]);
    }
    for (const [name, observation] of Object.entries(feedback.lidars || {})) {
      const detail = [];
      if (Number.isInteger(observation.hole_count)) detail.push(observation.hole_count + (fourHoles ? ' / 4 孔' : ' 个孔'));
      if (Number.isInteger(observation.point_count)) detail.push('观测窗 ' + observation.point_count + ' 点');
      if (Number.isFinite(observation.plane_rms_m)) detail.push('平面残差 ' + (1000*observation.plane_rms_m).toFixed(1) + ' mm');
      if (observation.condition) detail.push(conditions[observation.condition] || observation.condition);
      info.push([name, detail.join(' · ') || '等待当前观测',
        observation.condition === 'board_missing_in_lidar' || feedback.missing_sensors?.includes(name)]);
    }
    const clip = feedback.clip;
    if (clip && status.state === 'CALIBRATING' && Number.isInteger(clip.frames) && Number.isInteger(clip.required_frames)) {
      let progress = clip.frames + ' / ' + clip.required_frames + ' 帧';
      if (Number.isFinite(clip.duration_s) && Number.isFinite(clip.required_duration_s))
        progress += ' · ' + clip.duration_s.toFixed(1) + ' / ' + clip.required_duration_s + ' 秒';
      info.push(['本组稳态录制', progress]);
    }
    readings(info);
    const revisionLabel = fitted ? (fitted.transforms ? '内外参联合标定版本' : '内参标定版本') : '外参版本';
    const result = status.state === 'VERIFIED' && status.revision ? revisionLabel + ' ' + status.revision : conditions[feedback.condition] || '';
    text('cal-result', result);
    const download = byId('cal-download');
    download.hidden = !(status.task_id && feedback.video_available);
    if (download.hidden) download.removeAttribute('href');
    else download.href = '/api/calibration-capture.avi?' + new URLSearchParams({task_id:status.task_id});
  }
  function offline() {
    CalibrationPreflight.stop();
    writable = false;
    pausedTask = null;
    byId('cal-station').hidden = true;
    CalibrationCamera.stop();
    SessionView.preferCalibration(null);
  }
  return {render,offline};
})();
