/** Drive the public loopback Gazebo workbench, capture its actual browser frames, and preserve UI receipts. */
'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {spawn} = require('node:child_process');
const {createHash} = require('node:crypto');
const {parseArgs} = require('node:util');
const {performance} = require('node:perf_hooks');

const {values: options} = parseArgs({options: {
  url: {type: 'string', default: 'http://127.0.0.1:8910'},
  out: {type: 'string'},
  browser: {type: 'string'},
  puppeteer: {type: 'string', default: 'puppeteer-core'},
  ffmpeg: {type: 'string', default: 'ffmpeg'},
  gate: {type: 'string'},
  'explore-id': {type: 'string'},
  'resume-explore-id': {type: 'string'},
  'map-task-id': {type: 'string'},
  'navigate-id': {type: 'string'},
  place: {type: 'string'},
  'dynamic-scenario': {type: 'string'},
  'max-decisions': {type: 'string', default: '6'},
  decision: {type: 'string', default: 'skip'},
  timeout: {type: 'string', default: '600'},
  exercise: {type: 'boolean'},
  'capture-only': {type: 'boolean'},
  'abort-file': {type: 'string'},
  'stop-file': {type: 'string'}
}});

const origin = new URL(options.url).origin;
const target = new URL(options.url);
const scenario = options['dynamic-scenario'];
const scenarioPorts = {crossing: '8907', blocked: '8908', doorway: '8909'};
assert.equal(target.hostname, '127.0.0.1', 'Use the isolated loopback console');
assert.equal(target.protocol, 'http:');
if (scenario) {
  assert(Object.hasOwn(scenarioPorts, scenario), 'Unknown Gazebo scenario');
  assert(options.exercise && options['map-task-id'] && options['navigate-id'],
    'A dynamic exercise requires an explicit published map and original navigation task');
}
if (options.exercise) assert.equal(target.port, scenario ? scenarioPorts[scenario] : '8910',
  'The exercise must use a dedicated loopback Gazebo test site');
assert(options.out && options.browser, 'Pass --out and --browser');
assert(['skip', 'retry'].includes(options.decision));
assert(!options.exercise || options.gate, 'An exercise must wait for an explicit gate file');
assert(!options['capture-only'] || options['stop-file'], 'A capture needs an explicit stop file');
assert(!(options.exercise && options['capture-only']), 'Choose one mode');
assert(Number.isFinite(Number(options.timeout)) && Number(options.timeout) >= 10 && Number(options.timeout) <= 1800);
assert(!options['resume-explore-id'] || options.exercise, 'Resuming requires exercise permission');
assert(!(options['explore-id'] && options['resume-explore-id']));
assert(!options['map-task-id'] || (options.exercise && !options['explore-id'] && !options['resume-explore-id']),
  'A map task starts a navigation-only exercise');
for (const id of [options['explore-id'], options['resume-explore-id'], options['map-task-id'], options['navigate-id']]) {
  assert(!id || /^[A-Za-z0-9_.-]{1,96}$/.test(id), 'Use a valid task ID');
}
const maxDecisions = Number(options['max-decisions']);
assert(Number.isInteger(maxDecisions) && maxDecisions >= 1 && maxDecisions <= 16);
const duration = Number(options.timeout) * 1000;
const out = path.resolve(options.out);
const framesDir = path.join(out, 'frames');
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const query = id => '/api/exploration-mission' + (id ? `?task_id=${encodeURIComponent(id)}` : '');
const terminal = new Set(['SUCCEEDED', 'COMPLETE', 'PARTIAL', 'FAILED', 'BLOCKED', 'REJECTED', 'CANCELED', 'RECOVERY_REQUIRED']);
const successfulMap = new Set(['SUCCEEDED', 'COMPLETE', 'PARTIAL']);
const dynamicEvents = new Set(['dynamic_hold', 'dynamic_recheck', 'dynamic_resumed', 'dynamic_blocked']);
const dynamicChapterTitles = {
  '01-workbench': '连接 Gazebo 观测台', '05-published-map': '核对原地图并下发导航', '06-navigation': '原导航任务执行',
  '07-dynamic-hold': '动态阻挡与停车等待', '08-agent-recheck': 'Agent 复查通道',
  '09-same-task-resumed': '继续原导航任务', '09-still-blocked': '通道持续堵塞',
  '10-arrived': '到达并停稳', '10-stationary-terminal': '超时保持停车',
  '06-local-view': '拖动局部三维地图，查看实时感知', '07-unexpected-terminal': '运行终态'
};
const clean = obj => JSON.stringify(obj, null, 2) + '\n';

