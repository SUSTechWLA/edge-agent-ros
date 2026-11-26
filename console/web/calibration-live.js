'use strict';

// The camera view has its own lifetime. Task evidence and archived images stay in the task panel.
const CalibrationCamera = (() => {
  const image = document.getElementById('cal-preview');
  const empty = document.getElementById('cal-preview-empty');
  const caption = document.getElementById('cal-preview-caption');
  const ttlMs = 1500;
  let selected = null, enabled = false, controller = null, pollTimer, ageTimer;
  let newest = null;

  function blank(message) {
    clearTimeout(ageTimer);
    image.hidden = true;
    image.removeAttribute('src');
    delete image.dataset.stampNs;
    empty.hidden = false;
    empty.textContent = message;
    caption.textContent = newest ? newest.source + ' · 暂无实时帧' : '等候相机发布';
  }
  function showAge() {
    clearTimeout(ageTimer);
    const age = performance.now() - newest.capturedAt;
    if (age >= newest.ttl) {
      blank('帧已过期，等待相机新帧');
      return;
    }
    caption.textContent = newest.source + ' · 帧龄 ' + (age/1000).toFixed(1) + ' 秒';
    ageTimer = setTimeout(showAge, Math.min(200,newest.ttl - age));
  }
  function active() {
    return enabled && !document.hidden && navigator.onLine;
  }
  function restart() {
    clearTimeout(pollTimer);
    blank(navigator.onLine ? '等待相机新帧' : '已离线，等待重新连接');
    if (controller) controller.abort();
    else if (active()) pollTimer = setTimeout(read,0);
  }
  function connect(station) {
    if (!station?.robot_id || !station.environment) return stop();
    const key = station.robot_id + '/' + station.environment;
    if (enabled && selected?.key === key) return;
    if (selected?.key !== key) newest = null;
    selected = {key,robot:station.robot_id,environment:station.environment};
    enabled = true;
    restart();
  }
  function stop() {
    if (!enabled) return;
    enabled = false;
    restart();
  }
  function validFrame(frame) {
    const ns = value => typeof value === 'string' && /^[1-9]\d{0,19}$/.test(value);
    return frame?.source === 'live_camera' && frame.robot_id === selected.robot &&
      frame.environment === selected.environment && ns(frame.stamp_ns) && ns(frame.received_unix_ns) &&
      typeof frame.camera_id === 'string' && frame.camera_id && typeof frame.frame_id === 'string' && frame.frame_id &&
      Number.isInteger(frame.width) && Number.isInteger(frame.height) && frame.width > 0 && frame.height > 0 &&
      frame.width * frame.height <= 4096*2160 &&
      Number.isFinite(frame.max_age_ms) && frame.max_age_ms > 0 && Number.isFinite(frame.age_ms) && frame.age_ms >= 0 &&
      typeof frame.jpeg === 'string' && frame.jpeg.length > 0 && frame.jpeg.length <= 3*1024*1024;
  }
  async function read() {
    if (!active() || controller) return;
    controller = new AbortController();
    const signal = controller.signal, start = performance.now();
    const timeout = setTimeout(()=>controller?.abort(),ttlMs);
    let delay = 250;
    try {
      const r = await fetch('/api/calibration-live-frame',{cache:'no-store',signal});
      if (!r.ok) throw new Error();
      const frame = await r.json();
      if (signal.aborted || !active() || !validFrame(frame)) throw new Error();
      const stamp = BigInt(frame.stamp_ns), received = BigInt(frame.received_unix_ns);
      // The clocks of the console and the robot may differ. Include the entire request
      // round trip in the age bound. Repeated responses cannot renew a frame's lifetime.
      const capturedAt = start - frame.age_ms;
      const ttl = Math.min(ttlMs,frame.max_age_ms);
      if (performance.now()-capturedAt >= ttl) throw new Error();
      if (newest && stamp === newest.stamp && received === newest.received && !image.hidden) {
        newest.capturedAt = Math.min(newest.capturedAt,capturedAt);
        newest.ttl = Math.min(newest.ttl,ttl);
        showAge();
        return;
      }
      if (newest && (stamp <= newest.stamp || received <= newest.received)) throw new Error();
      const candidate = new Image();
      signal.addEventListener('abort',()=>candidate.removeAttribute('src'),{once:true});
      candidate.src = 'data:image/jpeg;base64,' + frame.jpeg;
      await candidate.decode();
      if (signal.aborted || !active() || performance.now()-capturedAt >= ttl ||
          candidate.naturalWidth !== frame.width || candidate.naturalHeight !== frame.height) throw new Error();
      newest = {stamp,received,capturedAt,ttl,source:frame.camera_id + ' · ' + frame.frame_id};
      image.src = candidate.src;
      image.dataset.stampNs = frame.stamp_ns;
      image.hidden = false;
      empty.hidden = true;
      showAge();
    } catch {
      blank(navigator.onLine ? '当前没有新鲜相机帧，正在重连' : '已离线，等待重新连接');
      delay = 600;
    } finally {
      clearTimeout(timeout);
      controller = null;
      if (active()) pollTimer = setTimeout(read,delay);
    }
  }
  document.addEventListener('visibilitychange',restart);
  window.addEventListener('offline',restart);
  window.addEventListener('online',restart);
  window.addEventListener('pagehide',stop);
  return {connect,stop};
})();
