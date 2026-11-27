// Analytical and VM-isolated frontend contract tests for SDD-063 live semantic map workbenches.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import test from 'node:test';
import {runInNewContext} from 'node:vm';

const require = createRequire(import.meta.url);
const calibrationMotion = require('../console/web/calibration-motion.js');
const CalibrationCorridorMap = require('../console/web/calibration-map.js');

function loadExplorationLocal() {
  const noOp = () => {};
  const created = [];
  const sandbox = {
    module: {exports: {}},
    exports: {},
    window: {devicePixelRatio: 1},
    document: {
      getElementById: () => ({hidden: false, setAttribute: noOp, getAttribute: () => 'false'}),
      createElement: () => {
        let imgData = null;
        const ctx = {
          createImageData: (w, h) => ({data: new Uint8ClampedArray(w * h * 4)}),
          putImageData: data => { imgData = data; },
        };
        const canvas = {width: 0, height: 0, getContext: () => ctx, get imageData() { return imgData; }};
        created.push(canvas);
        return canvas;
      },
    },
    requestAnimationFrame: fn => fn(),
  };
  const src = readFileSync(new URL('../console/web/exploration-local.js', import.meta.url), 'utf8');
  runInNewContext(src, sandbox);
  return {...sandbox.module.exports, created};
}

function loadConsoleEdgeMap() {
  const src = readFileSync(new URL('../console/web/map.js', import.meta.url), 'utf8');
  const sandbox = {module: {exports: {}}, exports: {}, window: undefined, document: undefined, Math, Number, Array, Object, Int32Array, Float32Array};
  runInNewContext(src, sandbox);
  return sandbox.module.exports;
}

test('calibration corridor state fuses live view_plan, historical steps, 6-stage progress and canvas hit-testing', () => {
  const station = {
    task_id: 'cal-live-1',
    state: 'CALIBRATING',
    phase: 'RECORDING',
    samples: 3,
    method: {id: 'four_hole_board', target: 'four_hole_board', min_fit: 3, min_holdout: 2},
    board_geometry: {confirmed: true},
    capture_feedback: {
      captured_lateral_left_m: 0.31,
      captured_lateral_right_m: 0.08,
      minimum_lateral_each_m: 0.28,
      captured_rgb_cells: 'top_left, middle_center',
      view_plan: {
        planner_version: '1.5.0',
        rejected: {outside_observed_clearance: 2},
        navigation_context: {
          robot: {x: 0, y: 0, yaw: 0},
          nearest_observed_m: {front: 1.8, front_left: 1.5, left: 1.2, rear: 1.4, right: 1.2, front_right: 1.5},
          target: {position_m: [1.6, 0.0, 0.45]},
        },
        scene_slice: {
          nodes: [
            {id: 'target', label: 'four_hole_board', position_m: [0, 0, 1.6]},
            {id: 'capture_zone', label: 'active_capture_corridor'},
          ],
          edges: [['target', 'guards', 'capture_zone']],
        },
        view_options: [
          {
            ref: 'view-right-low-risk',
            path: [[0, 0], [0.22, -0.38]],
            base_xy_yaw: [0.22, -0.38, 0.24],
            corridor_risk: 0.14,
            preferred_zone: 0.88,
            semantic_cost: 1.56,
            clearance_margin_m: 0.29,
            board_lateral_m: 0.34,
            new_rgb_cells: 3,
            fills_missing_side: true,
          },
        ],
      },
    },
  };
  const task = {
    id: 'cal-live-1',
    state: 'RUNNING',
    steps: [
      {tool: 'inspect_calibration_view', status: 'SUCCEEDED', operation_id: 'cal-live-1/step/1', evidence: {front_placement_confirmed: true}},
      {
        tool: 'move_to_pose',
        status: 'SUCCEEDED',
        operation_id: 'cal-live-1/step/2',
        args: {x: -0.15, y: 0.32, yaw: -0.2},
        physical_cap: {effect: 'actuation'},
        physical_pin: {class: 'physical', reservation_id: 'cal-live-1/step/2', verifier_id: 'pose_witness'},
        evidence: {outcome: 'arrived', verifier_id: 'pose_witness', verification: {operation_id: 'cal-live-1/step/2'}},
      },
    ],
  };

  const corridor = calibrationMotion.corridorState(task, station, {});
  assert.equal(corridor.visible, true);
  assert.equal(corridor.hasGuardsEdge, true);
  assert.equal(corridor.verifiedStops.length, 1);
  assert.equal(corridor.phases.length, 6);
  assert.equal(corridor.phases.find(p => p.key === 'place').status, 'done');
  assert.equal(corridor.phases.find(p => p.key === 'plan').status, 'done');
  assert.equal(corridor.phases.find(p => p.key === 'move').status, 'done');
  assert.equal(corridor.phases.find(p => p.key === 'record').status, 'active');

  const calls = [];
  const mockCtx = new Proxy({}, {get: (_, prop) => (...args) => calls.push([prop, args])});
  const mockCanvas = {clientWidth: 420, clientHeight: 240, width: 0, height: 0, getContext: () => mockCtx};
  const layout = CalibrationCorridorMap.paintCorridorCanvas(mockCanvas, corridor, 'semantic', 0);
  assert.equal(layout.mode, 'spatial');
  assert.equal(layout.candidates.length, 1);
  const summary = CalibrationCorridorMap.formatCandidateSummary(corridor.viewOptions[0], 0);
  assert.match(summary, /走廊风险 0\.14.*偏好度 0\.88.*条件代价 1\.56.*补缺侧基线/);

  // Historical fallback when station.capture_feedback is empty
  const archivedTask = {
    id: 'cal-archived',
    state: 'SUCCEEDED',
    steps: [
      {tool: 'plan_capture_views', status: 'SUCCEEDED', evidence: station.capture_feedback.view_plan},
      {tool: 'verify_sensor_extrinsics', status: 'SUCCEEDED', evidence: {verified: true, revision: 'rev-1'}},
    ],
  };
  const archivedCorridor = calibrationMotion.corridorState(
    archivedTask,
    {...station, task_id: 'cal-archived', state: 'VERIFIED', samples: 5, capture_feedback: null},
    {},
  );
  assert.equal(archivedCorridor.visible, true);
  assert.equal(archivedCorridor.viewOptions.length, 1);
  assert.ok(archivedCorridor.phases.every(p => p.status === 'done'));
});

