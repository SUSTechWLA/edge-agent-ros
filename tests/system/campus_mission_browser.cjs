/* Read-only browser acceptance against a running campus console and its published viewers. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {parseArgs} = require('node:util');

const {values: args} = parseArgs({options: Object.fromEntries(
  ['url', 'out', 'task', 'watch-task', 'browser', 'puppeteer', 'map-port', 'overview-port'].map(k => [k, {type: 'string'}]))});
const timeout = 30000;
const text = (page, selector) => page.$eval(selector, e => e.innerText.trim());
const shot = (page, out, name) => page.screenshot({path: path.join(out, name + '.png')});

async function read(url) {
  const response = await fetch(url, {signal: AbortSignal.timeout(12000)});
  assert.equal(response.status, 200, 'GET ' + url);
  return response.json();
}

async function fits(page, width) {
  const size = await page.evaluate(() => ({viewport: innerWidth, document: document.documentElement.scrollWidth}));
  assert.equal(size.viewport, width);
  assert(size.document <= width + 1, 'Horizontal scrolling at ' + width + 'px: ' + size.document);
}

// Quantize only for the paint check. Source counts, waypoint evidence and place names are checked separately.
async function colors(frame, selector) {
  return frame.$eval(selector, c => {
    const ctx = c.getContext('2d');
    if (!ctx || !c.width || !c.height) return 0;
    const data = ctx.getImageData(0, 0, c.width, c.height).data, palette = new Set();
    for (let y = 0; y < c.height; y += Math.max(1, Math.floor(c.height / 100))) {
      for (let x = 0; x < c.width; x += Math.max(1, Math.floor(c.width / 110))) {
        const i = (y * c.width + x) * 4;
        if (data[i + 3]) palette.add((data[i] >> 4) * 256 + (data[i + 1] >> 4) * 16 + (data[i + 2] >> 4));
      }
    }
    return palette.size;
  });
}

function thumbnail(frame, selector) {
  return frame.$eval(selector, c => {
    const thumbnail = document.createElement('canvas');
    thumbnail.width = 64;
    thumbnail.height = 40;
    thumbnail.getContext('2d').drawImage(c, 0, 0, thumbnail.width, thumbnail.height);
    return thumbnail.toDataURL();
  });
}

async function evidenceFor(site, requested) {
  const index = await read(new URL('/api/review/tasks?limit=100', site));
  const rows = index.tasks.filter(t => requested ? t.id === requested : t.state === 'SUCCEEDED');
  for (const row of rows) {
    const detail = await read(new URL('/api/task-detail?id=' + encodeURIComponent(row.id), site));
    const step = detail.steps?.find(s => s.tool === 'navigate_place' && s.evidence?.waypoints?.length);
    if (row.state === 'SUCCEEDED' && detail.state === row.state && step) {
      assert.equal(row.text, detail.text);
      assert.equal(step.evidence.verified_count, step.evidence.waypoints.length);
      return {row, detail, evidence: step.evidence, index};
    }
  }
  throw Error('A successful named-place task with verified waypoints must already be present in this campus.');
}

async function paintScene(page, selector, state, task) {
  await page.waitForFunction((sel, locations, waypoints) => {
    const map = document.querySelector(sel);
    return Number(map?.dataset.locations) === locations && Number(map?.dataset.routePoints) === waypoints;
  }, {timeout}, selector, state.sources.semantics.data.nodes.length, task.evidence.waypoints.length);
  const palette = await colors(page, selector);
  assert(palette >= 3, 'Visible mission map must paint several colours');
  return {registered: state.sources.semantics.data.nodes.map(node => node.id),
    waypoints: task.evidence.waypoints, measured: await page.$eval(selector, e => e.dataset.observedEnd), palette};
}

async function inspectViews(page, links, width, out) {
  const report = {};
  for (const [mode, host, selector] of [['2d', '#map2-host', '#planar-viewport'], ['3d', '#map3-host', '#viewport']]) {
    const element = await page.waitForSelector(host + ' iframe', {timeout});
    const href = await element.evaluate(e => e.src);
    const standalone = new URL(links['campus_' + mode]), embedded = new URL(href);
    const selected=new URL(page.url()).searchParams.get('task_id');
    if(selected)standalone.searchParams.set('task',selected);
    assert.equal(embedded.searchParams.get('embed'), '1');
    embedded.searchParams.delete('embed');
    assert.equal(embedded.href, standalone.href);
    assert.equal(await page.$eval(mode === '2d' ? '#map2-link' : '#map3-link', e => e.href), standalone.href);
    await element.evaluate(e => e.scrollIntoView({block: 'center'}));
    const frame = await element.contentFrame();
    await frame.waitForSelector(selector, {visible: true, timeout});
    const fraction = await frame.$eval(selector, c => {
      const bounds = c.getBoundingClientRect();
      return Math.max(0, Math.min(innerHeight, bounds.bottom) - Math.max(0, bounds.top)) / innerHeight;
    });
    assert(fraction >= .55, mode + ' canvas visible fraction of embedded window: ' + fraction);
    await frame.waitForFunction(() => document.querySelector('#place-fit')?.options.length >= 3, {timeout});
    const places = await frame.$$eval('#place-fit option', rows => rows.map(r => r.value));
    assert(places.includes('teaching') && places.includes('track'));
    await frame.select('#place-fit', 'track');
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    const track = await thumbnail(frame, selector);
    await frame.select('#place-fit', 'teaching');
    assert.equal(await frame.$eval(mode === '2d' ? '#mode-planar' : '#mode-spatial', e => e.getAttribute('aria-pressed')), 'true');
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert(track !== await thumbnail(frame, selector), mode + ' place selection must change the drawing');
    const palette = await colors(frame, selector);
    assert(palette >= 3, mode + ' canvas appears empty');
    report[mode] = {href, places, palette, visibleCanvasFraction: fraction};
    if (width === 1440) await element.screenshot({path: path.join(out, 'embedded-' + mode + '.png')});
  }
  const shown = await page.$$eval('#view-links a', rows => rows.map(a => a.href));
  const expectedReview = new URL(links.recording, page.url()).href;
  assert(shown.includes(links.overview) && shown.includes(expectedReview));
  await fits(page, width);
  return report;
}

async function paintRoutes(scope, selector, native=false) {
  return scope.$eval(selector,(canvas,native)=>{
    const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
    const key=native?document.querySelector('#mission-route-key'):null;
    const counts={global:Number(key?.dataset.global||canvas.dataset.routePoints||0),
      local:Number(key?.dataset.local||canvas.dataset.localRoutePoints||0),greenPixels:0,orangePixels:0};
    const green=native?[154,241,181]:[164,241,191], orange=native?[255,179,92]:[255,155,84];
    for(let i=0;i<data.length;i+=4){
      if(data[i]===green[0]&&data[i+1]===green[1]&&data[i+2]===green[2])counts.greenPixels++;
      if(data[i]===orange[0]&&data[i+1]===orange[1]&&data[i+2]===orange[2])counts.orangePixels++;
    }
    return counts;
  },native);
}

async function nativeFrame(page,host,task){
  const iframe=await page.waitForSelector('#'+host+'-host iframe',{timeout});
  const frame=await iframe.contentFrame();
  await frame.waitForFunction(id=>document.querySelector('#mission-route-key')?.dataset.task===id &&
    document.querySelector('#connection')?.dataset.ok==='true',{timeout},task);
  return frame;
}

async function inspectLocalCamera(page, taskId, out) {
  await page.click('#local-3d');
  const frame = await nativeFrame(page, 'local3', taskId);
  const canvas = await frame.waitForSelector('#viewport', {visible: true});
  await frame.waitForFunction(() => Number(document.querySelector('#point-count').textContent.replaceAll(',', '')) > 0, {timeout});
  await page.waitForSelector('#local3-hint', {visible: true});
  assert.equal(new URL(await page.$eval('#local3-link', a => a.href)).searchParams.get('local'), '1');
  await canvas.scrollIntoView();
  const {x, y, width, height} = await canvas.boundingBox();
  const cx = x + width / 2, cy = y + height / 2;
  async function pixels() {
    await frame.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    return thumbnail(frame, '#viewport');
  }
  async function drag(button, dx, dy) {
    await page.mouse.move(cx, cy);
    await page.mouse.down({button});
    await page.mouse.move(cx + dx, cy + dy, {steps: 6});
    await page.mouse.up({button});
  }
  const start = await pixels();
  await drag('left', 48, 16);
  const rotated = await pixels();
  assert.notEqual(rotated, start, 'Left drag must orbit the point cloud');
  await drag('right', 32, -17);
  const panned = await pixels();
  assert.notEqual(panned, rotated, 'Right drag must pan the point cloud');
  await page.mouse.wheel({deltaY: -150});
  const zoomed = await pixels();
  assert.notEqual(zoomed, panned, 'Wheel must zoom the point cloud');
  await page.waitForResponse(r => new URL(r.url()).pathname === '/api/map' && r.request().frame() === frame);
  assert.equal(await pixels(), zoomed, 'Polling the same observation must preserve the camera');
  await page.click('#local-2d');
  await page.waitForSelector('#local3-hint', {hidden: true});
  await page.click('#local-3d');
  assert.equal(await pixels(), zoomed, 'Switching back to 3D must preserve the camera');
  await page.$eval('#local3-host', e => e.scrollIntoView({block: 'center'}));
  await shot(page, out, 'local-3d-camera');
  await frame.click('#orbit-reset');
  assert.notEqual(await pixels(), zoomed, 'The fit button must restore an overview');
  await page.click('#local3-link');
  await page.waitForFunction(() => document.fullscreenElement?.id === 'local3-host', {timeout});
  await page.evaluate(() => document.exitFullscreen());
  await page.click('#local-2d');
  return {orbit: true, pan: true, zoom: true, fit: true, fullscreen: true, retainedOnPollAndTabSwitch: true};
}

async function observeMotion(site, id, out) {
  const pageUrl = new URL('/campus-mission.html', site);
  pageUrl.searchParams.set('task_id', id);
  const [views, before] = await Promise.all([read(new URL('/api/campus-mission/views', site)), read(new URL('/api/state', site))]);
  assert.equal(before.environment, 'simulation');
  const origins = new Set([site.origin, new URL(views.campus_2d).origin, new URL(views.overview).origin]);
  assert(!origins.has('http://127.0.0.1:8766'), 'Use an isolated simulator');
  fs.mkdirSync(out, {recursive: true});
  const browser = await require(args.puppeteer || 'puppeteer-core').launch({executablePath: args.browser, headless: true,
    args: ['--no-first-run', '--disable-background-networking'], defaultViewport: {width: 1440, height: 1640, deviceScaleFactor: 1}});
  const log = {passed: false, task: id, source: pageUrl.href, samples: [], deniedRequests: []};
  const file = path.join(out, 'browser-motion.json');
  const persist = () => fs.writeFileSync(file, JSON.stringify(log, null, 2) + '\n');
  try {
    const page = await browser.newPage();
    let last = null, preview=null;
    await page.setRequestInterception(true);
    page.on('request', req => {
      const url = new URL(req.url());
      if (!['about:', 'blob:', 'data:'].includes(url.protocol) && (req.method() !== 'GET' || !origins.has(url.origin))) {
        log.deniedRequests.push(req.method() + ' ' + url);
        return req.abort();
      }
      return req.continue();
    });
    page.on('response', async r => {
      if(!r.ok())return;
      const url=new URL(r.url());
      try{
        if(url.href===site.origin+'/api/state')last=await r.json();
        else if(url.origin===site.origin && url.pathname==='/api/campus-mission/local-preview' && url.searchParams.get('task_id')===id)preview=await r.json();
      }catch{}
    });
    await page.goto(pageUrl.href, {waitUntil: 'domcontentloaded'});
    const [map2,map3]=await Promise.all([nativeFrame(page,'map2',id),nativeFrame(page,'map3',id)]);
    await page.click('#local-3d');
    const local3=await nativeFrame(page,'local3',id);
    assert.equal(await page.$eval('#local-3d',e=>e.getAttribute('aria-pressed')),'true');
    assert.equal(new URL(local3.url()).searchParams.get('local'),'1');
    await page.click('#local-2d');
    await page.evaluate(()=>scrollTo(0,0));
    const finish = Date.now() + 600000;
    let lastStamp, start, moving, routed, toggled;
    while (Date.now() < finish) {
      await new Promise(resolve => setTimeout(resolve, 400));
      if (!last || last.now === lastStamp || !last.sources.odom.fresh) continue;
      const sample = last;
      const display = await page.evaluate(() => {
        const q = id => document.getElementById(id);
        const facts = Object.fromEntries([...q('robot-facts').children].map(c =>
          [c.querySelector('dt').textContent, c.querySelector('dd').textContent]));
        return {id: q('task-choice').value, text: q('task-text').textContent, status: q('task-status').textContent,
          marker: q('route-map').dataset.robotPosition, summary: q('motion-summary')?.innerText,
          connection: q('connection').textContent, scan: q('scan-status').textContent, steps: q('steps').innerText, facts};
      });
      const odom = sample.sources.odom.data, pose = (sample.sources.pose?.fresh ? sample.sources.pose : sample.sources.odom).data;
      const xy = (display.facts['平面位置'] || '').match(/-?\d+\.\d+/g)?.map(Number);
      const speed = parseFloat(display.facts['线速度']);
      const marker = display.marker && JSON.parse(display.marker);
      if (!xy || !Number.isFinite(speed) || Math.hypot(xy[0]-pose.x, xy[1]-pose.y) > .025 ||
          !Array.isArray(marker) || Math.hypot(marker[0]-pose.x, marker[1]-pose.y) > .025 ||
          !display.summary?.includes(display.facts['运动状态']) || !display.summary.includes(display.facts['线速度']) ||
          Math.abs(speed-odom.v) > .01 || display.scan !== '新鲜观测') continue;
      lastStamp = sample.now;
      const entry = {timestamp: sample.now, receivedUtc: new Date().toISOString(), frame: pose.frame, displayedXY: xy,
        displayedSpeed: speed, observedXY: [pose.x, pose.y], observedSpeed: odom.v, sourceAgeSeconds: sample.sources.odom.age_s,
        marker, headline: display.summary, state: display.facts['运动状态'], selected: display.id, text: display.text,
        taskState: display.status, connection: display.connection};
      log.samples.push(entry);
      if(start && display.id===id && !routed && preview?.status==='planned' && preview.task_id===id &&
          preview.motion_authorized===false && preview.operation_id===sample.active_operation){
        const [dashboard,grid,planar,spatial]=await Promise.all([paintRoutes(page,'#route-map'),paintRoutes(page,'#grid-map'),
          paintRoutes(map2,'#planar-viewport',true),paintRoutes(map3,'#viewport',true)]);
        entry.overlays={dashboard,grid,planar,spatial};
        const dual=roi=>roi.global>=2 && roi.local>=2 && roi.greenPixels>3 && roi.orangePixels>3;
        if(dual(dashboard)&&dual(planar)&&dual(spatial)&&grid.local>=2&&grid.orangePixels>3){
          routed={...entry,operationId:preview.operation_id,sourceStamp:preview.source_stamp_s,
            localPoints:preview.points,frame:preview.frame,sceneRevision:preview.scene_revision};
          log.routed=routed;persist();
          await page.evaluate(()=>scrollTo(0,0));
          await page.screenshot({path:path.join(out,'both-routes-first-screen.png'),clip:{x:0,y:0,width:1440,height:1010}});
          await shot(page,out,'both-routes-campus');
          console.log('Verified real global and local painted paths',JSON.stringify(entry.overlays));
          await page.click('#local-3d');
        }
      }
      if(routed && !toggled && preview?.status==='planned'){
        const points=await paintRoutes(local3,'#viewport',true);
        if(points.local>=2 && points.global===0 && points.orangePixels>3){
          toggled={mode:'local 3d',...points};log.local3d=toggled;
          await page.$eval('#local3-host',e=>e.scrollIntoView({block:'center'}));await shot(page,out,'local-route-3d');
          await page.click('#local-2d');await page.evaluate(()=>scrollTo(0,0));persist();
        }
      }
      if (!start && entry.state === '静止') {
        start = entry;
        await shot(page, out, 'motion-before');
        console.log('READY FOR SIMULATED TASK', id, 'live stationary position', xy, 'UTC', entry.receivedUtc);
      } else if (start && !moving && display.id === id && display.status !== '已完成' && entry.state === '运动中' && Math.abs(speed) > .04) {
        moving = entry;
        await page.evaluate(() => scrollTo(0, 0));
        await shot(page, out, 'motion-during');
        console.log('Moving on frontend', xy, speed);
      } else if (moving && display.id === id && display.status === '已完成' && entry.state === '静止' &&
        /停稳/.test(display.steps) && Math.hypot(xy[0]-start.displayedXY[0], xy[1]-start.displayedXY[1]) > .3) {
        assert(routed,'Task ended before verified global and local overlays appeared on all active maps');
        assert(toggled,'The local 3D choice never painted a verified local route');
        const [dashboard,grid,planar,spatial]=await Promise.all([paintRoutes(page,'#route-map'),paintRoutes(page,'#grid-map'),
          paintRoutes(map2,'#planar-viewport',true),paintRoutes(map3,'#viewport',true)]);
        if([dashboard,grid,planar,spatial].some(v=>v.local||v.orangePixels>0))continue;
        entry.stoppedOverlays={dashboard,grid,planar,spatial};
        await page.evaluate(() => scrollTo(0,0));
        await shot(page, out, 'motion-after');
        assert.deepEqual(log.deniedRequests, []);
        Object.assign(log, {passed: true, start, moving, stopped: entry, stopWitness: display.steps});
        persist();
        console.log('Verified real motion and independent stop', id, file);
        return;
      }
      persist();
    }
    throw Error('Timed out waiting for a fresh stationary → moving → independently stopped UI record for ' + id);
  } catch (e) {log.failure = e.message; throw e;}
  finally {persist(); await browser.close();}
}

async function main() {
  const site = new URL(args.url);
  assert(['127.0.0.1', 'localhost'].includes(site.hostname) && site.protocol === 'http:', 'Use a loopback campus');
  if (args['watch-task']) return observeMotion(site, args['watch-task'], path.resolve(args.out));
  const pageUrl = new URL('/campus-mission.html', site);
  const [links, state, task] = await Promise.all([
    read(new URL('/api/campus-mission/views', site)), read(new URL('/api/state', site)), evidenceFor(site, args.task)]);
  const map = new URL(links.campus_2d), overview = new URL(links.overview);
  assert.equal(new URL(links.campus_3d).origin, map.origin);
  assert([site.hostname, '127.0.0.1', 'localhost'].includes(map.hostname));
  assert([site.hostname, '127.0.0.1', 'localhost'].includes(overview.hostname));
  if (args['map-port']) assert.equal(map.port, args['map-port']);
  if (args['overview-port']) assert.equal(overview.port, args['overview-port']);
  assert(state.sources.odom.fresh && state.sources.scan.fresh && state.sources.map.available);
  pageUrl.searchParams.set('task_id', task.row.id);
  const out = path.resolve(args.out);
  fs.mkdirSync(out, {recursive: true});
  const puppeteer = require(args.puppeteer || 'puppeteer-core');
  const browser = await puppeteer.launch({executablePath: args.browser, headless: true,
    args: ['--no-first-run', '--disable-background-networking']});
  const report = {passed: false, source: site.origin, task: {id: task.row.id, text: task.row.text,
    state: task.row.state, destination: task.evidence.destination_id, verified: task.evidence.verified_count},
    views: links, screens: {}, errors: [], deniedRequests: []};
  const allowed = new Set([site.origin, map.origin, overview.origin]);
  let fault = null;
  let localSnapshot = null;
  async function newPage() {
    const page = await browser.newPage();
    page.setDefaultTimeout(timeout);
    page.on('pageerror', error => report.errors.push(error.message));
    await page.setRequestInterception(true);
    page.on('request', request => {
      const url = new URL(request.url());
      if (!['data:', 'blob:', 'about:'].includes(url.protocol) && (!allowed.has(url.origin) || request.method() !== 'GET')) {
        report.deniedRequests.push(request.method() + ' ' + url);
        return request.abort();
      }
      if (localSnapshot && url.origin === map.origin && url.pathname === '/api/map' &&
          new URL(request.frame().url()).searchParams.get('local') === '1') {
        // Replay one measured frame so changed pixels isolate camera motion from LiDAR noise.
        return request.respond({status: 200, contentType: 'application/json', body: localSnapshot});
      }
      if (url.origin === site.origin && url.pathname === '/api/state') {
        if (fault === 'offline') return request.abort('internetdisconnected');
        if (fault === 'stale') {
          const old = structuredClone(state);
          for (const sample of Object.values(old.sources)) Object.assign(sample, {fresh: false, age_s: 120});
          return request.respond({status: 200, contentType: 'application/json', body: JSON.stringify(old)});
        }
      }
      return request.continue();
    });
    return page;
  }
  try {
    const page = await newPage();
    for (const [width, height] of [[1440, 900], [390, 844]]) {
      await page.setViewport({width, height, deviceScaleFactor: 1});
      await page.goto(pageUrl.href, {waitUntil: 'domcontentloaded'});
      await page.waitForFunction((id, goal) => document.querySelector('#task-choice')?.value === id &&
        document.querySelector('#task-text')?.textContent === goal, {timeout}, task.row.id, task.row.text);
      await fits(page, width);
      assert(await page.$eval('#operator', e => e.getAttribute('aria-pressed') === 'true'));
      const evidence = await paintScene(page, '#route-map', state, task);
      assert.equal(evidence.measured, String(Array.isArray(task.evidence.observed_xy)));
      assert.equal(await text(page, '#task-status'), '已完成');
      const witness = state.missions.find(m => m.mission_id === task.evidence.mission_id)?.steps
        ?.find(s => s.odom_stamp_ns > 0 && /stationary odometry confirmed/i.test(s.action_result));
      if (witness) assert.match(await text(page, '#steps'), /停稳/);
      await page.$eval('.event-fold', e => {e.open = true;});
      await page.waitForFunction(() => document.querySelectorAll('#events li').length > 0, {timeout});
      await page.$eval('.event-fold', e => {e.open = false;});
      await shot(page, out, 'mission-' + width);
      await page.click('#developer');
      await page.waitForSelector('#debug', {visible: true});
      assert((await text(page, '#diagnostics')).length > 0);
      await fits(page, width);
      await page.click('#operator');
      await page.waitForSelector('#debug', {hidden: true});
      const views = await inspectViews(page, links, width, out);
      report.screens[width] = {route: evidence, views, taskStatus: await text(page, '#task-status'),
        progress: await text(page, '#task-progress'), sensors: [await text(page, '#scan-status'), await text(page, '#grid-status')]};
      if (width === 1440) {
        localSnapshot = JSON.stringify(await read(new URL('/api/map', map)));
        report.localCamera = await inspectLocalCamera(page, task.row.id, out);
        localSnapshot = null;
        const alternate = task.index.tasks.find(t => t.id !== task.row.id && t.text !== task.row.text && t.state === 'SUCCEEDED');
        if (alternate) {
          await page.select('#task-choice', alternate.id);
          await page.waitForFunction(goal => document.querySelector('#task-text').textContent === goal, {timeout}, alternate.text);
          assert.equal(new URL(page.url()).searchParams.get('task_id'), alternate.id);
          report.switchedTask = alternate.id;
        }
      }
    }
    const overviewPage = await newPage();
    await overviewPage.setViewport({width: 1440, height: 900, deviceScaleFactor: 1});
    for (const mode of ['2d', '3d']) {
      await overviewPage.goto(links['campus_' + mode]);
      await overviewPage.waitForFunction(() => !document.querySelector('#place-fit')?.disabled &&
        document.querySelector('#place-fit')?.options.length >= 3, {timeout});
      await overviewPage.select('#place-fit', mode === '2d' ? 'campus' : 'teaching');
      const selector = mode === '2d' ? '#planar-viewport' : '#viewport';
      await overviewPage.waitForSelector(selector, {visible: true});
      await overviewPage.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
      assert((await colors(overviewPage, selector)) >= 5, 'Expanded ' + mode + ' did not render');
      await shot(overviewPage, out, 'map-' + mode);
    }
    await overviewPage.goto(links.overview);
    const manifest = await read(new URL('manifest.json', links.overview));
    const place = manifest.areas.find(a => a.id === 'teaching');
    await overviewPage.click('#choices button[data-id="teaching"]');
    assert.equal(await text(overviewPage, '#label'), place.label);
    await overviewPage.click('#route-check');
    await overviewPage.waitForSelector('#route-details', {visible: true});
    const routeText = await text(overviewPage, '#route-details');
    assert(routeText.includes(String(manifest.routes[0].edges.length)));
    report.overview = {revision: manifest.scene_revision, locations: manifest.areas.length, walk: routeText};
    await shot(overviewPage, out, 'campus-overview');
    await overviewPage.goto(new URL(links.recording, site).href);
    await overviewPage.waitForSelector('#review', {visible: true});
    const draftHref = new URL(await page.$eval('#submit-link', link => link.href));
    assert.equal(draftHref.origin, site.origin);
    const draft = draftHref.searchParams.get('task_draft');
    assert(draft && draft.includes('教学楼'));
    await overviewPage.goto(draftHref.href);
    await overviewPage.waitForSelector('#task-modal[open]', {visible: true});
    assert.equal(await overviewPage.$eval('#task-text', e => e.value), draft);
    await overviewPage.click('#close-task');
    await overviewPage.waitForSelector('#task-modal', {hidden: true});
    report.draft = {text: draft, openedConfirmation: true, submitted: false};
    await overviewPage.close();
    await page.setViewport({width: 1440, height: 900, deviceScaleFactor: 1});
    await page.evaluate(() => window.scrollTo(0, 0));
    const live = await text(page, '#connection');
    const scanLive = await text(page, '#scan-status');
    assert.match(live, /在线|实时|连接|更新中|采样中/);
    fault = 'stale';
    await page.waitForFunction((conn, scan) => document.querySelector('#connection')?.textContent !== conn &&
      document.querySelector('#scan-status')?.textContent !== scan, {timeout}, live, scanLive);
    const stale = await text(page, '#connection');
    assert.match(stale, /过期|陈旧|等待|失效|历史|滞后|迟滞|超时|中断/);
    assert.equal(await page.$eval('#route-map', map => map.dataset.robotPosition), '');
    fault = 'offline';
    await page.waitForFunction(previous => document.querySelector('#connection')?.textContent !== previous, {timeout}, stale);
    const offline = await text(page, '#connection');
    assert.match(offline, /断开|离线|不可用|中断|失败|重连/);
    assert.equal(await text(page, '#task-text'), task.row.text);
    await shot(page, out, 'disconnected');
    fault = null;
    await page.waitForFunction((conn, scan) => document.querySelector('#connection')?.textContent === conn &&
      document.querySelector('#scan-status')?.textContent === scan, {timeout}, live, scanLive);
    report.observationFaults = {live, stale, offline, recovered: await text(page, '#connection'), taskRetained: task.row.id};
    assert.deepEqual(report.errors, []);
    assert.deepEqual(report.deniedRequests, []);
    report.passed = true;
  } catch (error) {
    report.failure = error.message;
    throw error;
  } finally {
    await browser.close();
    fs.writeFileSync(path.join(out, 'browser.json'), JSON.stringify(report, null, 2) + '\n');
  }
  console.log('Verified campus mission', task.row.id, 'at 1440px and 390px. Report:', path.join(out, 'browser.json'));
}

main().catch(error => {console.error(error); process.exitCode = 1;});
