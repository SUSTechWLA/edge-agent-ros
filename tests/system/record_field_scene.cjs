/* Read-only browser acceptance and real-time recording of a field task replay. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {createHash} = require('node:crypto');
const {spawnSync} = require('node:child_process');
const {parseArgs} = require('node:util');
const {setTimeout: wait} = require('node:timers/promises');

const {values: args} = parseArgs({options: Object.fromEntries(
  ['url','sim-url','browser','puppeteer','ffmpeg','ffprobe','out','origin','credentials','task-file','capture'].map(k=>[k,{type:'string'}])
)});
const hash = data => createHash('sha256').update(data).digest('hex');
const frame = page => page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
const mapState = page => page.$eval('#map',node=>({...node.dataset}));
const snap = (page, filename) => page.screenshot({path:filename, type:'png'});

async function record() {
  for (const key of ['url','out','browser','task-file','capture']) assert(args[key], 'Missing --'+key);
  assert(/^[0-9a-f]{64}$/.test(args.capture));
  const site = new URL(args.url);
  const sim = new URL(args['sim-url']||site.href,site);
  assert.equal(sim.origin,site.origin);
  assert(!site.username&&!site.password);
  const task = JSON.parse(fs.readFileSync(args['task-file'],'utf8'));
  const expected = task.steps.find(s=>s.evidence?.path_body_m)?.evidence;
  assert(expected && expected.status==='blocked' && expected.path_body_m.length===0,'Expect a recorded held planning task');
  assert.equal(task.id,site.searchParams.get('task'));
  const out=path.resolve(args.out);
  if(!fs.existsSync(out))fs.mkdirSync(out,{recursive:true});
  assert(!fs.existsSync(path.join(out,'interactive-map.mp4')),'Use a fresh result directory');
  const stages=[], issues=[], requests=new Set();
  const audit={version:1,source:'real browser screencast',field_url:site.href,simulation_url:args['sim-url']?sim.href:null,
    field_task_id:task.id,expected_snapshot:expected.source_evidence.snapshot_ref,stages,issues};
  const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'edge-map-cast-'));
  const intermediate=path.join(tmp,'capture.webm');
  const ffmpeg=args.ffmpeg||'ffmpeg', ffprobe=args.ffprobe||'ffprobe';
  let browser,recorder,began=0,missingSceneTest=false;
  const acted=async (name,page)=>{
    await frame(page);
    stages.push({action:name,sec:+((performance.now()-began)/1000).toFixed(3),url:page.url(),map:await mapState(page)});
  };
  try {
    const puppeteer=require(args.puppeteer||'puppeteer-core');
    browser=await puppeteer.launch({executablePath:args.browser,headless:true,
      defaultViewport:{width:1600,height:1000,deviceScaleFactor:1},
      args:['--disable-background-networking','--disable-component-update','--no-first-run','--hide-scrollbars']});
    const page=await browser.newPage();
    page.setDefaultTimeout(20000);
    if(args.credentials) {
      assert.equal(args.origin,site.origin,'Explicit origin required for credentials');
      const {username,password}=JSON.parse(fs.readFileSync(args.credentials,'utf8').replace(/^\uFEFF/,''));
      assert(typeof username==='string' && typeof password==='string');
      await page.goto(new URL('/login',site).href);
      assert.equal(new URL(page.url()).origin,site.origin);
      const ok=await page.evaluate(async c=>(await fetch('/login',{method:'POST',body:new URLSearchParams(c)})).ok,{username,password});
      assert(ok,'Login failed');
    }
    page.on('pageerror',error=>issues.push(String(error.message)));
    await page.setRequestInterception(true);
    page.on('request',r=>{
      const u=new URL(r.url());
      if(['data:','blob:','about:'].includes(u.protocol))return r.continue();
      if(!['GET','HEAD'].includes(r.method())||u.origin!==site.origin){
        issues.push('Unexpected request: '+r.method()+' '+u.pathname);
        return r.abort();
      }
      if(missingSceneTest && /^\/scene\/[a-f0-9]{64}\.json$/.test(u.pathname))
        return r.respond({status:503,contentType:'application/json',body:'{}'});
      requests.add(u.pathname);return r.continue();
    });
    await page.goto(site.href,{waitUntil:'domcontentloaded'});
    await page.evaluate(()=>document.fonts.ready);
    await page.waitForFunction(()=>Number(document.querySelector('#map')?.dataset.voxelCount)>0);
    const pinnedFieldImage=expected.source_evidence.camera.url;
    await page.waitForFunction(src=>{
      const image=document.getElementById('rgb');
      return image&&!image.hidden&&image.complete&&image.naturalWidth>0&&new URL(image.currentSrc).pathname===src;
    },{},pinnedFieldImage);
    audit.field_camera=pinnedFieldImage;
    audit.deployed_assets=await page.evaluate(async()=>{
      const entries=[];
      for(const name of ['/plan-observe.html','/plan-observe.css','/plan-observe.js','/plan-scene.js','/plan-example.json']){
        const r=await fetch(name,{credentials:'same-origin',cache:'no-cache'});
        if(!r.ok)throw Error('Unavailable deployed asset '+name);
        const bytes=new Uint8Array(await r.arrayBuffer());
        let chunks='';
        for(let start=0;start<bytes.length;start+=8192)
          chunks+=String.fromCharCode(...bytes.subarray(start,start+8192));
        entries.push([name,btoa(chunks)]);
      }
      return entries;
    }).then(entries=>Object.fromEntries(entries.map(([name,data])=>[name,hash(Buffer.from(data,'base64'))])));
    const original=await mapState(page);
    assert.equal(Number(original.routePoints),0);
    await frame(page);
    await snap(page,path.join(out,'field-overview.png'));
    recorder=await page.screencast({path:intermediate,format:'webm',quality:18,ffmpegPath:ffmpeg,fps:24});
    began=performance.now();
    await acted('实机归档 · 三维占据与停车结论',page);
    await wait(2300);
    const box=await page.$eval('#map',el=>el.getBoundingClientRect().toJSON());
    const x=box.x+.42*box.width,y=box.y+.57*box.height;
    await page.mouse.move(x,y);await page.mouse.down();
    for(let i=1;i<=28;i++){
      await page.mouse.move(x+130*i/28,y-42*i/28);
      await wait(23);
    }
    await page.mouse.up();await frame(page);
    const afterDrag=await mapState(page);
    assert(original.yaw!==afterDrag.yaw||original.pitch!==afterDrag.pitch,'Dragging must rotate the map');
    await acted('拖动环视实测障碍',page); await wait(1000);
    await page.click('#view-top'); await frame(page);
    assert.equal((await mapState(page)).view,'top');
    await acted('实机正俯视',page);await wait(1000);
    const beforeZoom=Number((await mapState(page)).zoom);
    await page.mouse.move(x,y);await page.mouse.wheel({deltaY:-128});
    await frame(page);await wait(220);
    assert.notEqual(Number((await mapState(page)).zoom),beforeZoom,'Mouse wheel must change the scale');
    await page.click('#zoom-in');await frame(page);
    const closer=Number((await mapState(page)).zoom);
    await page.click('#zoom-out');await frame(page);
    assert.notEqual(Number((await mapState(page)).zoom),closer);
    await acted('缩放车周净距',page);
    await snap(page,path.join(out,'field-top.png'));await wait(900);
    const image=async()=>hash(await (await page.$('#map')).screenshot({type:'png'}));
    for(const layer of ['occupied','ground']){
      const select='[data-layer="'+layer+'"]';
      const before=await image();
      await page.click(select);await frame(page);
      assert.notEqual(await image(),before,'Layer must visibly change the map: '+layer);
      await acted('切换图层 · '+layer,page);await wait(450);
      await page.click(select);await frame(page);
    }
    await page.click('#reset-camera');await frame(page);
    const reset=await mapState(page);
    for(const key of ['yaw','pitch','zoom'])assert.equal(reset[key],original[key],'Reset '+key);
    await acted('复位到车后上方',page);await wait(700);
    const rgbButton=await page.$('#view-rgbd');
    if(rgbButton && await rgbButton.isVisible() && await rgbButton.evaluate(el=>!el.disabled)){
      const rgbUrl=new URL(site.href);rgbUrl.searchParams.set('scene','rgbd');rgbUrl.searchParams.set('capture',args.capture);
      await page.goto(rgbUrl.href,{waitUntil:'domcontentloaded'});
      await page.waitForFunction(()=>document.querySelector('#map').dataset.frameKind==='camera'
        &&Number(document.querySelector('#map').dataset.triangleCount)>0);
      await page.waitForFunction(()=>document.querySelector('#map').dataset.texture==='source_png');
      await frame(page);await wait(1100);
      assert.equal(Number((await mapState(page)).routePoints),0,'Independent color camera cannot inherit a body path');
      assert.equal(new URL(page.url()).searchParams.get('capture'),args.capture,'The camera mesh must match the pinned acquisition');
      await page.waitForSelector('#rgb-grid',{visible:true});
      const gridPressed=()=>page.$eval('#rgb-grid',button=>button.getAttribute('aria-pressed')==='true');
      if(await gridPressed()){await page.click('#rgb-grid');await frame(page);}
      assert.equal(await gridPressed(),false);
      await acted('现场 RGB-D 视图',page);
      await snap(page,path.join(out,'field-color.png'));await wait(650);
      const rgbOnly=await image();
      const unmoved=await mapState(page);
      assert.equal(unmoved.rgbGrid,'false');
      await page.click('#rgb-grid');await frame(page);
      await page.waitForFunction(()=>Number(document.querySelector('#map').dataset.colorCells)>0);
      assert.equal(await gridPressed(),true);
      const rgbAndGrid=await image(),overlaid=await mapState(page);
      assert.equal(overlaid.rgbGrid,'true');
      assert.notEqual(rgbAndGrid,rgbOnly,'The occupancy overlay must change the camera raster');
      for(const key of ['yaw','pitch','zoom','triangleCount','frameKind','texture'])assert.equal(unmoved[key],overlaid[key],key);
      const independent=await page.evaluate(async capture=>{
        const response=await fetch('/scene/rgbd/'+capture+'.json',{credentials:'same-origin'});
        if(!response.ok)throw Error('Frozen RGB-D observation unavailable');
        const data=await response.json();
        const points=data.vertices.filter(v=>Array.isArray(v)&&v.slice(0,3).every(Number.isFinite)&&v[2]>0);
        const cells=new Set(points.map(v=>v.slice(0,3).map(x=>Math.floor(x*10)).join(',')));
        return {frame:data.frame_id,stamp_ns:data.stamp_ns,valid_vertices:points.length,
          voxel_count:cells.size,color_artifact:data.source_evidence.color.artifact_ref};
      },args.capture);
      assert.equal(independent.voxel_count,Number(overlaid.colorCells),'Grid count must match independently binned optical XYZ');
      audit.rgbd_overlay={capture:args.capture,texture_raster_sha256:rgbOnly,overlaid_raster_sha256:rgbAndGrid,
        occupied_cells:Number(overlaid.colorCells),cell_m:.1,triangles:Number(overlaid.triangleCount),independent};
      await acted('同一 RGB-D 采样 · 10 厘米占用网格叠加纹理',page);
      await snap(page,path.join(out,'field-color-grid.png'));await wait(1300);
      const initialColor=await mapState(page);
      await page.mouse.move(x,y);await page.mouse.down();
      for(let i=1;i<=20;i++){
        await page.mouse.move(x+58*i/20,y+12*i/20);
        await wait(32);
      }
      await page.mouse.up();await frame(page);
      assert.notEqual((await mapState(page)).yaw,initialColor.yaw,'The RGB-D surface must rotate');
      assert.equal(await gridPressed(),true);
      assert.equal((await mapState(page)).colorCells,initialColor.colorCells,'Orbit cannot change camera-space geometry');
      await acted('旋转同帧纹理与三维占用格',page);await wait(1100);
    }
    if(args['sim-url'])await page.goto(sim.href,{waitUntil:'domcontentloaded'});
    else await page.click('#scene-example');
    await page.waitForFunction(()=>Number(document.querySelector('#map')?.dataset.routePoints)>1);
    assert((await page.$eval('body',n=>n.innerText)).includes('仿真'),'Simulation must be labelled in the page');
    await acted('Gazebo 仿真 · 有规划轨迹的另一场景',page);await wait(2100);
    await snap(page,path.join(out,'simulation-route.png'));
    const routeSelector='[data-layer="route"]';
    const routeOn=await image();
    await page.click(routeSelector);await frame(page);
    assert.notEqual(await image(),routeOn,'Route layer must draw the actual supplied polyline');
    await wait(500);await page.click(routeSelector);await frame(page);
    await page.click('#view-top');await frame(page);
    await acted('仿真正俯视 · 对照绕行轨迹',page);await wait(2000);
    await snap(page,path.join(out,'simulation-top.png'));
    await recorder.stop();recorder=null;
    const video=path.join(out,'interactive-map.mp4');
    const result=spawnSync(ffmpeg,['-hide_banner','-loglevel','error','-y','-i',intermediate,'-an','-c:v','libx264',
      '-preset','veryfast','-crf','17','-pix_fmt','yuv420p','-movflags','+faststart',video],{windowsHide:true,encoding:'utf8'});
    assert.equal(result.status,0,result.stderr||String(result.error));
    const probe=spawnSync(ffprobe,['-v','error','-select_streams','v:0','-show_entries','stream=codec_name,width,height,nb_frames,duration','-of','json',video],
      {windowsHide:true,encoding:'utf8'});
    assert.equal(probe.status,0,probe.stderr);
    audit.media=JSON.parse(probe.stdout).streams[0];
    assert.equal(audit.media.codec_name,'h264');assert(Number(audit.media.duration)>12);
    audit.video_sha256=hash(fs.readFileSync(video));
    // A second, deliberately unavailable body cloud is exercised only after the recording ends.
    // No temporary interception can modify the field or Gazebo data shown in the footage.
    missingSceneTest=true;
    await page.goto(site.href,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>document.querySelector('#scene-field')?.getAttribute('aria-pressed')==='true'
      && document.querySelector('#request')?.dataset.taskState==='SUCCEEDED');
    await wait(800);
    const absent=await mapState(page);
    assert.equal(Number(absent.voxelCount),0,'A missing frozen snapshot cannot reuse an old mesh');
    assert.equal(Number(absent.routePoints),0,'A held task cannot retain the example path');
    audit.missing_body_scene_after_recording={map:absent,message:await page.$eval('#map-empty',n=>n.textContent)};
    audit.same_origin_read_requests=[...requests].sort();
    assert.deepEqual(issues,[]);
  } finally {
    if(recorder)await recorder.stop().catch(()=>{});
    if(browser)await browser.close();
    if(fs.existsSync(intermediate))fs.unlinkSync(intermediate);
    fs.rmdirSync(tmp);
    fs.writeFileSync(path.join(out,'browser-acceptance.json'),JSON.stringify(audit,null,2)+'\n');
  }
}

record().catch(error=>{console.error(error);process.exitCode=1;});
