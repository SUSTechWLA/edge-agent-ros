// UI contract test. Status and transport are local fixtures; no ROS publishers are contacted.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const puppeteer = require(process.env.PUPPETEER_MODULE || 'puppeteer-core');

async function main() {
  const out = process.argv[2];
  if (out) await fs.mkdir(out, {recursive:true});
  const root = path.resolve(__dirname, '../../console/web');
  const writes = [], failures = [];
  const method = {id:'four_hole_board',label:'四孔 ArUco 板 · Edge 多姿态外参',
    target:'four_hole_board', min_fit:3, min_holdout:2};
  const status = {method,board_geometry:{confirmed:false, profile_sha256:'first',suggestion_source:'测试加工图 A版',
    suggested_metrics:{marker_size_m:.11,marker_spacing_m:[.85,.6]}},state:'NEEDS_CALIBRATION',
    samples:0,message:'请核对标定板实测尺寸。',capture_feedback:null};
  let task, image, odom, draftGrant=false, stationOffline = false, readOnly = false;
  let preflight = null;
  let cameraMode='normal', lastFrame, liveRequests=0, activeReads=0, peakReads=0, historicReads=0;
  const server = http.createServer(async (req,res) => {
    const url = new URL(req.url,'http://localhost');
    function send(value, type='application/json',code=200) {
      res.writeHead(code,{'Content-Type':type,'Cache-Control':'no-store'});
      res.end(type==='application/json' ? JSON.stringify(value) : value);
    }
    if (req.method === 'POST') {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = JSON.parse(Buffer.concat(chunks));
      writes.push({path:url.pathname, body});
      if (url.pathname === '/api/calibration-board-geometry') {
        status.board_geometry={...body,profile_sha256:'second'};
        return send({saved:true});
      }
      if (url.pathname === '/agent/tasks') {
        task = {id:body.id, text:body.text, state:'RUNNING',steps:[]};
        Object.assign(status,{task_id:task.id,state:'CALIBRATING',phase:'OBSERVING'});
        return send(task);
      }
      if (url.pathname === '/agent/retry' && body.id === task.id && task.can_retry) {
        task.state = 'RUNNING';
        task.can_retry = false;
        Object.assign(status,{phase:'RECORDING',state:'CALIBRATING'});
        return send(task);
      }
    }
    if(url.pathname==='/api/state')return send({environment:'simulation',robot_id:'UI fixture',sources:{odom},
      task_proxy_mode:readOnly?'observe':'agent',calibration_status_available:true, cursor:0});
    if(url.pathname==='/api/calibration-status')return stationOffline?send({error:'测试工位离线'},'application/json',503):send(status);
    if(url.pathname==='/api/calibration-clock')return send({state:'NEEDS_CALIBRATION'});
    if(url.pathname==='/api/calibration-motion-preflight') {
      if(preflight===null)return send({error:'snapshot_unavailable'},'application/json',503);
      if(preflight==='absent')return send({error:'uninstalled'},'application/json',404);
      return send(preflight);
    }
    const older={id:'older',text:'历史任务',state:'SUCCEEDED'};
    if(url.pathname==='/api/sessions'){
      const all=task?[task,older]:[older];
      return send({counts:{all:all.length,active:all.filter(t=>['RUNNING','WAITING','PLANNING'].includes(t.state)).length,
        succeeded:all.filter(t=>t.state==='SUCCEEDED').length,issues:all.filter(t=>t.state==='BLOCKED').length},tasks:all,next_before:null});
    }
    if(url.pathname==='/api/session'){
      const current=url.searchParams.get('task_id')===task?.id?task:older;
      return send({task:current,headline:current.state,messages:[],coverage:{archived_events:0}});
    }
    if(url.pathname==='/api/calibration-preview.jpg'){
      historicReads++;
      return send(image,'image/jpeg');
    }
    if(url.pathname==='/api/calibration-live-frame'){
      liveRequests++;
      activeReads++;
      peakReads=Math.max(peakReads,activeReads);
      res.on('close',()=>activeReads--);
      if(cameraMode==='hang')return;
      if(!image || cameraMode==='offline')return send({code:'camera_unavailable'},'application/json',503);
      const stamp=String(BigInt(Date.now())*1000000n);
      if(cameraMode!=='repeated')lastFrame={source:'live_camera',camera_id:'D435',frame_id:'rgb_optical',
        robot_id:'UI fixture',environment:'simulation',stamp_ns:stamp,received_unix_ns:stamp,
        age_ms:cameraMode==='stale'?5000:0,max_age_ms:1500,width:720,height:400,jpeg:image.toString('base64')};
      return send(lastFrame);
    }
    if(url.pathname==='/agent/tasks' && task)return send(task);
    if(url.pathname==='/agent/capabilities' && url.searchParams.get('text')==='请帮我标定当前传感器')
      return send({goal_motion:{goal_tool:'verify_sensor_extrinsics',tools:draftGrant?['drive']:[]}});
    if(url.pathname==='/api/task-context' && task && url.searchParams.get('task_id')===task.id)
      return send({task,events:[]});
    if(url.pathname.startsWith('/agent/') || url.pathname.startsWith('/api/'))return send({
      tasks:[],events:[],capabilities:[],sessions:[]});
    const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
    if(!/^[\w.-]+$/.test(file))return send({},'application/json',404);
    try{
      const type={'.html':'text/html; charset=utf-8','.css':'text/css','.js':'application/javascript'}[path.extname(file)];
      if(!type)return send({},'application/json',404);
      send(await fs.readFile(path.join(root,file)),type);
    }catch{send({},'application/json',404);}
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  let browser;
  try {
    browser = await puppeteer.launch({headless:true,executablePath:process.env.CHROME_PATH});
    const page = await browser.newPage();
    page.on('pageerror', err=>failures.push(err.message));
    await page.setViewport({width:1320,height:960});
    await page.goto('http://127.0.0.1:'+server.address().port);
    // A visibly labelled preview tests browser decoding without impersonating a physical capture.
    image = Buffer.from(await page.evaluate(() => {
      const canvas=document.createElement('canvas');canvas.width=720;canvas.height=400;
      const c=canvas.getContext('2d');
      c.fillStyle='#263c3f';c.fillRect(0,0,720,400);
      c.fillStyle='#eff2e8';c.font='26px sans-serif';c.textAlign='center';
      c.fillText('浏览器界面测试帧',360,200);
      return canvas.toDataURL('image/jpeg').split(',')[1];
    }),'base64');
    async function wait(text,selector) {
      await page.waitForFunction((selector,expected)=>document.querySelector(selector)?.textContent.includes(expected),
        {timeout:9000},selector,text);
    }
    async function shot(name) {
      if(out)await (await page.$('#calibration-panel')).screenshot({path:path.join(out,name+'.png')});
    }
    await wait('先登记','#cal-instruction');
    assert.equal(await page.$eval('#cal-motion',e=>e.hidden),true);
    preflight={schema:'edge.calibration.motion-preflight.v1',task_id:'motion-check-only',
      observed_at_ns:String(BigInt(Date.now())*1000000n),valid_for_ms:4500,status:'blocked',
      checks:[{label:'急停输入',ok:false,detail:'未收到状态'},{label:'车轮反馈',ok:true,detail:'已收到'}],last_test:null};
    await wait('本次预检未通过','#cal-motion-state');
    await wait('未收到测试回执','#cal-motion-receipt');
    await wait('急停输入 · 未通过','#cal-motion-checks');
    assert.equal(await page.$eval('#cal-motion',e=>e.querySelectorAll('button,a,input').length),0);
    await wait('当前未知','#cal-motion-state');
    assert.equal(await page.$eval('#cal-motion-state',e=>e.dataset.state),'unknown');
    preflight={...preflight,status:'ready',observed_at_ns:String(BigInt(Date.now())*1000000n),valid_for_ms:5500,
      checks:[{label:'通信检查',ok:true,detail:'正常'}],last_test:{status:'verified',summary:'台架测试回执'}};
    await wait('本次预检通过','#cal-motion-state');
    await wait('已收到 · verified · 台架测试回执','#cal-motion-receipt');
    preflight='absent';
    await page.waitForFunction(()=>document.querySelector('#cal-motion').hidden);
    await wait('D435','#cal-preview-caption');
    const visible=()=>page.waitForFunction(()=>!document.querySelector('#cal-preview').hidden &&
      document.querySelector('#cal-preview').naturalWidth>0,{timeout:9000});
    const cleared=()=>page.waitForFunction(()=>document.querySelector('#cal-preview').hidden &&
      !document.querySelector('#cal-preview').hasAttribute('src'),{timeout:4000});
    await visible(); // No calibration task exists yet.
    const firstStamp=await page.$eval('#cal-preview',img=>img.dataset.stampNs);
    cameraMode='hang';
    await cleared(); // Already displayed image expires even while a request is stuck.
    cameraMode='normal';
    await visible();
    const recoveredStamp=await page.$eval('#cal-preview',img=>img.dataset.stampNs);
    assert.ok(BigInt(recoveredStamp)>BigInt(firstStamp));
    cameraMode='repeated';
    await cleared(); // Repeated old capture with a misleading age of zero cannot renew.
    cameraMode='normal';
    await visible();
    cameraMode='offline';
    await cleared();
    cameraMode='stale';
    const initialReads=liveRequests;
    await page.waitForFunction(()=>document.querySelector('#cal-preview-empty').textContent.includes('新鲜'));
    const deadline=Date.now()+4000;
    while(liveRequests<initialReads+2 && Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,80));
    assert.ok(liveRequests>=initialReads+2);
    assert.equal(await page.$eval('#cal-preview',img=>img.hasAttribute('src')),false);
    cameraMode='normal';
    await visible();
    assert.equal(await page.$eval('[name=markerEdge]',x=>x.value),'');
    await shot('01-unmeasured-board');
    await page.type('#cal-board-id','test-board');
    await page.type('[name=markerEdge]','100');
    await page.click('#cal-fill-reference');
    assert.deepEqual(await page.$eval('#cal-geometry-form',f=>[f.elements.markerEdge.value,f.elements.markerX.value,
      f.elements.markerY.value,f.elements.holeX.value,document.getElementById('cal-confirm-measure').checked]),
      ['100','850','600','',false]);
    assert.deepEqual(writes,[]);
    const dimensions={holeDiameter:200,holeX:400,holeY:300};
    for(const [name,value] of Object.entries(dimensions))await page.type('[name='+name+']',String(value));
    await page.click('#cal-confirm-measure');
    await page.click('#cal-save-board');
    await wait('已登记','#cal-board-status');
    assert.deepEqual(writes[0].body,{board_id:'test-board',confirmed:true,expected_profile_sha256:'first',
      metrics:{marker_size_m:.1,marker_spacing_m:[.85,.6],hole_diameter_m:.2,hole_spacing_m:[.4,.3]}});
    await wait('填写标定任务','#calibration-draft');
    draftGrant=true;
    await wait('填写自主换位标定任务','#calibration-draft');
    await page.click('#calibration-draft');
    assert.equal(await page.$eval('#task-text',x=>x.value),'请帮我标定当前传感器');
    await page.click('#task-submit');
    await page.waitForFunction(()=>!document.querySelector('#task-modal').open);
    assert.equal(writes[1].path,'/agent/tasks');
    status.phase='ADJUST_BOARD';
    status.capture_feedback={condition:'insufficient_points',guidance:'右雷达的孔缘缺失，请稍微转动板面。',
      preview_stamp_ns:'101',camera:{marker_ids:[1,2,3,4]},
      lidars:{lidar_left:{hole_count:4,point_count:841},lidar_right:{hole_count:2,point_count:94}},
      clip:{frames:0,duration_s:0,required_frames:4,required_duration_s:1}};
    await wait('右雷达的孔缘缺失','#cal-guidance');
    await wait('2 / 4 孔','#cal-readings');
    const operation=task.id+'/step/2';
    const move={status:'RUNNING',operation_id:operation,physical_cap:{effect:'actuation'},
      physical_pin:{class:'physical',reservation_id:operation,verifier_id:'pose_witness'}};
    const inspect={tool:'inspect_calibration_view',status:'RUNNING',operation_id:task.id+'/step/1',
      feedback:{goal:{tool:'verify_sensor_extrinsics'},required_motion_tools:['navigate'],placement_required:true}};
    task.steps=[inspect];
    await wait('车头正前方','#cal-instruction');
    await wait('底边触地','#cal-guidance');
    Object.assign(inspect,{status:'SUCCEEDED',evidence:{front_placement_confirmed:true}});
    await wait('已确认正面板位','#cal-instruction');
    task.steps.push(move);
    odom={fresh:true,data:{v:0,w:.1}};
    await wait('底盘正在自主换位','#cal-instruction');
    assert.doesNotMatch(await page.$eval('#cal-guidance',e=>e.innerText),/转动板面/);
    odom={fresh:true,data:{v:0,w:0}};
    await wait('等待底盘换位回执','#cal-instruction');
    Object.assign(move,{status:'SUCCEEDED',evidence:{outcome:'arrived',verifier_id:'pose_witness',
      verification:{operation_id:operation,position_error_m:.015,heading_error_rad:.01}}});
    await wait('底盘已到位','#cal-instruction');
    await wait('0.015 米','#cal-readings');
    task.steps=[];
    odom=undefined;
    await wait('右雷达的孔缘缺失','#cal-guidance');
    await page.evaluate(()=>{location.hash='sessions';});
    await page.waitForFunction(id=>new URLSearchParams(location.hash.split('?')[1]).get('task')===id,{},task.id);
    await page.evaluate(()=>{location.hash='sessions?task=older';});
    await page.waitForFunction(()=>document.querySelector('[data-task-id="older"]')?.getAttribute('aria-current')==='true');
    assert.equal(new URL(page.url()).hash,'#sessions?task=older');
    await page.waitForFunction(()=>document.querySelector('#cal-preview').naturalWidth>0);
    await shot('02-retake-guidance');
    Object.assign(status.capture_feedback.clip,{frames:3,duration_s:.8});
    status.capture_feedback={...status.capture_feedback,condition:'holding',guidance:'请继续举稳板。',preview_stamp_ns:'102'};
    status.phase='ACQUIRING';
    await wait('3 / 4 帧','#cal-readings');
    Object.assign(status,{phase:'PAUSED_FOR_OPERATOR',state:'PAUSED_FOR_OPERATOR',samples:2});
    Object.assign(task,{state:'BLOCKED',can_retry:true});
    await wait('原步骤','#cal-guidance');
    await page.click('#cal-resume');
    await page.waitForFunction(()=>document.querySelector('#cal-resume').hidden);
    assert.equal(writes[2].body.id,task.id);
    Object.assign(status,{state:'VERIFIED',samples:5,revision:'fixture-revision'});
    status.capture_feedback.video_available=true;
    await wait('独立验证通过','#cal-instruction');
    assert.equal(await page.$eval('#cal-active-task',a=>new URLSearchParams(a.hash.split('?')[1]).get('task')),task.id);
    assert.equal(await page.$eval('#cal-download',x=>x.hidden),false);
    assert.equal(await page.$eval('#cal-download',x=>new URL(x.href).searchParams.get('task_id')),task.id);
    await shot('03-verified');
    await page.setViewport({width:390,height:844});
    assert.equal(await page.$eval('#cal-station',e=>e.scrollWidth<=e.clientWidth),true);
    readOnly=true;
    await page.waitForFunction(()=>document.querySelector('#cal-save-board').disabled);
    status.method={id:'custom',label:'用户标定适配器',target:'custom',guided_capture:true,min_fit:4,min_holdout:1};
    await wait('用户标定适配器','#cal-method');
    assert.equal(await page.$eval('#cal-station',x=>x.hidden),false);
    assert.equal(await page.$eval('#cal-board',x=>x.hidden),true);
    stationOffline=true;
    await wait('标定状态未知','#calibration-title');
    assert.equal(await page.$eval('#cal-station',x=>x.hidden),true);
    // The sensor cursor in this fixture is always zero. Focus and an open evidence panel must survive completion.
    task={id:'poll-proof',text:'独立账本轮询',state:'WAITING',steps:[]};
    await page.evaluate(()=>{location.hash='sessions?task=poll-proof';});
    await wait('等待结果','#session-state');
    await page.waitForSelector('[data-task-id="poll-proof"]');
    await page.evaluate(()=>{
      document.querySelector('#session-coverage').open=true;
      document.querySelector('[data-task-id="poll-proof"]').focus();
    });
    task.state='SUCCEEDED';
    await wait('已完成','#session-state');
    await wait('0','[data-session-state="active"] strong');
    await wait('2','[data-session-state="succeeded"] strong');
    assert.equal(await page.evaluate(()=>document.activeElement?.dataset.taskId),'poll-proof');
    assert.equal(await page.$eval('#session-coverage',e=>e.open),true);
    assert.deepEqual(writes.map(w=>w.path),['/api/calibration-board-geometry','/agent/tasks','/agent/retry']);
    assert.deepEqual(failures,[]);
    assert.equal(peakReads,1);
    assert.equal(historicReads,0);
    console.log('PASS: independent live frames, single-flight timeout, expiry, duplicate rejection, disconnect/recovery, archive isolation and calibration operator flows');
  } finally {
    if(browser)await browser.close();
    await new Promise(resolve=>server.close(resolve));
  }
}
main().catch(err=>{console.error(err);process.exitCode=1;});