function run(file, args, cwd) {
  return new Promise((resolve, reject) => {
    const child = spawn(file, args, {cwd, windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
    let err = '';
    child.stderr.on('data', buffer => { err += buffer.toString(); });
    child.once('error', reject);
    child.once('close', exit => exit === 0 ? resolve() : reject(new Error(`${file} exited ${exit}\n${err}`)));
  });
}

async function exists(file) {
  try { await fs.access(file); return true; }
  catch (error) { if (error.code === 'ENOENT') return false; throw error; }
}

async function poll(fn, limit, failure) {
  while (performance.now() < limit) {
    if (options['abort-file'] && await exists(options['abort-file'])) throw new Error('Browser capture was stopped at the configured gate');
    const result = await fn();
    if (result) return result;
    await sleep(400);
  }
  throw new Error(failure);
}

async function status(id) {
  const response = await fetch(origin + query(id), {signal: AbortSignal.timeout(4200)});
  if (response.status === 404 && id) return {environment: 'simulation', mission: null};
  if (!response.ok) throw new Error(`Survey status HTTP ${response.status}`);
  const reply = await response.json();
  assert.equal(reply.environment, 'simulation');
  return reply;
}

async function idleSimulation(awaitSensors = false) {
  const response = await fetch(origin + '/api/state', {signal: AbortSignal.timeout(4200)});
  assert(response.ok, 'Read the motion guard before submitting an exercise');
  const state = await response.json();
  assert.equal(state.environment, 'simulation');
  assert.equal(state.active_operation, null, 'Start the exercise with no motion operation active');
  const fresh = state.sources?.pose?.fresh === true && state.sources?.scan?.fresh === true;
  if (!fresh && awaitSensors) return null;
  assert(fresh, 'Start the exercise with fresh simulated pose and scan');
  return {environment: state.environment, session_id: state.session_id, robot_id: state.robot_id};
}

async function execute() {
  await fs.mkdir(out, {recursive: true});
  assert(!(await exists(path.join(out, 'ready.json'))), 'Use a new capture output directory');
  await fs.mkdir(framesDir, {recursive: true});
  const Browser = require(options.puppeteer);
  const browser = await Browser.launch({
    executablePath: options.browser, headless: true,
    defaultViewport: {width: 1440, height: 900, deviceScaleFactor: 1},
    args: ['--disable-background-networking', '--disable-background-timer-throttling', '--disable-renderer-backgrounding']
  });
  const record = {source: origin, scenario: scenario || null, startedUtc: new Date().toISOString(), interactions: [], requests: [],
    observed: [], pageErrors: [], stageImages: [], outcome: 'started'};
  const outputFrames = [];
  let captureStartedAt;
  let capturing = false;
  let captureJob;
  let captureError;
  let lastObs = '';
  const page = await browser.newPage();
  page.on('pageerror', error => record.pageErrors.push(error.message));
  await page.setRequestInterception(true);
  page.on('request', request => {
    const url = new URL(request.url());
    const canWrite = options.exercise && (url.pathname === '/agent/tasks' ||
      (!scenario && url.pathname === '/api/exploration-mission/intervene'));
    if (url.origin === origin && (request.method() === 'GET' || (request.method() === 'POST' && canWrite))) {
      request.continue();
    } else request.abort();
  });
  page.on('response', async response => {
    const req = response.request();
    const url = new URL(response.url());
    if (req.method() === 'POST') {
      record.requests.push({endpoint: url.pathname, status: response.status(),
        when: new Date().toISOString(), arguments: req.postData() ? JSON.parse(req.postData()) : null});
    }
    if (url.pathname !== '/api/exploration-mission' || response.status() !== 200) return;
    try {
      const snapshot = await response.json();
      const mission = snapshot.mission;
      if (!mission) return;
      const latest = mission.events?.at(-1);
      const item = {time: new Date().toISOString(), id: mission.id, status: mission.status, phase: mission.phase,
        coverage: mission.coverage, pose: mission.pose, anomaly: mission.anomaly?.id, map_ref: mission.map_ref,
        target: mission.goal, frontier: mission.frontier, markers: mission.markers, tracking: mission.tracking,
        event: latest && {seq: latest.seq, kind: latest.kind, goal: latest.data?.goal, code: latest.data?.code,
          operationId: latest.data?.operation_id, anomalyId: latest.data?.anomaly_id},
        recovery: scenario ? mission.events?.filter(event => dynamicEvents.has(event.kind)).at(-1) : undefined};
      const key = JSON.stringify({...item, time: ''});
      if (lastObs !== key) {
        record.observed.push(item);
        lastObs = key;
      }
    } catch { /* A navigation after the response can dispose its browser context. */ }
  });

  async function stage(name) {
    const file = path.join(out, `${name}.png`);
    await page.screenshot({path: file});
    record.stageImages.push({name, time: new Date().toISOString(), seconds: (performance.now() - captureStartedAt) / 1000});
  }

  async function writeVideo() {
    if (outputFrames.length <= 1) return;
    const clip = outputFrames.map((frame, index) => {
      const time = ((outputFrames[index + 1]?.at || frame.at + 170) - frame.at) / 1000;
      return `file 'frames/${frame.file}'\nduration ${time.toFixed(4)}\n`;
    }).join('') + `file 'frames/${outputFrames.at(-1).file}'\n`;
    await fs.writeFile(path.join(out, 'capture.ffconcat'), 'ffconcat version 1.0\n' + clip);
    const file = scenario ? `dynamic-${scenario}.mp4` : 'campus-navigation.mp4';
    const args = ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '1', '-i', 'capture.ffconcat'];
    if (scenario) {
      const first = outputFrames[0].at - captureStartedAt;
      const length = Math.round(outputFrames.at(-1).at - outputFrames[0].at + 170);
      const entries = record.stageImages.filter(stage => dynamicChapterTitles[stage.name]);
      const chapters = entries.map((stage, index) => ({title: dynamicChapterTitles[stage.name],
        startMs: index ? Math.round(stage.seconds * 1000 - first) : 0}));
      const metadata = chapters.map((chapter, index) => '[CHAPTER]\nTIMEBASE=1/1000\n' +
        `START=${chapter.startMs}\nEND=${chapters[index + 1]?.startMs ?? length}\ntitle=${chapter.title}\n`).join('');
      await fs.writeFile(path.join(out, 'capture.ffmetadata'), ';FFMETADATA1\n' + metadata);
      args.push('-i', 'capture.ffmetadata', '-map_chapters', '1');
      record.chapters = chapters;
    }
    args.push('-vf', 'scale=1440:900:force_original_aspect_ratio=decrease,pad=1440:900:(ow-iw)/2:(oh-ih)/2,fps=15',
      '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', file);
    await run(options.ffmpeg, args, out);
    const bytes = await fs.readFile(path.join(out, file));
    record.video = {file, bytes: bytes.length, sha256: createHash('sha256').update(bytes).digest('hex')};
  }

  async function frameLoop() {
    while (capturing) {
      const at = performance.now();
      const file = `frame-${String(outputFrames.length + 1).padStart(6, '0')}.jpg`;
      await page.screenshot({path: path.join(framesDir, file), type: 'jpeg', quality: 76});
      outputFrames.push({at, file});
      await sleep(Math.max(0, 170 - (performance.now() - at)));
    }
  }

  async function submit(text, id) {
    await page.waitForFunction(() => !document.getElementById('submit-task').disabled);
    if (id) await page.$eval('#task-key', (input, taskId) => { input.value = taskId; }, id);
    await page.click('#task-text');
    // Insert through the page's native input path. The ordinary form still owns the submission.
    for (const character of text) {
      await page.keyboard.sendCharacter(character);
      await sleep(65);
    }
    await sleep(1300);
    const posted = page.waitForResponse(response =>
      new URL(response.url()).pathname === '/agent/tasks' && response.request().method() === 'POST');
    await page.click('#submit-task');
    const result = await posted;
    assert.equal(result.status(), 202, 'Agent task submission must receive its own real receipt');
    const receipt = await result.json();
    assert.equal(receipt.text, text);
    assert.equal(typeof receipt.id, 'string');
    if (id) assert.equal(receipt.id, id);
    record.interactions.push({kind: 'task.submitted', id: receipt.id, text, time: new Date().toISOString()});
    return receipt.id;
  }

  async function waitMission(id, predicate, limit) {
    return poll(async () => {
      let reply;
      try { reply = await status(id); }
      catch (error) {
        if (error.message.startsWith('Survey status HTTP 5') || error instanceof TypeError ||
            error.name === 'TimeoutError') return null;
        throw error;
      }
      const mission = reply.mission;
      assert(!mission || mission.id === id, 'A task-scoped status must return its own operation');
      return mission && predicate(mission) ? mission : null;
    }, limit, `Timed out waiting for mission ${id}`);
  }

  async function recordDynamicStage(id, kind, name, end, mapped, after = 0) {
    const mission = await waitMission(id, row => terminal.has(row.status) ||
      row.events?.some(event => event.kind === kind && event.seq > after), end);
    const event = mission.events?.find(item => item.kind === kind && item.seq > after);
    if (!event) await stage('07-unexpected-terminal');
    assert(event, `${kind} must be recorded before terminal ${mission.status}`);
    assert.equal(mission.operation_id, `${id}/step/1`);
    assert.equal(event.data?.task_operation_id, mission.operation_id);
    assert.deepEqual(mission.map_ref, mapped.map_ref);
    assert.deepEqual(event.data?.map_ref, mapped.map_ref);
    await page.waitForFunction((taskId, seq, revision) => document.body.dataset.taskId === taskId &&
      document.body.dataset.mapRevision === revision &&
      document.querySelector(`#events [data-seq="${seq}"]`) && !document.getElementById('dynamic-recovery').hidden,
    {timeout: 8000}, id, event.seq, mapped.map_ref.revision);
    await stage(name);
    return event.seq;
  }

  async function inspectLocalView() {
    await page.click('#view-local');
    const readCamera = () => page.$eval('#map', canvas => ({
      depthPoints: Number(canvas.dataset.depthPoints), scanPoints: Number(canvas.dataset.scanPoints),
      sceneFeatures: Number(canvas.dataset.sceneFeatures), yaw: Number(canvas.dataset.cameraYaw),
      zoom: Number(canvas.dataset.cameraZoom), speedTraceVisible: !document.querySelector('#speed-strip').hidden
    }));
    await page.waitForFunction(() => {
      const d = document.querySelector('#map').dataset;
      return Number(d.depthPoints) > 0 && Number(d.scanPoints) > 0 && Number(d.sceneFeatures) > 0
        && Number.isFinite(Number(d.cameraYaw)) && !document.querySelector('#speed-strip').hidden;
    }, {timeout: 12000});
    const before = await readCamera();
    const canvas = await page.$('#map');
    const box = await canvas.boundingBox();
    const x = box.x + box.width * .50, y = box.y + box.height * .50;
    await page.mouse.move(x, y);
    await page.keyboard.down('Shift');
    try {
      await page.mouse.down();
      await page.mouse.move(x - 50, y - 25, {steps: 16});
      await page.mouse.up();
    } finally { await page.keyboard.up('Shift'); }
    await page.mouse.wheel({deltaY: -85});
    await page.waitForFunction((start) => {
      const d = document.querySelector('#map').dataset;
      return Math.abs(Number(d.cameraYaw) - start.yaw) > .01 && Math.abs(Number(d.cameraZoom) - start.zoom) > .01;
    }, {timeout: 3000}, before);
    const after = await readCamera();
    record.interactions.push({kind: 'map.orbit_zoom', before, after, time: new Date().toISOString()});
    await page.$eval('#speed-strip', strip => strip.scrollIntoView({block: 'nearest', behavior: 'instant'}));
    await stage('06-local-view');
  }

  async function navigate(mapped, end) {
    assert(mapped.map_ref?.revision, 'The explored map must be published with its own version');
    const confirmed = (mapped.markers || []).filter(marker => marker.status === 'confirmed');
    const place = options.place || (confirmed.length === 1 && (confirmed[0].label || confirmed[0].name));
    assert(place, 'Pass --place to select the intended destination when the site contains multiple places');
    record.exploration = mapped;
    await page.waitForFunction(id => document.body.dataset.taskId === id &&
      ['SUCCEEDED', 'COMPLETE', 'PARTIAL'].includes(document.body.dataset.taskState), {}, mapped.id);
    await stage('05-published-map');
    await sleep(1700);
    const navId = await submit('前往' + place, options['navigate-id']);
    record.navigationId = navId;
    await waitMission(navId, mission => mission.path?.length > 1 || terminal.has(mission.status), end);
    await page.waitForFunction(id => document.body.dataset.taskId === id && document.body.dataset.mapRevision,
      {timeout: 8000}, navId);
    await stage('06-navigation');
    if (scenario) await inspectLocalView();
    if (scenario && scenario !== 'doorway') {
      const hold = await recordDynamicStage(navId, 'dynamic_hold', '07-dynamic-hold', end, mapped);
      const recheck = await recordDynamicStage(navId, 'dynamic_recheck', '08-agent-recheck', end, mapped, hold);
      const disposition = scenario === 'crossing' ? 'dynamic_resumed' : 'dynamic_blocked';
      const image = scenario === 'crossing' ? '09-same-task-resumed' : '09-still-blocked';
      await recordDynamicStage(navId, disposition, image, end, mapped, recheck);
    }
    const arrived = await waitMission(navId, mission => terminal.has(mission.status), end);
    record.navigation = arrived;
    assert.deepEqual(arrived.map_ref, mapped.map_ref, 'Navigation must use the recorded published map');
    await page.waitForFunction((id, state) => document.body.dataset.taskId === id &&
      document.body.dataset.taskState === state, {}, navId, arrived.status);
    const success = ['SUCCEEDED', 'COMPLETE'].includes(arrived.status);
    await stage(scenario ? (success ? '10-arrived' : '10-stationary-terminal') :
      (success ? '07-arrived' : '07-blocked-navigation'));
    if (!scenario) {
      assert(success, `Navigation ended ${arrived.status}`);
      return;
    }
    const recovery = arrived.events.filter(event => dynamicEvents.has(event.kind));
    assert.equal(arrived.operation_id, `${navId}/step/1`);
    for (const event of recovery) {
      assert.equal(event.data?.task_operation_id, arrived.operation_id);
      assert.deepEqual(event.data?.map_ref, mapped.map_ref);
      assert(event.data?.gateway_boot_id, 'Each recovery event must identify its Gateway instance');
    }
    const bootIds = [...new Set(recovery.map(event => event.data?.gateway_boot_id).filter(Boolean))];
    if (scenario === 'doorway') assert.equal(recovery.length, 0, 'Stationary door-side people caused a false stop');
    else assert.equal(bootIds.length, 1, 'Gateway identity must not change during the same navigation');
    assert(Number.isFinite(arrived.pose?.speed) && Math.abs(arrived.pose.speed) <= .02 &&
      Number.isFinite(arrived.pose.yaw_rate) && Math.abs(arrived.pose.yaw_rate) <= .05,
    'Navigation must report stationary odometry at terminal');
    if (scenario !== 'blocked') assert(success, `Navigation ended ${arrived.status}`);
    else {
      assert.equal(arrived.status, 'BLOCKED');
      assert(!recovery.some(event => event.kind === 'dynamic_resumed'), 'Persistent blockage must not resume');
    }
    assert.deepEqual(record.requests.map(item => [item.endpoint, item.status]), [['/agent/tasks', 202]],
      'Recovery must use the original navigation form submission and no intervention request');
    record.dynamicRecovery = {taskId: navId, operationId: arrived.operation_id, mapRef: mapped.map_ref, bootIds, events: recovery};
  }

  try {
    const resumeId = options['resume-explore-id'];
    const mapTaskId = options['map-task-id'];
    const openingId = resumeId || mapTaskId;
    const pageUrl = origin + '/exploration-mission.html' + (openingId ? `?task_id=${encodeURIComponent(openingId)}` : '');
    await page.goto(pageUrl, {waitUntil: 'domcontentloaded'});
    await page.waitForFunction(() => document.getElementById('connection').dataset.tone === 'good',
      {timeout: 120000});
    const initial = await status(openingId);
    if (openingId) assert.equal(initial.mission?.id, openingId);
    if (scenario) {
      assert(successfulMap.has(initial.mission?.status) && initial.mission?.map_ref?.revision,
        'Arm the recovery exercise on an already published map');
      assert(!(await status(options['navigate-id'])).mission, 'Use an unused navigation task ID');
      record.simulation = await poll(() => idleSimulation(true), performance.now() + 90000,
        'Timed out awaiting fresh simulated pose and scan');
    }
    await fs.writeFile(path.join(out, 'ready.json'), clean({origin, utc: new Date().toISOString(),
      initialTask: initial.mission?.id || null, environment: initial.environment,
      ...(scenario ? {scenario, navigationId: options['navigate-id'], mapRef: initial.mission.map_ref} : {})}));
    if (options.gate) await poll(() => exists(options.gate), performance.now() + duration, 'Capture gate not opened');
    if (scenario) {
      assert.deepEqual(await idleSimulation(), record.simulation);
      assert.deepEqual((await status(openingId)).mission?.map_ref, initial.mission.map_ref);
    }
    captureStartedAt = performance.now();
    record.captureStartedUtc = new Date().toISOString();
    capturing = true;
    captureJob = frameLoop().catch(error => { captureError = error; capturing = false; });
    await stage('01-workbench');
    await sleep(900);
    const end = performance.now() + duration;

    if (mapTaskId) {
      record.exploreId = mapTaskId;
      assert(successfulMap.has(initial.mission.status), `Source map operation ended ${initial.mission.status}`);
      await navigate(initial.mission, end);
    } else if (options.exercise) {
      assert(initial.area, 'The campus simulation must register the allowed exploration area');
      const exploreId = resumeId || await submit(`探索${initial.area}并建图`, options['explore-id']);
      record.exploreId = exploreId;
      if (!resumeId) {
        await page.waitForFunction(id => document.body.dataset.taskId === id &&
          document.getElementById('coverage').dataset.known === '0' &&
          document.querySelector('#events [data-kind="empty_map"]'), {timeout: 15000}, exploreId);
        await stage('01b-empty-map');
      }
      const progress = await waitMission(exploreId, mission => mission.coverage?.known > 0 || terminal.has(mission.status), end);
      assert(progress.coverage?.known > 0, `No observed exploration coverage: ${progress.status}`);
      await stage('02-exploring');
      const handled = new Set();
      let mapped;
      while (!mapped) {
        const assistance = await waitMission(exploreId, mission => terminal.has(mission.status) ||
          (mission.anomaly?.actions?.some(a => a.id === options.decision) && !handled.has(mission.anomaly.id)), end);
        if (terminal.has(assistance.status)) { mapped = assistance; break; }
        assert(handled.size < maxDecisions, 'Exploration exceeded the allowed browser intervention count');
        const id = assistance.anomaly.id;
        const action = `[data-decision="${options.decision}"]`;
        await page.waitForSelector(action, {visible: true, timeout: 8000});
        await stage(`03-operator-review-${handled.size + 1}`);
        await sleep(2400);
        const submitted = page.waitForResponse(response =>
          new URL(response.url()).pathname === '/api/exploration-mission/intervene'
          && response.request().method() === 'POST');
        await page.click(action);
        const response = await submitted;
        assert(response.ok(), 'The visible decision must be accepted by the provider');
        handled.add(id);
        record.interactions.push({kind: 'operator.decision', id: exploreId, anomaly: id,
          decision: options.decision, time: new Date().toISOString()});
        const resumed = await waitMission(exploreId, mission => mission.events?.some(event =>
          event.kind === 'resumed' && event.data?.anomaly_id === id && event.data.operation_id === `${exploreId}/step/1`), end);
        assert.equal(resumed.id, exploreId);
        await page.waitForFunction(taskId => document.body.dataset.taskId === taskId &&
          !!document.querySelector('#events [data-kind="resumed"]'), {timeout: 8000}, exploreId);
        await stage(`04-same-task-resumed-${handled.size}`);
      }
      assert(successfulMap.has(mapped.status), `Map operation ended ${mapped.status}`);
      await navigate(mapped, end);
    } else if (options['capture-only']) {
      await poll(() => exists(options['stop-file']), end, 'Capture stop file not signaled');
    } else {
      await page.click('#view-local');
      assert.equal(await page.$eval('#view-local', button => button.getAttribute('aria-pressed')), 'true');
      await page.click('#view-global');
      await page.click('#show-details');
      assert.equal(await page.$eval('#details', details => details.hidden), false);
      await page.setViewport({width: 430, height: 820});
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
      await stage('02-mobile');
      await page.setViewport({width: 1440, height: 900, deviceScaleFactor: 1});
    }
    await sleep(1600);
    assert.deepEqual(record.pageErrors, []);
    record.outcome = 'passed';
  } catch (error) {
    record.outcome = 'failed';
    record.error = error.message;
    if (scenario && record.navigationId && !record.navigation) {
      try { record.navigation = (await status(record.navigationId)).mission; }
      catch { /* The last successful browser responses still carry the observed evidence. */ }
    }
    if (capturing) await sleep(1200);
    throw error;
  } finally {
    capturing = false;
    await captureJob;
    await browser.close();
    record.endedUtc = new Date().toISOString();
    record.frames = outputFrames.length;
    try {
      await writeVideo();
      if (captureError) throw captureError;
    } catch (error) {
      record.outcome = 'failed';
      record.captureError = error.message;
      throw error;
    } finally {
      await fs.writeFile(path.join(out, 'browser-report.json'), clean(record));
    }
  }
  process.stdout.write(`Capture complete at ${out}\n`);
}

execute().catch(error => { process.stderr.write(error.stack + '\n'); process.exitCode = 1; });
