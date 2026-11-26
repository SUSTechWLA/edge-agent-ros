'use strict';

// Display the commissioned site's motion checks. This view only reads observations.
const CalibrationPreflight = (() => {
  const byId = id => document.getElementById(id);
  const panel = byId('cal-motion');
  let stationKey = null, request = null, nextRead = 0, expiryTimer;
  let last = null, deadline = 0;

  function hide() {
    clearTimeout(expiryTimer);
    panel.hidden = true;
    last = null;
    deadline = 0;
  }
  function stop() {
    stationKey = null;
    request?.abort();
    nextRead = 0;
    hide();
  }
  function render(connected) {
    clearTimeout(expiryTimer);
    if (!last) return;
    const fresh = connected && performance.now()<deadline;
    const disposition = {
      ready:'本次预检通过', blocked:'本次预检未通过', unavailable:'本次预检结果未知'
    }[last.data.status];
    const state = byId('cal-motion-state');
    const history = {ready:'通过',blocked:'未通过',unavailable:'结果未知'}[last.data.observed_status || last.data.status];
    state.textContent = fresh ? disposition : '当前未知 · 上次预检'+history;
    state.dataset.state = fresh ? last.data.status : 'unknown';
    if (fresh) expiryTimer = setTimeout(()=>render(true),Math.min(2147483647, Math.ceil(deadline-performance.now())));
  }
  function observedMillis(value) {
    if (typeof value === 'string' && !/^[1-9]\d{0,19}$/.test(value)) return NaN;
    if (typeof value !== 'string' && typeof value !== 'number') return NaN;
    const ms = Math.floor(Number(value)/1e6);
    return Number.isSafeInteger(ms) && ms>0 && ms<=8.64e15 ? ms : NaN;
  }
  function show(data, response, started) {
    const at = observedMillis(data.observed_at_ns);
    const validFor = data.valid_for_ms === undefined ? 5000 : data.valid_for_ms;
    if (!Number.isFinite(at) || !Number.isFinite(validFor) || validFor<=0 ||
        !['blocked','ready','unavailable'].includes(data.status) || !Array.isArray(data.checks)) {
      render(false);
      return;
    }
    const now = performance.now();
    const serverDate = Date.parse(response.headers.get('Date'));
    // HTTP Date is second-granular. Use the upper end of that second and include network delay.
    const upper = Number.isFinite(serverDate) ? serverDate+1000 : Date.now();
    const age = upper-at;
    let until = data.fresh!==false && age>=0 ? now+validFor-age-(now-started) : now;
    if (last?.task === data.task_id && at<last.at) { render(false); return; }
    if (last?.task === data.task_id && at===last.at) until=Math.min(until,deadline);
    last = {at,task:data.task_id,data};
    deadline = until;
    const time = new Date(at).toLocaleString('zh-CN',{hour12:false});
    byId('cal-motion-identity').textContent = '检查于 ' + time + (data.task_id ? ' · 任务 '+data.task_id : '');
    const items = [];
    for (const check of data.checks) {
      if (typeof check?.label !== 'string') continue;
      const result = check.ok === true ? '通过' : check.ok === false ? '未通过' : '未知';
      const item = document.createElement('li');
      item.textContent = check.label+' · '+result+(check.detail ? ' · '+check.detail : '');
      items.push(item);
    }
    byId('cal-motion-checks').replaceChildren(...items);
    const test = data.last_test;
    byId('cal-motion-receipt').textContent = test === null ? '未收到测试回执' : test && typeof test === 'object'
      ? ['已收到',test.status,test.summary].filter(v=>typeof v==='string' && v).join(' · ') : '测试回执状态未知';
    panel.hidden = false;
    render(true);
  }
  async function refresh(station) {
    const key = station?.robot_id && station.environment ? station.robot_id+'/'+station.environment : null;
    if (!key) return stop();
    if (key!==stationKey) { stop(); stationKey=key; }
    if (document.hidden || request || performance.now()<nextRead) return;
    const controller = new AbortController();
    request = controller;
    const timer = setTimeout(()=>controller.abort(),2000);
    const started = performance.now();
    try {
      const response = await fetch('/api/calibration-motion-preflight',{cache:'no-store',signal:controller.signal});
      if (key!==stationKey || controller.signal.aborted) return;
      if (response.status===404) { hide(); nextRead=performance.now()+30000; return; }
      if (!response.ok) throw new Error();
      const data = await response.json();
      if (key!==stationKey || controller.signal.aborted) return;
      if (data?.schema !== 'edge.calibration.motion-preflight.v1') { hide(); nextRead=performance.now()+30000; return; }
      show(data,response,started);
    } catch {
      if (key===stationKey) render(false);
    } finally {
      clearTimeout(timer);
      request=null;
    }
  }
  window.addEventListener('offline',()=>render(false));
  return {refresh,stop};
})();
