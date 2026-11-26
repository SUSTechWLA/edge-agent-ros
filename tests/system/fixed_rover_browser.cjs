// Record a real loopback Console. In --submit mode only the actual form's single Gazebo task POST is allowed.
'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const assert = require('node:assert/strict');
const puppeteer = require(process.env.PUPPETEER_MODULE || 'puppeteer-core');
const [taskArg, target, origin = 'http://127.0.0.1:8916', witness, suppliedGoal] = process.argv.slice(2);
const submit=taskArg==='--submit', goal=suppliedGoal || '请帮我标定当前传感器';
const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
const terminal = new Set(['SUCCEEDED','BLOCKED','FAILED','CANCELED','RECOVERY_REQUIRED','REJECTED']);
const terminalLabel={SUCCEEDED:'已完成',BLOCKED:'受阻',FAILED:'已失败',CANCELED:'已取消',RECOVERY_REQUIRED:'待恢复',REJECTED:'REJECTED'};
async function record() {
  let task=submit ? null : taskArg;
  assert((submit || /^[\w.-]{1,96}$/.test(task)) && new URL(origin).hostname === '127.0.0.1');
  if(submit) {
    assert(origin==='http://127.0.0.1:8916' && witness);
    const live=await (await fetch(origin+'/api/state')).json();
    assert(live.robot_id==='gazebo-lingao' && live.environment==='simulation');
  }
  const dir = path.resolve(target);
  await fs.mkdir(dir, {recursive:true});
  const book = path.join(dir,'receipts.jsonl');
  await fs.writeFile(book,'', {flag:'wx'});
  const browser = await puppeteer.launch({headless:true,executablePath:process.env.CHROME_PATH});
  try {
    const page = await browser.newPage();
    await page.setCacheEnabled(false);
    await page.setViewport({width:1440,height:960});
    await page.setRequestInterception(true);
    let posted=null;
    page.on('request', req => {
      if(req.method()==='GET')return req.continue();
      if(submit && !posted && req.method()==='POST' && req.url()===origin+'/agent/tasks') {
        const body=JSON.parse(req.postData());
        if(body.text===goal && typeof body.id==='string' && /^[\w.-]{1,96}$/.test(body.id)) {
          posted=body;
          return req.continue();
        }
      }
      return req.abort();
    });
    await page.goto(origin+'/#sessions',{waitUntil:'domcontentloaded'});
    await page.waitForSelector('#cal-station:not([hidden])',{timeout:45000});
    let lastState = '', terminalFrames = 0, frame = 0;
    async function capture(row){
      const slot=String(frame).padStart(5,'0');
      await page.screenshot({path:path.join(dir,slot+'.jpg'),type:'jpeg',quality:77});
      await fs.appendFile(book,JSON.stringify({index:frame++,task,...row})+'\n');
    }
    if(submit){
      // The passive witnesses precede the only submit. None of their observations are exposed to the Agent.
      await fs.access(path.join(witness,'witness-ready.json'));
      await page.waitForSelector('#open-task:not([disabled])');
      await page.click('#open-task');
      async function draftFrame(){
        await capture({at_ms:Date.now(),state:'DRAFT',panel:'form',goal:await page.$eval('#task-text',e=>e.value)});
      }
      await draftFrame();
      const chunks=goal.match(/.{1,5}/gu);
      for (const part of chunks){
        await page.type('#task-text',part,{delay:100});
        await draftFrame();
      }
      const reply=page.waitForResponse(r=>r.url()===origin+'/agent/tasks' && r.request().method()==='POST',{timeout:15000});
      await page.click('#task-submit');
      const response=await reply;
      assert(response.ok() && posted);
      const record=await response.json();
      assert.equal(record.id,posted.id);
      task=posted.id;
      const receipt=path.join(witness,'ui-receipt.json');
      await fs.writeFile(receipt+'.tmp',JSON.stringify(posted),{flag:'wx'});
      await fs.rename(receipt+'.tmp',receipt);
      console.log(JSON.stringify({task,text:posted.text,stage:'console_submitted'}));
      await page.waitForFunction(()=>!document.querySelector('#task-modal').open);
      await page.evaluate(id=>location.hash='sessions?task='+encodeURIComponent(id),task);
      await page.waitForFunction(text=>document.querySelector('#session-goal')?.textContent===text,{timeout:8000},goal);
    }
    for (let index=0;; index++) {
      const at = Date.now();
      let snapshot;
      try {
        const reply = await fetch(origin+'/agent/tasks?id='+encodeURIComponent(task)+'&limit=1',{signal:AbortSignal.timeout(4000)});
        snapshot = reply.ok ? await reply.json() : {};
      } catch { await wait(500); continue; }
      if (!snapshot.state) { await wait(350); continue; }
      if (!frame) {
        // A cold Gazebo start can reset source timestamps while waiting. Open a fresh real page.
        await page.goto('about:blank');
        await page.goto(origin+'/#sessions?task='+encodeURIComponent(task),{waitUntil:'domcontentloaded'});
        await page.waitForFunction(text=>document.querySelector('#session-goal')?.textContent===text,
                                   {timeout:8000},snapshot.text);
      }
      const placement=await page.$eval('#cal-instruction',e=>e.innerText.includes('车头正前方'));
      const done=terminal.has(snapshot.state);
      const showSession = done ? terminalFrames===1 : frame===0 || (!placement && (frame < 4 || frame % 34 >= 29));
      await page.evaluate(session => {
        const panel=document.querySelector(session ? '#session-reader' : '#calibration-panel');
        if(panel) panel.scrollIntoView({block:'start',behavior:'instant'});
      },showSession);
      const active = await page.evaluate(id => ({
        instruction:document.querySelector('#cal-instruction')?.innerText,
        guidance:document.querySelector('#cal-guidance')?.innerText || '',
        readings:document.querySelector('#cal-readings')?.innerText || '',
        camera:document.querySelector('#cal-preview')?.dataset.stampNs || null,
        caption:document.querySelector('#cal-preview-caption')?.innerText || '',
        goal:document.querySelector('#session-goal')?.innerText || '',
        result:document.querySelector('#cal-result')?.innerText || '',
        sessionState:document.querySelector('#session-state')?.innerText,
        listState:document.querySelector(`[data-task-id="${CSS.escape(id)}"] .badge`)?.innerText,
      }),task);
      await capture({at_ms:at,panel:showSession?'session':'camera',state:snapshot.state,step_count:snapshot.steps?.length,...active});
      if (snapshot.state !== lastState) {
        console.log(JSON.stringify({task,state:snapshot.state,at_ms:at,index}));
        lastState=snapshot.state;
      }
      if(done){
        terminalFrames=active.sessionState===terminalLabel[snapshot.state] && active.listState===active.sessionState
          ? terminalFrames+1 : 0;
        if(terminalFrames>=5)return;
      }
      await wait(Math.max(0,1200-(Date.now()-at)));
    }
  } finally {
    await browser.close();
  }
}
record().catch(e => {console.error(e);process.exitCode=1;});