test('exploration semantic map layers preserve default pixels, enforce monotonic obstacle veto, and flip on TTL expiry', () => {
  const {semanticCellColor, inspectSemanticCell, gridTiles} = loadExplorationLocal();
  const grid = {
    width: 4,
    height: 1,
    resolution: 0.5,
    origin_x: 0,
    origin_y: 0,
    origin_yaw: 0,
    frame: 'odom',
    cells: [-1, 0, 0, 100],
  };
  const sem = {
    source_ref: 'c'.repeat(64),
    stale: false,
    risk_block: 0.80,
    min_traversable: 0.35,
    risk_weight: 4.0,
    pref_weight: 2.5,
    // Cell 1: preferred low-risk corridor; Cell 2: semantically hazardous free space; Cell 3: geometric wall with fake 0 risk
    risk: [0.34, 0.05, 0.91, 0.0],
    traversable: [0.65, 0.96, 0.20, 1.0],
    preferred: [0.35, 0.90, 0.10, 1.0],
    summary: {robot_on_preferred: true},
  };

  // 1. Occupancy mode is pixel-identical to legacy 3-color palette
  assert.deepEqual(Array.from(semanticCellColor(-1, 0, sem, 'occupancy')), [34, 52, 46, 255]);
  assert.deepEqual(Array.from(semanticCellColor(0, 1, sem, 'occupancy')), [100, 127, 110, 255]);
  assert.deepEqual(Array.from(semanticCellColor(100, 3, sem, 'occupancy')), [191, 202, 182, 255]);

  // 2. Monotonic obstacle veto: cell 3 (occupied=100) never turns green even when semantic risk=0, preferred=1
  for (const mode of ['occupancy', 'risk', 'preferred', 'admitted']) {
    const wall = Array.from(semanticCellColor(100, 3, sem, mode));
    const freeSafe = Array.from(semanticCellColor(0, 1, sem, mode));
    assert.notDeepEqual(wall, freeSafe, `wall must remain distinct from safe corridor in ${mode}`);
    for (let i = 0; i < 4; i++) {
      const c = Array.from(semanticCellColor(grid.cells[i], i, sem, mode)).slice(0, 3);
      assert.notDeepEqual(c, [167, 232, 243], 'must not collide with local 3D vehicle landmark color');
      assert.notDeepEqual(c, [133, 217, 238], 'must not collide with planar vehicle landmark color');
    }
  }

  // 3. Fresh vs stale TTL transition on cell 2 (geometrically free, semantically high-risk)
  const freshTile = gridTiles(grid, sem, 'admitted').imageData.data;
  assert.deepEqual([...freshTile.slice(8, 12)], [222, 146, 62, 255]);
  const staleSem = {...sem, stale: true};
  const staleTile = gridTiles(grid, staleSem, 'admitted').imageData.data;
  assert.deepEqual([...staleTile.slice(8, 12)], [118, 148, 136, 255]);

  // 4. Interactive cell inspector reports ROAM channels, conditional multiplier, and TTL fallback
  const toGrid = ([x, y]) => [x / 0.5, y / 0.5];
  const freshPick = inspectSemanticCell([1.25, 0.25], {grid, semantic_costmap: sem}, {toGrid});
  assert.equal(freshPick.admitted, false);
  assert.equal(freshPick.verdict, '语义高危禁入');
  assert.ok(freshPick.multiplier > 5.5);

  const detourPick = inspectSemanticCell(
    [1.25, 0.25],
    {grid, semantic_costmap: {...sem, risk: [0.34, 0.05, 0.20, 0.0], traversable: [0.65, 0.96, 0.90, 1.0], summary: {robot_on_preferred: false}}},
    {toGrid},
  );
  const onTrailPick = inspectSemanticCell(
    [1.25, 0.25],
    {grid, semantic_costmap: {...sem, risk: [0.34, 0.05, 0.20, 0.0], traversable: [0.65, 0.96, 0.90, 1.0], summary: {robot_on_preferred: true}}},
    {toGrid},
  );
  assert.ok(onTrailPick.multiplier > detourPick.multiplier, 'off-trail penalty lifts when robot is already detouring');

  const stalePick = inspectSemanticCell([1.25, 0.25], {grid, semantic_costmap: staleSem}, {toGrid});
  assert.equal(stalePick.admitted, true);
  assert.equal(stalePick.multiplier, 1.0);
  assert.equal(stalePick.verdict, '先验过期降级放行');
});

