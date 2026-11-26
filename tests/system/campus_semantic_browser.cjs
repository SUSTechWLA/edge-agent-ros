/** Submit ordinary language through the campus workbench and correlate each task with a Gazebo witness. */
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const {execFile} = require('node:child_process');
const {promisify} = require('node:util');
const {parseArgs} = require('node:util');
const {values: args} = parseArgs({options:{
  browser:{type:'string'},puppeteer:{type:'string',default:'puppeteer-core'},
  ffmpeg:{type:'string'},out:{type:'string'},trial:{type:'string'},
}});
assert(args.out && args.browser && /^[a-z0-9-]{1,24}$/.test(args.trial));
const origin='http://127.0.0.1:8910';
const cases=[
  {key:'outdoor',text:'前往门外北侧等候点',entity:'teaching-exit-north-wait',goal:[3.30,.85]},
  {key:'ambiguous',text:'前往等候点'},
  {key:'unknown',text:'前往不存在的气象站'},
  {key:'indoor',text:'前往一楼门厅',entity:'teaching-lobby',goal:[0,0]},
];
const sleep=ms=>new Promise(resolve=>setTimeout(resolve,ms));
async function save(name,value) {
  const target=path.join(args.out,name);
  await fs.writeFile(target+'.tmp',JSON.stringify(value,null,2)+'\n');
  await fs.rename(target+'.tmp',target);
}
async function wait(fn,reason,seconds=100) {
  const until=Date.now()+seconds*1000;
  while(Date.now()<until) {
    const result=await fn();
    if(result)return result;
    await sleep(300);
  }
  throw new Error(reason);
}
async function file(name) {
  try {return JSON.parse(await fs.readFile(path.join(args.out,name),'utf8'));}
  catch(e){if(e.code!=='ENOENT')throw e;return null;}
}
async function api(route) {
  const r=await fetch(origin+route,{signal:AbortSignal.timeout(5000)});
  if(r.status===404)return null;
  assert(r.ok,'HTTP '+r.status+' '+route);
  return r.json();
}
async function main(){
  const world=await api('/api/state');
  assert.equal(world.environment,'simulation');assert(!world.active_operation);
  const map=await api('/api/exploration-mission?task_id=campus-teaching-survey');
  assert.equal(map.mission?.kind,'map_import');assert.equal(map.environment,'simulation');
  const browser=await require(args.puppeteer).launch({executablePath:args.browser,headless:true});
  const base='campus-phase1-'+args.trial+'-';
  const receipt={source:origin,run:base,map:map.mission.map_ref,tasks:[],visibleZones:[],browserErrors:[]};
  let recording=false, recordingJob;
  const frames=[];
  let captureStarted;
  const action=(name,id,extra={})=>{
    receipt.interactions.push({name,id,seconds:(performance.now()-captureStarted)/1000,utc:new Date().toISOString(),...extra});
  };
  receipt.interactions=[];
  try {
    const page=await browser.newPage();
    await page.setViewport({width:1440,height:960,deviceScaleFactor:1});
    page.on('pageerror',e=>receipt.browserErrors.push(String(e)));
    await page.goto(origin+'/exploration-mission.html?task_id=campus-teaching-survey');
    await page.waitForSelector('#submit-task:enabled',{timeout:60000});
    await page.exposeFunction('recordVisibleZone',sample=>receipt.visibleZones.push(sample));
    await page.$eval('#pose-zone',el=>{
      let last='';
      new MutationObserver(()=>{
        const id=document.body.dataset.taskId, zone=el.innerText.trim();
        if(!zone || last===id+' '+zone)return;
        last=id+' '+zone;
        void window.recordVisibleZone({id,zone,pose:document.querySelector('#pose').innerText,time:new Date().toISOString()});
      }).observe(el,{childList:true,subtree:true,characterData:true});
    });
    captureStarted=performance.now();
    if(args.ffmpeg){
      const folder=path.join(args.out,'frames');
      await fs.mkdir(folder);
      recording=true;
      recordingJob=(async()=>{
        while(recording){
          const at=performance.now();
          const name='frame-'+String(frames.length).padStart(5,'0')+'.jpg';
          await page.screenshot({path:path.join(folder,name),type:'jpeg',quality:75});
          frames.push({at,name});
          await sleep(Math.max(0,420-performance.now()+at));
        }
      })();
    }
    for(const scenario of cases) {
      const id=base+scenario.key;
      const route='/api/exploration-mission?task_id='+id;
      assert(!(await api(route))?.mission,'Choose unused task IDs');
      await save('stage.json',{id,goal:scenario.goal});
      await wait(()=>file(id+'.armed.json'),'Waiting for independent Gazebo truth '+id);
      const state=await api('/api/state');
      assert.equal(state.environment,'simulation');
      assert(!state.active_operation && state.sources.odom.fresh && state.sources.scan.fresh);
      await page.waitForSelector('#submit-task:enabled',{timeout:18000});
      await page.$eval('#task-key',(e,id)=>e.value=id,id);
      await page.click('#task-text',{clickCount:3});await page.keyboard.press('Backspace');
      action('input.empty',id);
      await sleep(550);
      for(const c of scenario.text){await page.keyboard.sendCharacter(c);await sleep(80);}
      action('input.complete',id,{text:scenario.text});
      await sleep(600);
      const submitted=page.waitForResponse(r=>r.request().method()==='POST' && new URL(r.url()).pathname==='/agent/tasks');
      await page.click('#submit-task');
      const response=await submitted;
      assert.equal(response.status(),202);
      const accepted=await response.json();assert.equal(accepted.id,id);assert.equal(accepted.text,scenario.text);
      action('task.accepted',id,{text:scenario.text});
      let showedGlobal=false;
      const snapshot=await wait(async()=>{
        const result=(await api(route))?.mission;
        if(!showedGlobal && scenario.goal && result?.status==='RUNNING' && result.initial_pose && result.pose &&
           result.last_navigation_path?.points?.length>1 &&
           Math.hypot(result.pose.x-result.initial_pose.x,result.pose.y-result.initial_pose.y)>.30) {
          showedGlobal=true;
          await page.click('#view-global');
          action('map.global',id);
          await sleep(1800);
          await page.click('#view-local');
          action('map.local',id);
          if(scenario.key==='outdoor') {
            await page.waitForFunction(()=>document.querySelector('#view-local').getAttribute('aria-pressed')==='true' &&
              Number(document.querySelector('#map').dataset.cameraTilt)<1.44);
            const mapCanvas=await page.$('#map');
            const rect=await mapCanvas.boundingBox();
            const x=rect.x+rect.width*.45,y=rect.y+rect.height*.45;
            const startingYaw=Number(await mapCanvas.evaluate(e=>e.dataset.cameraYaw));
            await page.mouse.move(x,y);
            await page.mouse.down({button:'right'});
            await page.mouse.move(x+56,y+20,{steps:12});
            await page.mouse.up({button:'right'});
            await page.waitForFunction(yaw=>Math.abs(Number(document.querySelector('#map').dataset.cameraYaw)-yaw)>.15,{},startingYaw);
            await page.mouse.wheel({deltaY:-100});
            await page.waitForFunction(()=>Number(document.querySelector('#map').dataset.cameraZoom)>1.1);
            action('map.orbitZoom',id,await mapCanvas.evaluate(e=>({yaw:Number(e.dataset.cameraYaw),zoom:Number(e.dataset.cameraZoom)})));
            const from=await mapCanvas.evaluate(e=>[Number(e.dataset.cameraX),Number(e.dataset.cameraY)]);
            await page.mouse.down();
            await page.mouse.move(x+22,y+20,{steps:9});
            await page.mouse.up();
            await page.waitForFunction(([x,y])=>{
              const camera=document.querySelector('#map').dataset;
              return Math.hypot(Number(camera.cameraX)-x,Number(camera.cameraY)-y)>.025;
            },{},from);
            action('map.pan',id);
            await sleep(650);
            await page.click('#view-reset');
            action('map.reset',id);
          }
        }
        return result && ['SUCCEEDED','BLOCKED','REJECTED','FAILED','RECOVERY_REQUIRED','NEEDS_INPUT'].includes(result.status) && result;
      },'Timed out waiting for navigation session '+id,180);
      const agent=await wait(async()=>{
        const a=await api('/agent/tasks?id='+id+'&limit=64');
        return a && ['SUCCEEDED','BLOCKED','FAILED','REJECTED','RECOVERY_REQUIRED','CANCELED'].includes(a.state) && a;
      },'Timed out waiting for Agent terminal receipt '+id,50);
      await save(id+'.terminal.json',{mission:snapshot,agent});
      const truth=await wait(()=>file(id+'.world.json'),'Missing terminal world evidence '+id);
      action('task.terminal',id,{agent:agent.state,session:snapshot.status});
      const row={id,text:scenario.text,status:snapshot.status,reason:snapshot.reason,agent:agent.state,
                 goal:snapshot.goal,semantic:snapshot.destination,events:snapshot.events.map(e=>({kind:e.kind,data:e.data})),
                 map:snapshot.map_ref,world:truth};
      receipt.tasks.push(row);await save('browser.json',receipt);
      console.log(id, snapshot.status, snapshot.reason, truth.passed, truth.goal_error_m);
      await page.waitForFunction(id=>document.body.dataset.taskId===id &&
        !document.querySelector('#destination-panel').hidden,{},id);
      row.visibleDestination=await page.$eval('#destination-panel',panel=>panel.innerText);
      await page.screenshot({path:path.join(args.out,scenario.key+'.png')});
      action('terminal.screenshot',id,{file:scenario.key+'.png'});
      await save('browser.json',receipt);
      assert(truth.passed,JSON.stringify(truth));
      if(scenario.goal) {
        assert.equal(snapshot.status,'SUCCEEDED');assert.equal(agent.state,'SUCCEEDED');
        assert.deepEqual(snapshot.goal,scenario.goal);assert.deepEqual(snapshot.map_ref,map.mission.map_ref);
        assert.equal(snapshot.destination.state,'resolved');assert.equal(snapshot.destination.entity_id,scenario.entity);
        assert(snapshot.destination.source_kind && snapshot.destination.zone && snapshot.destination.environment);
      } else {
        assert(!snapshot.events.some(e=>e.kind==='route_planned'),'Unexpected motion plan');
        assert.equal(snapshot.status,'BLOCKED');assert.equal(snapshot.destination.state,scenario.key);
        assert.equal(snapshot.destination.candidates.length,scenario.key==='ambiguous' ? 2 : 0);
      }
      if(scenario.goal)assert(row.visibleDestination.includes(snapshot.destination.name));
      else {
        assert(row.visibleDestination.includes(scenario.key==='ambiguous'?'需要区分地点':'未找到地点'));
        for(const c of snapshot.destination.candidates)assert(row.visibleDestination.includes(c.name));
      }
    }
    for(const [key,sequence] of [['outdoor',['室内','室内外过渡区','室外']],['indoor',['室外','室内外过渡区','室内']]]){
      let offset=0;
      for(const sample of receipt.visibleZones.filter(s=>s.id===base+key)){
        if(sample.zone.split(' · ')[0]===sequence[offset])offset++;
      }
      assert.equal(offset,sequence.length,'Missing indoor/outdoor transition in live UI: '+key);
    }
    assert.deepEqual(receipt.browserErrors,[]);
    receipt.passed=true;
    await save('browser.json',receipt);
  } finally {
    await fs.writeFile(path.join(args.out,'complete'),'');
    recording=false;
    await recordingJob;
    await browser.close();
    if(frames.length>1){
      await save('frame-index.json',{source:'Chrome screenshots from the real campus task page',rate:'fixed wall-clock playback, about 2.4 samples/second',videoSpeed:1,
        firstFrameSeconds:(frames[0].at-captureStarted)/1000,frames:frames.map(f=>({file:'frames/'+f.name,seconds:(f.at-frames[0].at)/1000}))});
      const list=frames.map((f,i)=>"file 'frames/"+f.name+"'\nduration "+
        (((frames[i+1]?.at ?? f.at+420)-f.at)/1000).toFixed(3)).join('\n');
      await fs.writeFile(path.join(args.out,'frames.txt'),list+"\nfile 'frames/"+frames.at(-1).name+"'\n");
      await promisify(execFile)(args.ffmpeg,['-hide_banner','-loglevel','error','-f','concat','-safe','1','-i','frames.txt',
        '-vf','fps=12','-c:v','libx264','-threads','2','-preset','fast','-crf','21','-pix_fmt','yuv420p',
        '-movflags','+faststart','campus-navigation.mp4'],{cwd:path.resolve(args.out),windowsHide:true});
    }
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
