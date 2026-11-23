/* Record the archive workbench through its normal browser controls and verify the wire receipts. */
'use strict';
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const {createHash} = require('node:crypto');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {setTimeout: wait} = require('node:timers/promises');
const {parseArgs} = require('node:util');

const {values: args} = parseArgs({options: Object.fromEntries(
  ['url','browser','puppeteer','ffmpeg','ffprobe','out'].map(name=>[name,{type:'string'}]))});
const sha256 = file => createHash('sha256').update(fs.readFileSync(file)).digest('hex');
const frame = page => page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
const fmt = value => JSON.stringify(value,null,2)+'\n';
const named = (data, keys) => Object.fromEntries(keys.filter(key=>Object.hasOwn(data,key)).map(key=>[key,data[key]]));

function tool(exe, opts) {
  const result=spawnSync(exe,opts,{windowsHide:true,encoding:'utf8'});
  assert.equal(result.status,0,result.stderr||String(result.error));
  return result.stdout;
}
function registry(state) {
  assert.equal(state.mode,'archive_replay');
  assert.equal(state.motion_authorized,false);
  return {archive:state.archive.id,texture_enabled:state.texture_enabled,
    plugins:state.plugins.map(row=>named(row,['id','service','kind','version','status','binding','pid','process_alive']))};
}
function plugin(catalog, kind) {
  return catalog.plugins.find(row=>row.kind===kind);
}
function planReceipt(reply) {
  const result=reply.plan;
  assert.equal(result.motion_authorized,false);
  assert(Array.isArray(result.path_body_m));
  return {...named(reply,['case','request_id','service','provider_version','binding','evaluation_seq','snapshot_ref',
    'geometry_ref','source','source_clock','source_stamp_ns','admission_stamp_ns','clock_mode','profile_id']),
    ...named(result,['status','reason','base_frame','margin_m','present_min_clearance_m','coverage_fraction_on_path']),
    path_points:result.path_body_m.length,motion_authorized:result.motion_authorized};
}
// Save real Chrome compositor frames while the CPU-sensitive planner is running.
// Encoding is deferred until all calls finish. At each 24 Hz presentation tick
// select the most recently observed image, preserving real elapsed screen time.
async function capture(page, directory) {
  const session=await page.createCDPSession();
  const shots=[];
  const start=performance.now();
  function append(bytes) {
    const sec=(performance.now()-start)/1000;
    const file='frame-'+String(shots.length).padStart(5,'0')+'.jpg';
    fs.writeFileSync(path.join(directory,file),bytes);
    shots.push({file,sec});
  }
  append(await page.screenshot({type:'jpeg',quality:91}));
  const received=event=>{
    append(Buffer.from(event.data,'base64'));
    session.send('Page.screencastFrameAck',{sessionId:event.sessionId}).catch(()=>{});
  };
  session.on('Page.screencastFrame',received);
  await session.send('Page.startScreencast',{format:'jpeg',quality:91,everyNthFrame:1});
  return {start, async finish(){
    await session.send('Page.stopScreencast');
    session.off('Page.screencastFrame',received);
    append(await page.screenshot({type:'jpeg',quality:91}));
    await session.detach();
    const total=Math.max(1,Math.round(shots.at(-1).sec*24));
    let current=0;
    for(let i=0;i<total;i++){
      while(current+1<shots.length && shots[current+1].sec<=i/24)current++;
      const frozen=path.join(directory,shots[current].file);
      const target=path.join(directory,'tick-'+String(i).padStart(6,'0')+'.jpg');
      try {fs.linkSync(frozen,target);}
      catch (error) {
        if(!['ENOTSUP','EPERM','EXDEV','EMLINK'].includes(error.code))throw error;
        fs.copyFileSync(frozen,target);
      }
    }
    return {sequence:path.join(directory,'tick-%06d.jpg'),frames:shots.length,total,elapsed:shots.at(-1).sec};
  }};
}
async function record() {
  assert(args.url && args.out && args.browser,'Expect --url, --out and --browser');
  const site=new URL(args.url);
  assert(['127.0.0.1','localhost'].includes(site.hostname) && site.protocol==='http:' &&
    !site.username && !site.password && !site.search,'A local archive site is required');
  const out=path.resolve(args.out);
  fs.mkdirSync(out,{recursive:true});
  const paths=Object.fromEntries(['scene-layers.mp4','cover.png','acceptance.json'].map(name=>[name,path.join(out,name)]));
  for(const file of Object.values(paths))assert(!fs.existsSync(file),'Use a fresh output directory: '+file);
  const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'dsh-scene-recording-'));
  const ffmpeg=args.ffmpeg||'ffmpeg', ffprobe=args.ffprobe||'ffprobe';
  const puppeteer=require(args.puppeteer||'puppeteer-core');
  let browser, recording, start;
  const chapters=[],issues=[],calls=[];
  const audit={schema_version:1,source:'Chromium page screencast of loopback archive workbench',
    clock:'Chapter offsets use the monotonic capture clock; sensor and admission stamps are separate.',
    passed:false,chapters,calls,issues};
  try {
    browser=await puppeteer.launch({executablePath:args.browser,headless:true,
      defaultViewport:{width:1600,height:1000,deviceScaleFactor:1},
      args:['--disable-background-networking','--no-first-run','--hide-scrollbars']});
    const page=await browser.newPage();
    page.setDefaultTimeout(16000);
    page.on('pageerror',error=>issues.push(error.message));
    const allowed=new Set(['/api/layers','/api/compose','/api/texture','/api/plan']);
    await page.setRequestInterception(true);
    page.on('request',request=>{
      const url=new URL(request.url());
      if(url.origin!==site.origin || !(request.method()==='GET'||request.method()==='POST'&&allowed.has(url.pathname))){
        issues.push('Unexpected request '+request.method()+' '+url.pathname);
        return request.abort();
      }
      return request.continue();
    });
    page.on('response',resp=>{
      const url=new URL(resp.url());
      if(url.pathname.startsWith('/api/'))calls.push({path:url.pathname,method:resp.request().method(),status:resp.status()});
    });
    function nextResponse(route, matches=()=>true) {
      return page.waitForResponse(response=>{
        if(new URL(response.url()).pathname!=='/api/'+route)return false;
        return matches(response.request().postData()?JSON.parse(response.request().postData()):{});
      });
    }
    const idle=()=>page.waitForFunction(()=>!document.querySelector('#open-archive').disabled);
    async function chapter(title,hold) {
      await frame(page);
      chapters.push({title,sec:+((performance.now()-start)/1000).toFixed(3)});
      await wait(hold);
    }
    const firstState=nextResponse('state');
    await page.goto(site.href,{waitUntil:'domcontentloaded'});
    audit.before=registry(await (await firstState).json());
    const initialTexture=plugin(audit.before,'texture');
    assert.equal(audit.before.texture_enabled,true,'Start with the texture provider enabled');
    assert(initialTexture?.pid && initialTexture.process_alive);
    await idle();
    await page.evaluate(()=>document.fonts.ready);
    recording=await capture(page,tmp);
    start=recording.start;
    await chapter('打开 Jetson 归档工作台',1800);
    const layerNames=['occupancy','texture'];
    const layerReads=layerNames.map(kind=>nextResponse('layers',body=>body.layer===kind));
    await page.click('#open-archive');
    const layerReplies=await Promise.all((await Promise.all(layerReads)).map(response=>response.json()));
    audit.layers={};
    for(const [index,item] of layerReplies.entries()){
      assert(/^sha256:[a-f0-9]{64}$/.test(item.scene_ref) && /^sha256:[a-f0-9]{64}$/.test(item.layer_ref));
      audit.layers[layerNames[index]]={scene_ref:item.scene_ref,layer_ref:item.layer_ref,
        frame_id:item.scene.frame_id,stamp_ns:item.scene.stamp_ns,
        ...(index===0?{voxel_count:item.scene.voxels.length,cell_m:item.scene.voxel_size_m}
          :{registration:item.scene.registration,triangles:item.scene.triangles.length,
            color_ref:item.scene.source_evidence.color.artifact_ref})};
    }
    assert.notEqual(audit.layers.texture.frame_id,audit.layers.occupancy.frame_id);
    await page.waitForFunction(()=>document.getElementById('texture-map').dataset.texture==='source_png' &&
      !document.getElementById('texture-photo').hidden && +document.getElementById('occupancy-map').dataset.voxelCount>0);
    await chapter('在各自坐标系读取实测观测',3600);
    const compose=nextResponse('compose');
    await page.click('#check-registration');
    const composed=await compose;
    assert.equal(composed.status(),422);
    const rejection=await composed.json();
    assert.equal(rejection.scene_ref,null);
    assert(rejection.reasons.some(row=>row.code==='scene.time_skew'));
    assert(rejection.reasons.some(row=>row.code==='scene.missing_registration'));
    assert.equal(rejection.time_span_ms,
      Number(BigInt(audit.layers.texture.stamp_ns)-BigInt(audit.layers.occupancy.stamp_ns))/1e6);
    audit.compose=named(rejection,['status','scene_ref','reasons','time_span_ms','motion_authorized']);
    await page.waitForFunction(()=>document.getElementById('query-title').textContent==='拒绝合并');
    await chapter('核对未配准原因和采样时差',4100);
    await page.screenshot({path:paths['cover.png']});

    async function plan(name) {
      await idle();
      await page.select('#plan-case',name);
      const pending=nextResponse('plan',body=>body.case===name);
      await page.click('#run-plan');
      const response=await pending;
      const data=await response.json();
      if(response.status()!==200){
        audit.planning_failure={case:name,http_status:response.status(),response:data};
        throw Error('Plan request rejected: '+fmt(audit.planning_failure));
      }
      const receipt=planReceipt(data);
      assert.equal(receipt.case,name);assert.equal(receipt.service,'planning.local_path');
      await idle();
      const points=await page.$eval('#plan-map',e=>Number(e.dataset.routePoints));
      assert.equal(points,receipt.path_points);
      if(name==='field_hold'){assert.equal(receipt.status,'blocked');assert.equal(points,0);}
      else {assert.equal(receipt.status,'partial');assert(points>2);}
      return receipt;
    }
    audit.planning_before={field_hold:await plan('field_hold')};
    await chapter('停用前 · 实车近障保持原位',3300);
    audit.planning_before.centered=await plan('centered');
    await chapter('停用前 · 仿真中央箱体绕行',3600);

    const manage=nextResponse('texture',body=>body.enabled===false);
    const freshState=nextResponse('state');
    const denied=nextResponse('layers',body=>body.layer==='texture');
    await page.click('#toggle-texture');
    const reply=await (await manage).json();
    const change=reply.change;
    audit.disable=named(change,['state','service','pid','exit_code','drain','remove']);
    assert.equal(change.state,'absent');assert.equal(change.pid,initialTexture.pid);
    assert.equal(change.exit_code,0);assert(change.drain.event_id && change.remove.event_id);
    assert.notEqual(change.drain.event_id,change.remove.event_id);
    audit.after=registry(await (await freshState).json());
    assert.equal(audit.after.texture_enabled,false);assert.equal(plugin(audit.after,'texture'),undefined);
    for(const name of ['occupancy','query']){
      const original=plugin(audit.before,name),current=plugin(audit.after,name);
      assert(original.pid && current.process_alive);
      assert.equal(current.pid,original.pid);assert.equal(current.binding,original.binding);
    }
    const read=await denied;
    const failure=await read.json();
    assert.equal(read.status(),409);assert.equal(failure.code,'plugin.service_missing');
    audit.texture_read_after_disable={http_status:read.status(),code:failure.code,scene_ref:failure.scene_ref};
    await page.waitForFunction(()=>document.getElementById('texture-caption').textContent.includes('plugin.service_missing') &&
      document.getElementById('texture-photo').hidden && document.getElementById('texture-map').dataset.triangleCount==='0');
    await chapter('热停用纹理插件，再次读取被拒绝',4700);

    audit.planning_after={field_hold:await plan('field_hold')};
    await chapter('停用后 · 再次请求实车近障规划',3900);
    audit.planning_after.centered=await plan('centered');
    for(const name of Object.keys(audit.planning_before)){
      const before=audit.planning_before[name],after=audit.planning_after[name];
      for(const key of ['snapshot_ref','geometry_ref','binding','provider_version','status','base_frame','path_points']){
        assert(before[key]!==undefined, key+' missing');
        assert.equal(after[key],before[key],name+' '+key);
      }
      assert.notEqual(before.request_id,after.request_id);
      assert(Number.isInteger(before.evaluation_seq) && after.evaluation_seq>before.evaluation_seq);
    }
    await chapter('停用后 · 同一输入生成同一条绕行曲线',3300);
    await page.click('#view-top');
    await page.waitForFunction(()=>document.getElementById('plan-map').dataset.view==='top');
    await chapter('俯视复核候选路径',2600);
    const film=await recording.finish();recording=null;
    const video=paths['scene-layers.mp4'];
    tool(ffmpeg,['-hide_banner','-loglevel','error','-nostdin','-framerate','24','-i',film.sequence,'-frames:v',String(film.total),'-an','-c:v','libx264','-preset',
      'veryfast','-crf','18','-pix_fmt','yuv420p','-movflags','+faststart',video]);
    const info=JSON.parse(tool(ffprobe,['-v','error','-select_streams','v:0','-show_entries',
      'stream=codec_name,width,height,r_frame_rate,duration,nb_frames','-of','json',video])).streams[0];
    assert.equal(info.codec_name,'h264');assert.equal(info.width,1600);assert.equal(info.height,1000);
    assert(Number(info.duration)>chapters.at(-1).sec && Number(info.duration)>=30,'Invalid chapter/duration alignment');
    tool(ffmpeg,['-v','error','-xerror','-nostdin','-i',video,'-f','null','-']);
    assert.deepEqual(issues,[]);
    assert.equal(Number(info.nb_frames),film.total);
    assert(Math.abs(Number(info.duration)-film.elapsed)<1/24,'Capture clock and encoded media diverged');
    audit.media={...info,file:path.basename(video),sha256:sha256(video),full_decode:true,captured_frames:film.frames,
      capture_duration_sec:film.elapsed,
      cover:'cover.png',cover_sha256:sha256(paths['cover.png'])};
    audit.passed=true;
  } finally {
    if(recording)await recording.finish().catch(()=>{});
    if(browser)await browser.close();
    for(const file of fs.readdirSync(tmp)){
      if(/^(?:frame|tick)-\d+\.jpg$/.test(file))fs.unlinkSync(path.join(tmp,file));
    }
    fs.rmdirSync(tmp);
    fs.writeFileSync(paths['acceptance.json'],fmt(audit));
  }
}
record().catch(error=>{console.error(error);process.exitCode=1;});
