/** Exercise the camera on a real, archived campus task without submitting requests to the robot. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const {parseArgs} = require('node:util');

const {values: opts} = parseArgs({options: {
  url: {type: 'string'}, browser: {type: 'string'}, puppeteer: {type: 'string', default: 'puppeteer-core'},
  output: {type: 'string'}
}});
const url = new URL(opts.url);
assert.equal(url.pathname, '/exploration-mission.html');
assert(url.searchParams.get('task_id') && opts.browser);

async function state(page) {
  await page.evaluate(() => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done))));
  return page.$eval('#map', async canvas => {
    const d = canvas.dataset;
    const image = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
    // Use the painted vehicle (3D arrow or planar odometry marker) as an independent world landmark.
    const [r,g,b] = d.view==='local' ? [167,232,243] : [133,217,238];
    let x=0,y=0,n=0;
    for (let i=0; i<image.length; i+=4) {
      if (image[i]!==r || image[i+1]!==g || image[i+2]!==b) continue;
      x+=(i/4)%canvas.width; y+=Math.floor(i/4/canvas.width); n++;
    }
    const hash = Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',image))).map(
      v => v.toString(16).padStart(2,'0')).join('');
    return {view:d.view, center:[Number(d.cameraX),Number(d.cameraY)], heading:d.cameraYaw,
      tilt:d.cameraTilt, zoom:Number(d.cameraZoom), hash,
      marker:n ? [x/n*canvas.clientWidth/canvas.width,y/n*canvas.clientHeight/canvas.height] : null};
  });
}

function sameView(actual,expected) {
  // Sensor overlays can repaint independently. Check the camera and its projection of the robot.
  for (const field of ['view','center','heading','tilt','zoom']) assert.deepEqual(actual[field],expected[field],field);
  assert(actual.marker && expected.marker && Math.hypot(...actual.marker.map((n,i)=>n-expected.marker[i]))<1);
}

function translated(from,to,dx,dy,tolerance=3) {
  assert(from.marker && to.marker, 'Expected a visible measured robot position');
  const x = to.marker[0]-from.marker[0], y = to.marker[1]-from.marker[1];
  assert(Math.abs(x-dx)<tolerance && Math.abs(y-dy)<tolerance,
    `Robot marker moved (${x.toFixed(1)},${y.toFixed(1)}); dragged (${dx},${dy})`);
  assert.equal(from.zoom,to.zoom);
  assert.equal(from.heading,to.heading);
}

async function exercise() {
  const browser = await require(opts.puppeteer).launch({executablePath:opts.browser,headless:true});
  const evidence = {task:url.searchParams.get('task_id'),url:opts.url};
  try {
    const page = await browser.newPage();
    const errors=[], writes=[];
    let replaySnapshot=null;
    page.on('pageerror', e => errors.push(String(e)));
    await page.setRequestInterception(true);
    page.on('request', r => {
      if (replaySnapshot && r.method()==='GET' && r.url()===replaySnapshot.url)
        return void r.respond({status:200,contentType:'application/json',body:replaySnapshot.body});
      if (r.method()==='GET' && new URL(r.url()).origin===url.origin) return void r.continue();
      writes.push([r.method(),r.url()]); return void r.abort();
    });
    await page.setViewport({width:1440,height:960});
    const localUrl=new URL(url);
    localUrl.searchParams.set('view','local');
    await page.goto(localUrl.href);
    await page.waitForFunction(id => document.body.dataset.taskId===id &&
      ['SUCCEEDED','BLOCKED'].includes(document.body.dataset.taskState) &&
      Number(document.querySelector('#map').dataset.depthPoints)>0,{},evidence.task);
    async function center() {
      return page.$eval('#map',el=>{
        el.scrollIntoView({block:'center',behavior:'instant'});
        const box=el.getBoundingClientRect();
        return {x:box.x+box.width*.52,y:box.y+box.height*.53};
      });
    }
    async function drag(dx,dy,button='left') {
      const {x,y}=await center();
      await page.mouse.move(x,y); await page.mouse.down({button});
      await page.mouse.move(x+dx,y+dy,{steps:8}); await page.mouse.up({button});
      return state(page);
    }
    const original=await state(page);
    const pan=await drag(72,32);
    translated(original,pan,72,32);
    const source=new URL('/api/exploration-mission'+url.search,url);
    const snapshot=await (await fetch(source)).json();
    assert.equal(snapshot.mission.id,evidence.task);
    const {initial_pose:initial,pose:final}=snapshot.mission;
    assert.equal(initial.frame,final.frame);
    assert(Math.hypot(initial.x-final.x,initial.y-final.y)>.2,'Choose an archive in which the rover moved');
    const beforeReplay=await drag(0,-100);
    // Feed the archived initial odometry into this tab's next GET. Neither the archive nor the robot is modified.
    const finalPoseText=await page.$eval('#pose',element=>element.textContent);
    snapshot.mission.pose=initial;
    replaySnapshot={url:source.href,body:JSON.stringify(snapshot)};
    await page.waitForFunction(text=>document.querySelector('#pose').textContent!==text,{},finalPoseText);
    const pinned=await state(page);
    assert.deepEqual(pinned.center,beforeReplay.center);
    assert.equal(pinned.heading,beforeReplay.heading);
    assert(beforeReplay.marker && pinned.marker &&
      Math.hypot(...pinned.marker.map((v,i)=>v-beforeReplay.marker[i]))>5);
    await page.click('#view-reset');
    const following=await state(page);
    assert.deepEqual(following.center,[initial.x,initial.y]);
    replaySnapshot=null;
    await page.waitForFunction(text=>document.querySelector('#pose').textContent===text,{},finalPoseText);
    sameView(await state(page),original);
    evidence.odometryReplay={mapRevision:snapshot.mission.map_ref.revision,initial,final,beforeReplay,pinned,following};
    await drag(72,32);
    const rotated=await drag(48,16,'right');
    assert.notEqual(rotated.heading,pan.heading); assert.notEqual(rotated.hash,pan.hash);
    await page.keyboard.down('Shift');
    const shifted=await drag(-22,-8);
    await page.keyboard.up('Shift');
    assert.notEqual(shifted.heading,rotated.heading);
    const {x,y}=await center(); await page.mouse.move(x,y); await page.mouse.wheel({deltaY:-120});
    const zoom=await state(page);
    assert(zoom.zoom>shifted.zoom); assert.notEqual(zoom.hash,shifted.hash);
    await page.waitForResponse(r=>new URL(r.url()).pathname==='/api/exploration-mission');
    sameView(await state(page),zoom);
    await page.click('#view-global');
    const global=await state(page);
    const globalPan=await drag(-30,-20);
    translated(global,globalPan,-30,-20);
    await page.click('#view-local'); sameView(await state(page),zoom);
    await page.click('#view-global'); sameView(await state(page),globalPan);
    await page.click('#view-reset'); sameView(await state(page),global);
    await page.click('#view-local'); await page.click('#view-reset'); sameView(await state(page),original);
    evidence.desktop={original,pan,rotated,shifted,zoom,global,globalPan};

    await page.setViewport({width:390,height:844,deviceScaleFactor:1,isMobile:true,hasTouch:true});
    await page.waitForFunction(()=>Number(document.querySelector('#map').dataset.depthPoints)>0);
    const mobile=await state(page);
    const cdp=await page.createCDPSession();
    const touch=(type,touchPoints)=>cdp.send('Input.dispatchTouchEvent',{type,touchPoints});
    const start=await center();
    async function swipe(dx,dy) {
      await touch('touchStart',[{...start,id:1}]);
      await touch('touchMove',[{x:start.x+dx,y:start.y+dy,id:1}]);
      await touch('touchEnd',[]);
      return state(page);
    }
    const touchPan=await swipe(32,18);
    translated(mobile,touchPan,32,18);
    const pair=sep=>[{id:1,x:start.x-sep,y:start.y},{id:2,x:start.x+sep,y:start.y}];
    await touch('touchStart',pair(22)); await touch('touchMove',pair(39)); await touch('touchEnd',[]);
    const pinch=await state(page);
    assert(pinch.zoom>touchPan.zoom); assert.notEqual(pinch.hash,touchPan.hash);
    await page.click('#view-rotate'); await center();
    const touchRotate=await swipe(35,14);
    assert.notEqual(touchRotate.heading,pinch.heading);
    await page.click('#view-reset'); sameView(await state(page),mobile);
    evidence.mobile={original:mobile,pan:touchPan,pinch,rotate:touchRotate};
    assert.deepEqual(writes,[]); assert.deepEqual(errors,[]);
    evidence.writes=writes; evidence.errors=errors;
    if (opts.output) await fs.writeFile(opts.output,JSON.stringify(evidence,null,2)+'\n');
    console.log('Passed desktop and touch camera checks on '+evidence.task);
  } finally { await browser.close(); }
}
exercise().catch(error=>{console.error(error);process.exitCode=1;});