test('main console EdgeMap computes obstacle clearance field and rotated world cursor inspection', () => {
  const EdgeMap = loadConsoleEdgeMap();
  const grid = {
    frame: 'map',
    width: 5,
    height: 5,
    resolution: 0.2,
    origin_x: 1.0,
    origin_y: 2.0,
    origin_yaw: 0,
    cells: Array(25).fill(0),
  };
  grid.cells[12] = 100; // Obstacle at center (u=2, v=2)
  const distances = EdgeMap.computeClearanceMeters(grid);
  assert.equal(distances[12], 0);
  assert.ok(Math.abs(distances[13] - 0.0) < 1e-6); // Immediate neighbor step=1 -> (1-1)*0.2 = 0m contact margin
  assert.ok(Math.abs(distances[14] - 0.2) < 1e-6); // Two cells away -> 0.20m clearance

  const state = {
    sources: {
      map: {data: grid, fresh: true},
      semantics: {
        data: {
          frame: 'map',
          zones: [{id: 'hall', label: '主走廊', bounds: [0.8, 1.8, 2.2, 3.2]}],
        },
        fresh: true,
      },
    },
  };
  const info = EdgeMap.inspectWorldPoint(1.9, 2.5, state);
  assert.equal(info.cell.u, 4);
  assert.equal(info.cell.v, 2);
  assert.match(info.text, /栅格 \(4, 2\).*自由通行 \(0\).*障碍净距 0\.20 m.*语义区 · 主走廊/);
});
