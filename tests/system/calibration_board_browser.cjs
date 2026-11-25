// Observe the live station and submit one task through its regular Console form.
// Moving the simulated operator's board is a separate fixture responsibility.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const puppeteer = require(process.env.PUPPETEER_MODULE || 'puppeteer-core');
const delay = ms => new Promise(resolve => setTimeout(resolve, ms));

async function main() {
  const out = path.resolve(process.argv[2]);
  await fs.mkdir(out, {recursive: true});
  const origin = 'http://127.0.0.1:' + (process.env.EDGE_CALIBRATION_CONSOLE_PORT || '8911');
  const state = await (await fetch(origin + '/api/state')).json();
  assert.equal(state.environment, 'simulation');
  assert.equal(state.calibration_status_available, true);
  const status = async () => {
    const reply = await fetch(origin + '/api/calibration-status', {signal: AbortSignal.timeout(5000)});
    assert.equal(reply.status, 200);
    return reply.json();
  };
  const recognitionOnly=process.argv[3]==='sign';
  const repeat=process.argv[3]==='repeat';
  const clockMode = { 'clock-moving':'moving', 'clock-static':'static' }[process.argv[3]];
  const resumedId=process.argv[3]==='watch'?process.argv[4]:null;
  const initial=await status();
  const clockProof = ['clock-proof-moving','clock-proof-static'].includes(process.argv[3]);
  if(!resumedId)assert.equal(initial.state, recognitionOnly||repeat||clockMode||clockProof?'VERIFIED':'NEEDS_CALIBRATION');
  const browser = await puppeteer.launch({headless:true, executablePath:process.env.CHROME_PATH});
  try {
    const page = await browser.newPage();
    await page.setViewport({width:1440, height:900});
    const writes = [];
    page.on('request', req => { if(req.method()==='POST') writes.push(new URL(req.url()).pathname); });
    const task = clockMode?'请为当前两台 MID360 与 D435 自主标定软件时间偏移。':
      recognitionOnly?'请识别当前画面中的校园标志牌。':'请帮我标定当前传感器';
    const save = (file, value) => fs.writeFile(path.join(out, file), JSON.stringify(value,null,2)+'\n');
    await page.goto(resumedId ? origin+'/#sessions?task='+encodeURIComponent(resumedId) : origin);
    if (clockProof) {
      const moving = process.argv[3]==='clock-proof-moving';
      await page.waitForFunction(ok => {
        const text = document.querySelector('#calibration-clock')?.textContent || '';
        return ok ? text.includes('lidar_left') && text.includes('lidar_right') :
          text.includes('excitation') || text.includes('激励');
      },{},moving);
      const panel = await page.$('#calibration-panel');
      await panel.screenshot({path:path.join(out,(moving?'moving':'static')+'-task.png')});
      assert.deepEqual(writes,[]);
      return;
    }
    let accepted={id:resumedId};
    if(!resumedId){
      await page.waitForSelector(recognitionOnly||repeat||clockMode?'#calibration-panel':'#calibration-draft', {visible:true});
      await page.screenshot({path:path.join(out, 'startup.png')});
      await page.click('#open-task');
      await page.type('#task-text', task);
      await page.screenshot({path:path.join(out, 'request.png')});
      const submitted = page.waitForResponse(r => new URL(r.url()).pathname==='/agent/tasks'
        && r.request().method()==='POST');
      await page.click('#task-submit');
      accepted = await (await submitted).json();
      assert.ok(accepted.id && accepted.text===task);
      await save('accepted.json', {id:accepted.id, text:task});
    }
    const changes = [];
    const deadline = Date.now()+20*60*1000;
    let result;
    while(Date.now()<deadline) {
      const facts = await status();
      const decision = await (await fetch(origin+'/agent/tasks?id='+encodeURIComponent(accepted.id))).json();
      const last = changes.at(-1);
      if (!last || last.phase!==facts.phase || last.message!==facts.message || last.state!==facts.state) {
        changes.push({utc:new Date().toISOString(), ...facts});
        await save('station-events.json', changes);
      }
      if (['SUCCEEDED','FAILED','REJECTED','BLOCKED','CANCELED','RECOVERY_REQUIRED'].includes(decision.state)) {
        result=decision;
        break;
      }
      await delay(850);
    }
    assert.ok(result, 'Agent task did not finish within the station test window');
    await save('task.json', result);
    if(clockMode==='static') assert.ok(['BLOCKED','FAILED'].includes(result.state), result.detail);
    else assert.equal(result.state,'SUCCEEDED', result.detail);
    const final=await status();
    assert.equal(final.state,'VERIFIED');
    if(clockMode) {
      assert.equal(final.revision, initial.revision, 'clock test changed spatial calibration');
      assert.equal(final.samples, initial.samples);
      await save('spatial.json', {before:initial.revision, after:final.revision});
    }
    if(repeat){
      assert.equal(final.task_id, accepted.id);
      assert.notEqual(final.revision, initial.revision);
    }
    if(recognitionOnly){
      assert.ok(result.steps.length>0);
      assert.equal(final.revision, initial.revision);
      assert.equal(final.samples, initial.samples);
      assert.equal(final.task_id, initial.task_id);
      assert.ok(changes.every(row=>row.state==='VERIFIED'));
    }
    assert.deepEqual(writes, resumedId?[]:['/agent/tasks']);
    await page.waitForFunction(()=>document.querySelector('#calibration-title').textContent==='已验证标定结果');
    await page.screenshot({path:path.join(out, 'result.png')});
    console.log(JSON.stringify({id:accepted.id, state:result.state, submissions:writes.length}));
  } finally { await browser.close(); }
}
main().catch(error=>{console.error(error); process.exitCode=1;});
