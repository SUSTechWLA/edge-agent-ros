/* The shell owns the snapshot and layer switches. Each viewer owns its camera and input. */
(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const statuses = {waiting:"等待", stale:"已过期", online:"新鲜", error:"输入异常"};
  const modes = {
    spatial: {canvas:el("viewport"), factory:window.createSpatialRenderer, name:"三维透视",
      hint:"拖动环绕 · Shift 或右键拖动平移 · 滚轮或双指缩放",
      prompt:"点击可见的点、物体或地面，读取空间坐标。"},
    planar: {canvas:el("planar-viewport"), factory:window.createPlanarRenderer, name:"二维平面 · XY",
      hint:"拖动平移 · 滚轮或双指缩放 · 点击查询地面",
      prompt:"点击地图，读取 XY 坐标、栅格和语义信息。"}
  };
  const mutedSources = new Set();
  const layers = Object.fromEntries(["cloud","scene","roof","pose","grid"].map(key => [key, el("layer-"+key)]));
  let active = new URLSearchParams(location.search).get("view")==="2d" ? "planar" : "spatial";
  let data=null, connected=false, scheduled=false, sourceSignature=null;

  function draw() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      const options = {connected, mutedSources};
      for (const [key, input] of Object.entries(layers)) options[key] = input.checked;
      const view = modes[active].renderer.render(data, options);
      el("empty").hidden = !view.empty;
      el("empty").textContent = data ? "当前可见图层没有可显示的数据" : "等待地图数据";
      const hasHeight = active==="spatial" && options.cloud && Number.isFinite(view.zMin) && Number.isFinite(view.zMax);
      el("height-key").hidden = !hasHeight;
      if (hasHeight) {
        el("z-min").textContent = view.zMin.toFixed(2);
        el("z-max").textContent = view.zMax.toFixed(2);
      }
      el("grid-key").hidden = !options.grid || !EdgeGrid.usable(data?.grid,data?.frame);
    });
  }
  function showPick() {
    const mode = modes[active];
    el("pick-result").textContent = mode.pick || mode.prompt;
    el("pick-result").classList.toggle("selected", !!mode.pick);
  }
  function clearPicks() {
    for (const mode of Object.values(modes)) {
      mode.renderer.clearPick?.();
      mode.pick = "";
    }
    showPick();
  }
  for (const [id, mode] of Object.entries(modes)) {
    mode.pick = "";
    mode.renderer = mode.factory(mode.canvas, {invalidate:draw, onPick(text) {
      mode.pick = text;
      if (active === id) showPick();
    }});
    el("mode-"+id).onclick = () => select(id);
    new ResizeObserver(draw).observe(mode.canvas);
  }
  function select(id) {
    active = id;
    const planar = id==="planar", mode = modes[id];
    for (const [key, entry] of Object.entries(modes)) {
      entry.canvas.hidden = key!==id;
      el("mode-"+key).setAttribute("aria-pressed", String(key===id));
    }
    el("view-name").textContent = mode.name;
    el("gestures").textContent = mode.hint;
    el("view-top").hidden = el("roof-row").hidden = planar;
    el("cloud-label").textContent = planar ? "点云 XY 投影" : "实测点云";
    showPick();
    draw();
  }
  el("view-top").onclick = () => modes.spatial.renderer.top();
  el("orbit-reset").onclick = () => modes[active].renderer.reset();
  el("zoom-in").onclick = () => modes[active].renderer.zoomBy(1.25);
  el("zoom-out").onclick = () => modes[active].renderer.zoomBy(1/1.25);
  for (const input of Object.values(layers)) input.onchange = () => {clearPicks(); draw();};

  function updateSources(extras) {
    const picker = el("inspect-source");
    const signature = JSON.stringify(extras.map(([id,v]) => [id,v.name,v.status,v.error]));
    if (sourceSignature === signature) return;
    sourceSignature = signature;
    for (const id of mutedSources) if (!Object.hasOwn(data.cloud_sources || {}, id)) mutedSources.delete(id);
    const selected = picker.value, list = el("lidar-sources");
    picker.replaceChildren(new Option("主点云",""));
    list.replaceChildren();
    for (const [id,v] of extras) {
      picker.add(new Option(v.name,id));
      const label=document.createElement("label"), input=document.createElement("input");
      input.type="checkbox"; input.checked=!mutedSources.has(id); label.className="sub";
      input.onchange=() => {
        if (input.checked) mutedSources.delete(id); else mutedSources.add(id);
        clearPicks();draw();
      };
      label.append(input,document.createTextNode(v.name+" · "+(statuses[v.status] || v.status)));
      list.append(label);
    }
    el("inspect-source-row").hidden=!extras.length;
    picker.value = selected && data.cloud_sources?.[selected] ? selected :
      (!data.cloud.points.length && extras.length ? extras[0][0] : "");
  }
  function update() {
    updateSources(Object.entries(data.cloud_sources || {}));
    const c = data.cloud_sources?.[el("inspect-source").value] || data.cloud;
    const g = data.grid, s = data.scene, p = data.pose;
    el("env").textContent = data.environment==="simulation" ? "GAZEBO  /  ROS 2" : "ROS 2  /  机器人";
    el("frame").textContent = data.frame+" · m";
    el("grid-info").textContent = EdgeGrid.usable(g,data.frame) ?
      "占据栅格 · "+g.width+" × "+g.height+" · "+Number((g.resolution*100).toFixed(2))+" cm/格 · "+(connected?statuses[g.status]:"断线快照") :
      "占据栅格"+(g?.error?"异常 · "+g.error:"等待数据")+"。语义和点云可独立显示。";
    el("point-count").textContent = c.points.length.toLocaleString();
    el("cloud-status").dataset.ok = String(connected && c.status==="online");
    el("cloud-status").textContent = connected ? (c.error==="no_valid_returns" && c.status==="error"?"本帧无有效回波":statuses[c.status] || c.status) : "快照";
    el("sensor").textContent = data.environment==="simulation" && c.source_frame==="map3d_survey"?"固定仿真台站 · map3d_survey":c.source_frame || "等待点云";
    el("topic").textContent = c.topic || "—";
    el("sample").textContent = c.stamp_s?.toFixed(3) ?? "—";
    el("resolution").textContent = (c.voxel_m*100).toFixed(0)+" cm";
    el("sources").textContent = "语义 "+(statuses[s.status] || "—")+(s.source?" · "+s.source:"")+"。融合位姿 "+(statuses[p.status] || "—")+"。"+(c.error?"点云诊断 "+c.error+"。":"")+(data.input_catalog_error?"清单目录 "+data.input_catalog_error:"");
    draw();
  }
  el("inspect-source").onchange=() => {if (data) update();};
  async function poll() {
    try {
      const response=await fetch("/api/map",{cache:"no-store",signal:AbortSignal.timeout(4500)});
      if (!response.ok) throw Error();
      const next = await response.json();
      if (data && (data.frame!==next.frame || data.generation!==next.generation)) {
        clearPicks();
        for (const mode of Object.values(modes)) mode.renderer.reset();
      }
      data = next;
      connected = true;
    } catch { connected = false; }
    el("connection").dataset.ok=String(connected);
    el("connection").textContent=connected?"已连接":"连接中断";
    if (data) update(); else draw();
    setTimeout(poll,1000);
  }
  select(active);
  poll();
})();
