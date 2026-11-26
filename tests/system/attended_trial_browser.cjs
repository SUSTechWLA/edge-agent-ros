// Owns its loopback fixture. This test accepts no station URL and never connects to a vehicle.
'use strict';
const assert = require('node:assert/strict');
const http = require('node:http');
const fs = require('node:fs/promises');
const path = require('node:path');
const {chromium} = require('playwright');
const sleep = ms => new Promise(resolve => setTimeout(resolve,ms));

async function until(check, message) {
  for (let i=0;i<150;i++) { if (await check()) return; await sleep(15); }
  assert.fail(message);
}

async function run(browser, html, mode) {
  const profileId='forward-50mm-100mmps';
  const state = {profile_id:profileId,phase:'idle',reason:'',operation_id:null,ready:false,stopped:null,displacement_m:null,peak_speed_m_s:null};
  const writes = [], accepted = [], errors = [];
  const server = http.createServer(async (req,res) => {
    const reply = (data, status=200) => { res.writeHead(status,{'Content-Type':'application/json','Cache-Control':'no-store'});res.end(JSON.stringify(data)); };
    if (req.method === 'GET' && req.url === '/') {res.writeHead(200,{'Content-Type':'text/html; charset=utf-8'});res.end(html);return;}
    if (req.method === 'GET' && req.url === '/state') {
      if (mode === 'state-loss' && writes.filter(w=>w.path==='/hold').length>=3) reply({},503);
      else if (mode === 'state-reset' && writes.filter(w=>w.path==='/hold').length>=3) reply({...state,phase:'idle',operation_id:null});
      else reply(state);
      return;
    }
    if (req.method !== 'POST' || !['/begin','/hold','/release'].includes(req.url)) {reply({},404);return;}
    let raw='';for await (const part of req) raw+=part;
    const body=JSON.parse(raw);
    const entry={path:req.url,body,contentType:req.headers['content-type']};
    writes.push(entry);
    const last=accepted.at(-1);
    if ((!last && (req.url!=='/begin'||body.seq!==0)) ||
        (last && (last.body.operation_id!==body.operation_id || body.seq<=last.body.seq || req.url==='/begin'))) {
      reply({error:'expired or repeated sequence'},409);return;
    }
    accepted.push(entry);
    if (req.url === '/begin') {
      Object.assign(state,{phase:'running',operation_id:body.operation_id,ready:false,stopped:false,displacement_m:0,peak_speed_m_s:0});
      if (mode === 'lost-begin') await sleep(260);
    }
    if (req.url === '/hold' && mode === 'lost-pulse' && writes.filter(w=>w.path==='/hold').length>=2) { reply({error:'pulse rejected'},409);return; }
    if (req.url === '/hold' && mode === 'http-disconnect' && writes.filter(w=>w.path==='/hold').length>=2) {req.socket.destroy();return;}
    if (req.url === '/release') Object.assign(state,{phase:'stopping',reason:'操作人松开'});
    reply(state);
  });
  await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));
  const origin=`http://127.0.0.1:${server.address().port}`;
  const touch=['two-touches','touchcancel'].includes(mode);
  const ctx=await browser.newContext({serviceWorkers:'block',viewport:{width:760,height:730},hasTouch:touch});
  await ctx.route('**/*', route=>new URL(route.request().url()).origin===origin ? route.continue() : route.abort());
  const page=await ctx.newPage();page.on('pageerror',e=>errors.push(e.message));
  const count=p=>writes.filter(w=>w.path===p).length;
  try {
    await page.goto(origin);await page.bringToFront();
    const control=page.locator('#hold');
    assert(await control.isDisabled());
    assert.match(await page.locator('h1').textContent(),/前进 5 cm/);
    assert.match(await page.locator('main').textContent(),/0\.10 m\/s，名义时长 0\.5 秒/);
    Object.assign(state,{profile_id:'other-profile',phase:'ready',ready:true,stopped:true});
    await until(async()=>(await page.locator('#hint').textContent()).includes('不匹配'),'profile mismatch was not shown');
    assert(await control.isDisabled());
    state.profile_id=profileId;
    Object.assign(state,{phase:'ready',ready:true,stopped:true});
    await until(()=>control.isEnabled(),'button did not enable after ready');
    if (mode==='release' && process.env.EDGE_TEST_SCREENSHOTS) {
      const directory=process.env.EDGE_TEST_SCREENSHOTS;await fs.mkdir(directory,{recursive:true});
      await page.screenshot({path:path.join(directory,'fake-ready-desktop.png'),fullPage:true});
      await page.setViewportSize({width:375,height:760});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth===innerWidth));
      await page.screenshot({path:path.join(directory,'fake-ready-mobile.png'),fullPage:true});
      await page.setViewportSize({width:760,height:730});
    }
    await control.dispatchEvent('pointerdown',{isPrimary:true,pointerId:7,button:0,buttons:1});
    await control.focus();await page.keyboard.press('Enter');
    await control.click({button:'right'});await page.keyboard.press('Escape');
    assert.equal(writes.length,0,'untrusted, keyboard or secondary press caused a request');
    const box=await control.boundingBox(),x=box.x+box.width/2,y=box.y+box.height/2;
    const cdp=touch ? await ctx.newCDPSession(page) : null;
    if (cdp) await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:11}]});
    else { await page.mouse.move(x,y);await page.mouse.down(); }
    await until(()=>count('/begin')===1,'begin missing');
    if (mode==='lost-begin') {
      await until(()=>count('/release')===1,'begin timeout did not release');assert.equal(count('/hold'),0);
    } else if (['lost-pulse','http-disconnect','state-loss','state-reset'].includes(mode)) {
      await until(()=>count('/release')===1,'rejected heartbeat did not release');
    } else {
      await until(()=>count('/hold')>=3,'renewals missing');
      if (mode==='release') await page.mouse.up();
      if (mode==='blur') await page.evaluate(()=>window.dispatchEvent(new Event('blur')));
      if (mode==='offline') await page.evaluate(()=>window.dispatchEvent(new Event('offline')));
      if (mode==='leave') await page.mouse.move(box.x-5,y);
      if (mode==='cancel') await page.keyboard.press('Escape');
      if (mode==='touchcancel') await cdp.send('Input.dispatchTouchEvent',{type:'touchCancel',touchPoints:[]});
      if (mode==='two-touches') await cdp.send('Input.dispatchTouchEvent',{type:'touchStart',touchPoints:[{x,y,id:11},{x:x+14,y:y+7,id:12}]});
      if (mode==='stall') await page.evaluate(()=>{const end=performance.now()+240;while(performance.now()<end) {} });
      if (mode==='completed') Object.assign(state,{phase:'verified',stopped:false,displacement_m:.0487,peak_speed_m_s:.098});
      else await until(()=>count('/release')===1,'interruption did not issue release');
    }
    await until(()=>control.isDisabled(),'control remained enabled');
    const after=count('/hold');await sleep(220);assert.equal(count('/hold'),after,'sent another hold after stopping');
    if (mode==='release'||mode==='completed') {
      Object.assign(state,{phase:'verified',stopped:false,displacement_m:.0487,peak_speed_m_s:.098});
      await until(async()=>await page.locator('#distance').textContent()==='48.7','MCU displacement did not update');
      assert(!((await page.locator('#phase').textContent()).includes('已验证')));
      state.stopped=true;
      await until(async()=>await page.locator('#phase').textContent()==='微动与停稳已验证','verified and stopped did not complete');
      assert.equal(await page.locator('#speed').textContent(),'0.098');
    }
    assert.equal(count('/begin'),1);
    // HTTP can replay a POST after a dropped persistent socket. Replayed sequence numbers must never renew a lease.
    assert(accepted.every((w,i)=>w.body.seq===i && w.body.operation_id===writes[0].body.operation_id &&
      Object.keys(w.body).sort().join(',')==='operation_id,seq' && w.contentType.startsWith('application/json')),
      JSON.stringify(writes.map(w=>[w.path,w.body.seq])));
    assert(writes.every(w=>accepted.some(a=>a.path===w.path && a.body.seq===w.body.seq && a.body.operation_id===w.body.operation_id)));
    if (mode==='http-disconnect') assert.deepEqual(accepted.map(x=>x.path),['/begin','/hold','/hold','/release']);
    assert.match(writes[0].body.operation_id,/^[\da-f]{8}-[\da-f]{4}-4[\da-f]{3}-[89ab][\da-f]{3}-[\da-f]{12}$/i);
    Object.assign(state,{phase:'ready',ready:true,stopped:true,operation_id:null});
    await page.reload();await sleep(230);assert(await control.isDisabled(),'refresh rearmed same page');
    assert.equal(count('/begin'),1);assert.deepEqual(errors,[]);
    console.log(`PASS ${mode}`);
  } finally {await ctx.close();await new Promise(resolve=>server.close(resolve));}
}

(async()=>{
  const html=await fs.readFile(path.join(__dirname,'../../calibration/station/attended-trial.html'));
  const browser=await chromium.launch({executablePath:process.env.EDGE_TEST_CHROMIUM || undefined,headless:true,
    args:['--disable-background-networking','--disable-extensions','--no-proxy-server']});
  const cases=['release','completed','blur','offline','leave','cancel','lost-begin','lost-pulse','http-disconnect','state-loss','state-reset','stall','touchcancel','two-touches'];
  const selected=process.argv.slice(2);
  try { for (const mode of selected.length ? selected : cases) {assert(cases.includes(mode));await run(browser,html,mode);} }
  finally { await browser.close(); }
})().catch(e=>{console.error(e);process.exitCode=1;});
