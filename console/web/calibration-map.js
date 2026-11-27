'use strict';

(function initCalibrationCorridorMap(root, factory) {
  const exported = factory();
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
  if (root) root.CalibrationCorridorMap = exported;
})(typeof window !== 'undefined' ? window : globalThis, () => {
  const finite = Number.isFinite;
  const SECTOR_ANGLES = {
    front: 0,
    front_left: Math.PI / 4,
    left: Math.PI / 2,
    rear_left: (3 * Math.PI) / 4,
    rear: Math.PI,
    rear_right: -(3 * Math.PI) / 4,
    right: -Math.PI / 2,
    front_right: -Math.PI / 4,
  };
  const RGB_CELLS = [
    ['top_left', '左上'], ['top_center', '中上'], ['top_right', '右上'],
    ['middle_left', '左中'], ['middle_center', '中心'], ['middle_right', '右中'],
    ['bottom_left', '左下'], ['bottom_center', '中下'], ['bottom_right', '右下'],
  ];
  const RELATION_LABELS = {
    guards: '守护走廊',
    attached_to: '几何附着',
    observed_by: '已观测',
    inside: '位于',
    approach_to: '接近',
    adjacent: '邻接',
  };

  function riskPalette(risk) {
    const r = Math.max(0, Math.min(1, Number(risk) || 0));
    if (r < 0.28) return {stroke: '#2ec495', fill: '#2ec49533', badge: '低风险'};
    if (r < 0.55) return {stroke: '#edb458', fill: '#edb45836', badge: '中风险'};
    return {stroke: '#e86f58', fill: '#e86f5838', badge: '高风险'};
  }

  function clearancePalette(margin) {
    const m = Number(margin);
    if (!finite(m) || m < 0.08) return {stroke: '#e86f58', fill: '#e86f5833'};
    if (m < 0.22) return {stroke: '#edb458', fill: '#edb45833'};
    return {stroke: '#53d6c4', fill: '#53d6c433'};
  }

  function geometryPalette(lateral) {
    const lat = Number(lateral) || 0;
    if (lat < -0.08) return {stroke: '#6bc5f8', fill: '#6bc5f833'};
    if (lat > 0.08) return {stroke: '#b6e36b', fill: '#b6e36b33'};
    return {stroke: '#dfd69e', fill: '#dfd69e33'};
  }

  function candidateColor(option, layerMode) {
    if (layerMode === 'clearance') return clearancePalette(option?.clearance_margin_m);
    if (layerMode === 'geometry') return geometryPalette(option?.board_lateral_m);
    return riskPalette(option?.corridor_risk);
  }

  function parseCellSet(raw) {
    const out = new Set();
    if (Array.isArray(raw)) raw.forEach(item => { if (typeof item === 'string' && item) out.add(item.trim()); });
    else if (typeof raw === 'string') {
      raw.split(',').map(s => s.trim()).filter(Boolean).forEach(s => out.add(s));
    }
    return out;
  }

  function formatCandidateSummary(option, index) {
    if (!option) return '悬停或点选候选视点查看 ROAM 风险、偏好与净距评分';
    const parts = [`候选 #${index + 1}${option.ref ? ' (' + option.ref + ')' : ''}`];
    if (Array.isArray(option.base_xy_yaw) && option.base_xy_yaw.length >= 3 && option.base_xy_yaw.every(finite)) {
      const [x, y, yaw] = option.base_xy_yaw;
      parts.push(`位姿 (${x.toFixed(2)}, ${y.toFixed(2)}) m · ${(yaw * 180 / Math.PI).toFixed(0)}°`);
    } else if (Array.isArray(option.end_xyz_m) && option.end_xyz_m.length >= 2 && option.end_xyz_m.slice(0, 2).every(finite)) {
      parts.push(`视点 (${option.end_xyz_m[0].toFixed(2)}, ${option.end_xyz_m[1].toFixed(2)}) m`);
    }
    if (finite(option.corridor_risk)) parts.push(`走廊风险 ${option.corridor_risk.toFixed(2)}`);
    if (finite(option.preferred_zone)) parts.push(`偏好度 ${option.preferred_zone.toFixed(2)}`);
    if (finite(option.semantic_cost)) parts.push(`条件代价 ${option.semantic_cost.toFixed(2)}`);
    if (finite(option.clearance_margin_m)) parts.push(`净距余量 ${option.clearance_margin_m.toFixed(2)} m`);
    if (finite(option.board_lateral_m)) parts.push(`横向视差 ${option.board_lateral_m >= 0 ? '+' : ''}${option.board_lateral_m.toFixed(2)} m`);
    if (Number.isInteger(option.new_rgb_cells) && option.new_rgb_cells > 0) parts.push(`补新格 +${option.new_rgb_cells}`);
    if (option.fills_missing_side) parts.push('补缺侧基线');
    return parts.join(' · ');
  }

  function computeCorridorLayout(width, height, state) {
    const nav = state?.navigationContext;
    const options = Array.isArray(state?.viewOptions) ? state.viewOptions : [];
    const stops = Array.isArray(state?.verifiedStops) ? state.verifiedStops : [];
    const points = [];
    const addXY = p => {
      if (Array.isArray(p) && p.length >= 2 && finite(p[0]) && finite(p[1])) points.push([p[0], p[1]]);
    };
    if (nav?.robot && finite(nav.robot.x) && finite(nav.robot.y)) {
      const rx = nav.robot.x, ry = nav.robot.y, ryaw = Number(nav.robot.yaw) || 0;
      addXY([rx, ry]);
      const sectors = nav.nearest_observed_m || {};
      for (const [name, relAngle] of Object.entries(SECTOR_ANGLES)) {
        const dist = Math.min(3.5, Math.max(0.25, Number(sectors[name]) || 0));
        if (finite(sectors[name]) && sectors[name] > 0) {
          const ang = ryaw + relAngle;
          addXY([rx + dist * Math.cos(ang), ry + dist * Math.sin(ang)]);
        }
      }
    }
    if (nav?.target?.position_m) addXY(nav.target.position_m);
    for (const opt of options) {
      if (Array.isArray(opt?.base_xy_yaw)) addXY(opt.base_xy_yaw);
      else if (Array.isArray(opt?.end_xyz_m)) addXY(opt.end_xyz_m);
      for (const wp of opt?.path || []) addXY(wp);
    }
    for (const stop of stops) addXY(stop.xy);
    if (state?.activeTargetXY) addXY(state.activeTargetXY);

    if (!points.length) {
      const target = state?.opticalTarget;
      if (target && Array.isArray(target.center) && target.center.length >= 3) {
        const tx = Number(target.center[0]) || 0;
        const tz = Math.max(0.4, Number(target.center[2]) || 1.2);
        points.push([0, 0], [tx, tz], [tx - 0.55, tz + 0.3], [tx + 0.55, tz + 0.3]);
        const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
        const minX = Math.min(...xs) - 0.35, maxX = Math.max(...xs) + 0.35;
        const minY = Math.min(-0.25, ...ys) - 0.25, maxY = Math.max(...ys) + 0.35;
        const scale = Math.min((width - 36) / Math.max(1.2, maxX - minX), (height - 34) / Math.max(1.2, maxY - minY));
        const ox = width * 0.5 - ((minX + maxX) * 0.5) * scale;
        const oy = height * 0.5 + ((minY + maxY) * 0.5) * scale;
        return {
          mode: 'optical',
          scale,
          toScreen: ([x, y]) => [ox + x * scale, oy - y * scale],
          toWorld: (sx, sy) => [(sx - ox) / scale, (oy - sy) / scale],
          candidates: [],
        };
      }
      return null;
    }

    const xs = points.map(p => p[0]), ys = points.map(p => p[1]);
    const minX = Math.min(...xs) - 0.45, maxX = Math.max(...xs) + 0.45;
    const minY = Math.min(...ys) - 0.45, maxY = Math.max(...ys) + 0.45;
    const scale = Math.min((width - 40) / Math.max(1.6, maxX - minX), (height - 36) / Math.max(1.4, maxY - minY));
    const ox = width * 0.5 - ((minX + maxX) * 0.5) * scale;
    const oy = height * 0.5 + ((minY + maxY) * 0.5) * scale;
    const toScreen = ([x, y]) => [ox + x * scale, oy - y * scale];
    const toWorld = (sx, sy) => [(sx - ox) / scale, (oy - sy) / scale];
    const candidates = options.map((option, index) => {
      const xy = Array.isArray(option?.base_xy_yaw) && option.base_xy_yaw.length >= 2
        ? [option.base_xy_yaw[0], option.base_xy_yaw[1]]
        : Array.isArray(option?.end_xyz_m) && option.end_xyz_m.length >= 2
          ? [option.end_xyz_m[0], option.end_xyz_m[1]] : null;
      if (!xy || !xy.every(finite)) return null;
      const [sx, sy] = toScreen(xy);
      const yaw = Array.isArray(option.base_xy_yaw) && finite(option.base_xy_yaw[2])
        ? option.base_xy_yaw[2]
        : finite(option.end_heading_deg) ? (option.end_heading_deg * Math.PI) / 180 : null;
      return {index, option, xy, yaw, sx, sy};
    }).filter(Boolean);
    return {mode: 'spatial', scale, ox, oy, toScreen, toWorld, candidates};
  }

  function paintCorridorCanvas(canvas, state, layerMode = 'semantic', selectedIndex = -1) {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return null;
    const dpr = Math.min((typeof window !== 'undefined' && window.devicePixelRatio) || 1, 2);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = '#11292d';
    ctx.fillRect(0, 0, w, h);

    const layout = computeCorridorLayout(w, h, state);
    if (!layout) return null;
    const {toScreen, scale} = layout;

    // Metric reference grid
    const step = [0.25, 0.5, 1, 2].find(s => s * scale >= 38) || 1;
    const [wx0, wy1] = layout.toWorld(0, 0);
    const [wx1, wy0] = layout.toWorld(w, h);
    ctx.lineWidth = 1;
    for (let x = Math.ceil(wx0 / step) * step; x <= wx1; x += step) {
      const [sx] = toScreen([x, 0]);
      ctx.strokeStyle = Math.abs(x) < 1e-6 ? '#7eb3aa55' : '#6c9c9522';
      ctx.beginPath(); ctx.moveTo(sx, 0); ctx.lineTo(sx, h); ctx.stroke();
    }
    for (let y = Math.ceil(wy0 / step) * step; y <= wy1; y += step) {
      const [, sy] = toScreen([0, y]);
      ctx.strokeStyle = Math.abs(y) < 1e-6 ? '#7eb3aa55' : '#6c9c9522';
      ctx.beginPath(); ctx.moveTo(0, sy); ctx.lineTo(w, sy); ctx.stroke();
    }

    if (layout.mode === 'optical') {
      const target = state.opticalTarget;
      const [cx, cy] = toScreen([0, 0]);
      const [tx, ty] = toScreen([target.center[0], Math.max(0.4, target.center[2])]);
      ctx.fillStyle = '#53d6c41f';
      ctx.beginPath();
      ctx.moveTo(cx, cy);
      ctx.lineTo(cx - 0.68 * scale, cy - 1.5 * scale);
      ctx.lineTo(cx + 0.68 * scale, cy - 1.5 * scale);
      ctx.closePath();
      ctx.fill();
      ctx.strokeStyle = '#53d6c477';
      ctx.stroke();

      ctx.strokeStyle = '#eff6de';
      ctx.lineWidth = 4;
      ctx.beginPath();
      ctx.moveTo(tx - 22, ty);
      ctx.lineTo(tx + 22, ty);
      ctx.stroke();
      ctx.fillStyle = '#d8f694';
      ctx.font = '600 11px system-ui, sans-serif';
      ctx.fillText(`标定板 Z=${Number(target.center[2]).toFixed(2)}m`, Math.max(8, tx - 42), Math.max(14, ty - 10));

      ctx.fillStyle = '#80e5d8';
      ctx.beginPath(); ctx.arc(cx, cy, 6, 0, Math.PI * 2); ctx.fill();
      ctx.fillStyle = '#dfece6';
      ctx.fillText('相机/雷达观测原点', Math.max(8, cx - 44), Math.min(h - 8, cy + 16));
      return layout;
    }

    const nav = state.navigationContext;
    const robot = nav?.robot;
    // 1. 8-sector LiDAR perimeter clearance envelope
    if (robot && finite(robot.x) && finite(robot.y) && nav?.nearest_observed_m) {
      const ordered = ['front', 'front_left', 'left', 'rear_left', 'rear', 'rear_right', 'right', 'front_right'];
      const poly = [];
      const ryaw = Number(robot.yaw) || 0;
      for (const key of ordered) {
        const r = Number(nav.nearest_observed_m[key]);
        if (!finite(r) || r <= 0) continue;
        const clipped = Math.min(3.6, r);
        const a = ryaw + SECTOR_ANGLES[key];
        poly.push(toScreen([robot.x + clipped * Math.cos(a), robot.y + clipped * Math.sin(a)]));
      }
      if (poly.length >= 4) {
        ctx.save();
        ctx.beginPath();
        poly.forEach(([px, py], i) => { if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py); });
        ctx.closePath();
        ctx.fillStyle = layerMode === 'clearance' ? '#49d3bf29' : '#49d3bf16';
        ctx.fill();
        ctx.strokeStyle = layerMode === 'clearance' ? '#63ead5aa' : '#63ead555';
        ctx.setLineDash([4, 4]);
        ctx.lineWidth = 1.4;
        ctx.stroke();
        ctx.restore();
      }
    }

    // 2. Active capture corridor ('guards' semantic region) + 3D board target
    const targetPos = nav?.target?.position_m;
    if (Array.isArray(targetPos) && targetPos.length >= 2 && targetPos.slice(0, 2).every(finite)) {
      const [tx, ty] = toScreen(targetPos);
      if (state.hasGuardsEdge) {
        ctx.save();
        ctx.beginPath();
        ctx.arc(tx, ty, Math.max(20, 0.85 * scale), 0, Math.PI * 2);
        ctx.fillStyle = '#b9ee7914';
        ctx.fill();
        ctx.strokeStyle = '#c8f17c66';
        ctx.setLineDash([5, 4]);
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.restore();
      }
      const faceYaw = robot && finite(robot.x) && finite(robot.y)
        ? Math.atan2(robot.y - targetPos[1], robot.x - targetPos[0]) : Math.PI;
      const bx = Math.cos(faceYaw + Math.PI / 2) * 16;
      const by = -Math.sin(faceYaw + Math.PI / 2) * 16;
      ctx.strokeStyle = '#f5f8ea';
      ctx.lineWidth = 4;
      ctx.beginPath(); ctx.moveTo(tx - bx, ty - by); ctx.lineTo(tx + bx, ty + by); ctx.stroke();
      // Board normal arrow
      const nx = tx + Math.cos(faceYaw) * 18, ny = ty - Math.sin(faceYaw) * 18;
      ctx.strokeStyle = '#d6fb80';
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(tx, ty); ctx.lineTo(nx, ny); ctx.stroke();
      ctx.fillStyle = '#eef5e4';
      ctx.font = '600 11px system-ui, sans-serif';
      ctx.fillText('四孔标定板', Math.min(w - 64, Math.max(6, tx + 8)), Math.max(14, ty - 8));
    }

    // 3. Historical verified stops
    for (const stop of state.verifiedStops || []) {
      if (!Array.isArray(stop.xy) || stop.xy.length < 2) continue;
      const [sx, sy] = toScreen(stop.xy);
      ctx.fillStyle = '#8cdcc855';
      ctx.strokeStyle = '#8cdcc8';
      ctx.lineWidth = 1.5;
      ctx.beginPath(); ctx.arc(sx, sy, 4.5, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    }

    // 4. Candidate swept corridors & ROAM scored viewpoints
    for (const item of layout.candidates) {
      const {index, option, sx, sy, yaw} = item;
      const palette = candidateColor(option, layerMode);
      const isBest = index === 0;
      const isSelected = index === selectedIndex;
      if (Array.isArray(option.path) && option.path.length >= 2) {
        ctx.save();
        ctx.beginPath();
        option.path.forEach((p, idx) => {
          if (!Array.isArray(p) || p.length < 2) return;
          const [px, py] = toScreen(p);
          if (idx === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
        });
        ctx.strokeStyle = palette.stroke;
        ctx.globalAlpha = isSelected || isBest ? 0.92 : 0.55;
        ctx.lineWidth = isSelected ? 3.2 : isBest ? 2.4 : 1.6;
        ctx.stroke();
        ctx.restore();
      }
      if (finite(option.preferred_zone) && option.preferred_zone >= 0.55) {
        ctx.beginPath();
        ctx.arc(sx, sy, isSelected ? 12 : 9.5, 0, Math.PI * 2);
        ctx.fillStyle = palette.fill;
        ctx.fill();
      }
      ctx.beginPath();
      ctx.arc(sx, sy, isSelected ? 7.5 : isBest ? 6.5 : 5.2, 0, Math.PI * 2);
      ctx.fillStyle = '#102629';
      ctx.fill();
      ctx.lineWidth = isSelected ? 2.8 : 2;
      ctx.strokeStyle = palette.stroke;
      ctx.stroke();
      if (finite(yaw)) {
        ctx.beginPath();
        ctx.moveTo(sx, sy);
        ctx.lineTo(sx + 12 * Math.cos(yaw), sy - 12 * Math.sin(yaw));
        ctx.strokeStyle = palette.stroke;
        ctx.lineWidth = 1.8;
        ctx.stroke();
      }
      ctx.fillStyle = '#eef5ea';
      ctx.font = '600 10px Consolas, monospace';
      ctx.fillText(`#${index + 1}`, sx + 7, sy - 5);
    }

    // 5. Active motion target
    if (Array.isArray(state.activeTargetXY) && state.activeTargetXY.length >= 2) {
      const [ax, ay] = toScreen(state.activeTargetXY);
      ctx.save();
      ctx.strokeStyle = '#ffcf70';
      ctx.setLineDash([3, 3]);
      ctx.lineWidth = 2;
      ctx.beginPath(); ctx.arc(ax, ay, 11, 0, Math.PI * 2); ctx.stroke();
      ctx.restore();
    }

    // 6. Live robot pose & heading
    if (robot && finite(robot.x) && finite(robot.y)) {
      const [rx, ry] = toScreen([robot.x, robot.y]);
      const ryaw = Number(robot.yaw) || 0;
      ctx.save();
      ctx.translate(rx, ry);
      ctx.rotate(-ryaw);
      ctx.beginPath();
      ctx.arc(0, 0, Math.max(10, 0.28 * scale), 0, Math.PI * 2);
      ctx.fillStyle = '#c8f17c22';
      ctx.fill();
      ctx.beginPath();
      ctx.moveTo(11, 0); ctx.lineTo(-7, -6.5); ctx.lineTo(-4, 0); ctx.lineTo(-7, 6.5);
      ctx.closePath();
      ctx.fillStyle = '#d6fb80';
      ctx.fill();
      ctx.strokeStyle = '#132d27';
      ctx.lineWidth = 1.5;
      ctx.stroke();
      ctx.restore();
    }

    // Scale legend
    const barPx = Math.min(96, step * scale);
    ctx.strokeStyle = '#bdd3cc';
    ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(12, h - 10); ctx.lineTo(12 + barPx, h - 10); ctx.stroke();
    ctx.fillStyle = '#bdd3cc';
    ctx.font = '10px Consolas, monospace';
    ctx.fillText(`${step} m`, 16 + barPx, h - 7);
    return layout;
  }

  function createBrowserController() {
    if (typeof document === 'undefined') return null;
    const byId = id => document.getElementById(id);
    let currentState = null;
    let layerMode = 'semantic';
    let selectedIdx = -1;
    let lastLayout = null;
    let bound = false;

    function updateInspection() {
      const inspectEl = byId('cal-corridor-inspect');
      if (!inspectEl) return;
      const options = currentState?.viewOptions || [];
      if (selectedIdx >= 0 && options[selectedIdx]) {
        inspectEl.textContent = formatCandidateSummary(options[selectedIdx], selectedIdx);
      } else if (options.length > 0) {
        inspectEl.textContent = '首选建议 · ' + formatCandidateSummary(options[0], 0);
      } else if (currentState?.opticalTarget) {
        const c = currentState.opticalTarget.center;
        inspectEl.textContent = `光学观测坐标系 · 板中心 (${c.map(v => Number(v).toFixed(2)).join(', ')}) m`;
      } else {
        inspectEl.textContent = '悬停或点选候选视点查看 ROAM 风险、偏好与净距评分';
      }
    }

    function repaintCanvas() {
      const canvas = byId('cal-corridor-map');
      const empty = byId('cal-corridor-empty');
      if (!canvas || !empty) return;
      if (!currentState) {
        empty.hidden = false;
        return;
      }
      lastLayout = paintCorridorCanvas(canvas, currentState, layerMode, selectedIdx);
      empty.hidden = Boolean(lastLayout);
      canvas.dataset.mode = lastLayout?.mode || 'empty';
      canvas.dataset.candidates = String(lastLayout?.candidates?.length || 0);
      canvas.dataset.layer = layerMode;
      updateInspection();
    }

    function bindOnce() {
      if (bound) return;
      const canvas = byId('cal-corridor-map');
      if (!canvas) return;
      bound = true;
      document.querySelectorAll('[data-cal-layer]').forEach(btn => {
        btn.addEventListener('click', () => {
          layerMode = btn.dataset.calLayer || 'semantic';
          document.querySelectorAll('[data-cal-layer]').forEach(other => {
            other.setAttribute('aria-pressed', String(other === btn));
          });
          repaintCanvas();
        });
      });
      const pick = event => {
        if (!lastLayout?.candidates?.length) return -1;
        const rect = canvas.getBoundingClientRect();
        const x = event.clientX - rect.left, y = event.clientY - rect.top;
        let best = -1, bestDist = 20;
        for (const c of lastLayout.candidates) {
          const d = Math.hypot(c.sx - x, c.sy - y);
          if (d < bestDist) { bestDist = d; best = c.index; }
        }
        return best;
      };
      canvas.addEventListener('pointermove', event => {
        const hit = pick(event);
        if (hit !== selectedIdx) {
          selectedIdx = hit;
          repaintCanvas();
        }
      });
      canvas.addEventListener('click', event => {
        const hit = pick(event);
        selectedIdx = hit >= 0 ? hit : (lastLayout?.candidates?.length ? 0 : -1);
        repaintCanvas();
      });
      if (typeof ResizeObserver !== 'undefined') {
        new ResizeObserver(() => { if (!byId('cal-live-workbench')?.hidden) repaintCanvas(); }).observe(canvas);
      }
    }

    function render(state) {
      const root = byId('cal-live-workbench');
      if (!root) return;
      bindOnce();
      currentState = state;
      root.hidden = !state?.visible;
      if (root.hidden) return;
      if (selectedIdx >= (state.viewOptions?.length || 0)) selectedIdx = -1;

      // 1. Stepper
      const stepper = byId('cal-phase-stepper');
      if (stepper) {
        stepper.replaceChildren(...(state.phases || []).map((p, idx) => {
          const li = document.createElement('li');
          li.dataset.stepStatus = p.status;
          const num = document.createElement('span');
          num.className = 'cal-step-num';
          num.textContent = p.status === 'done' ? '✓' : String(idx + 1);
          const lbl = document.createElement('strong');
          lbl.textContent = p.label;
          li.append(num, lbl);
          return li;
        }));
      }

      // 2. Header metadata
      const meta = byId('cal-corridor-meta');
      if (meta) {
        const bits = [];
        if (state.plannerVersion) bits.push(`规划器 v${state.plannerVersion}`);
        if (state.viewOptions?.length) bits.push(`可达候选 ${state.viewOptions.length} 个`);
        const rejTotal = Object.values(state.rejected || {}).reduce((s, n) => s + (Number.isInteger(n) ? n : 0), 0);
        if (rejTotal > 0) bits.push(`安全筛除 ${rejTotal} 个`);
        meta.textContent = bits.join(' · ') || '实时几何与语义投影';
      }

      // 3. RGB 3x3 coverage grid & lateral baseline meter
      const covered = parseCellSet(state.coveredCells);
      const missing = parseCellSet(state.missingCells);
      const gridEl = byId('cal-rgb-grid');
      if (gridEl) {
        gridEl.replaceChildren(...RGB_CELLS.map(([key, label]) => {
          const cell = document.createElement('div');
          const isCovered = covered.has(key);
          const isMissing = !isCovered && (missing.size ? missing.has(key) : true);
          cell.className = 'cal-rgb-cell';
          cell.dataset.cellState = isCovered ? 'covered' : isMissing ? 'missing' : 'neutral';
          cell.textContent = label;
          return cell;
        }));
      }
      const summaryEl = byId('cal-coverage-summary');
      if (summaryEl) {
        summaryEl.textContent = `已覆盖 ${covered.size} / 9 格 · 样本 ${state.acceptedSamples || 0} / ${state.requiredSamples || 0}`;
      }
      const meterEl = byId('cal-baseline-meter');
      const captionEl = byId('cal-baseline-caption');
      if (meterEl && captionEl) {
        const minEach = Math.max(0.15, Number(state.minimumLateralEachM) || 0.28);
        const maxSpan = Math.max(minEach * 1.4, 0.5);
        const leftM = Math.max(0, Number(state.lateralLeftM) || 0);
        const rightM = Math.max(0, Number(state.lateralRightM) || 0);
        const leftPct = Math.min(50, (leftM / maxSpan) * 50);
        const rightPct = Math.min(50, (rightM / maxSpan) * 50);
        const targetPct = Math.min(48, (minEach / maxSpan) * 50);
        meterEl.innerHTML =
          `<span class="cal-base-target" style="left:${(50 - targetPct).toFixed(1)}%"></span>` +
          `<span class="cal-base-target" style="left:${(50 + targetPct).toFixed(1)}%"></span>` +
          `<span class="cal-base-fill left" style="width:${leftPct.toFixed(1)}%;right:50%"></span>` +
          `<span class="cal-base-fill right" style="width:${rightPct.toFixed(1)}%;left:50%"></span>` +
          `<span class="cal-base-zero"></span>`;
        captionEl.textContent = finite(state.lateralLeftM) || finite(state.lateralRightM)
          ? `左偏 ${leftM.toFixed(2)} m · 右偏 ${rightM.toFixed(2)} m（门槛各 ≥ ${minEach.toFixed(2)} m）`
          : '等待多视角横向基线采样';
      }

      // 4. Semantic scene relation pills
      const sceneEl = byId('cal-scene-graph');
      if (sceneEl) {
        const scene = state.sceneSlice;
        const nodesById = new Map((scene?.nodes || []).map(n => [n.id, n]));
        const edges = Array.isArray(scene?.edges) ? scene.edges : [];
        if (edges.length) {
          sceneEl.replaceChildren(...edges.slice(0, 8).map(([src, rel, dst]) => {
            const pill = document.createElement('span');
            pill.className = 'cal-scene-pill';
            pill.dataset.relation = rel;
            const sLabel = nodesById.get(src)?.label || src;
            const dLabel = nodesById.get(dst)?.label || dst;
            pill.textContent = `${sLabel} · ${RELATION_LABELS[rel] || rel} → ${dLabel}`;
            return pill;
          }));
        } else {
          const emptyPill = document.createElement('span');
          emptyPill.className = 'cal-submeta';
          emptyPill.textContent = '尚无已配准的靶板语义子图';
          sceneEl.replaceChildren(emptyPill);
        }
      }

      repaintCanvas();
    }

    function clear() {
      currentState = null;
      selectedIdx = -1;
      const root = byId('cal-live-workbench');
      if (root) root.hidden = true;
    }

    return {render, clear};
  }

  const controller = createBrowserController();
  return {
    render: state => controller?.render(state),
    clear: () => controller?.clear(),
    computeCorridorLayout,
    paintCorridorCanvas,
    formatCandidateSummary,
    candidateColor,
    parseCellSet,
  };
});
