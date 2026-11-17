// Exercise the shipped browser UI against an isolated live simulation.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';

const [url, output] = process.argv.slice(2);
const binary = [process.env.CHROME_PATH, '/usr/bin/google-chrome', '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  join(process.env.ProgramFiles || '', 'Google/Chrome/Application/chrome.exe'),
  join(process.env['ProgramFiles(x86)'] || '', 'Microsoft/Edge/Application/msedge.exe')]
  .find(file => file && existsSync(file));
if (!binary) throw new Error('Set CHROME_PATH to an installed Chrome or Edge browser.');
const state = await (await fetch(new URL('/api/state', url))).json();
if (state.environment !== 'simulation') throw new Error('A simulation endpoint is required.');
const profile = mkdtempSync(join(tmpdir(), 'edge-browser-test-'));
const browser = spawn(binary, ['--headless=new','--disable-gpu','--no-first-run','--disable-extensions',
  '--no-sandbox','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],
  {stdio:['ignore','ignore','pipe'],windowsHide:true});
let diagnostics = '';
browser.stderr.on('data', chunk => { diagnostics = (diagnostics + chunk).slice(-3000); });
const exited = once(browser,'exit');
const pending = new Map();
let id = 0, session, socket;
const errors = [], viewports = [];
async function connectBrowser() {
  const portFile = join(profile,'DevToolsActivePort');
  const deadline = Date.now()+20000;
  while (Date.now()<deadline && browser.exitCode === null && !browser.signalCode) {
    if (existsSync(portFile)) {
      const [port,path] = readFileSync(portFile,'utf8').trim().split(/\r?\n/);
      if (/^\d+$/.test(port) && path?.startsWith('/devtools/browser/')) {
        const connection = new WebSocket('ws://127.0.0.1:'+port+path);
        await once(connection,'open',{signal:AbortSignal.timeout(10000)});
        return connection;
      }
    }
    await delay(100);
  }
  throw new Error('Browser did not start: '+diagnostics);
}
function send(method, params={}, sessionId=session) {
  return new Promise((resolve,reject) => {
    const current = ++id;
    const timer = setTimeout(() => { pending.delete(current); reject(new Error('timeout: '+method+' '+diagnostics)); }, 10000);
    pending.set(current, packet => {
      clearTimeout(timer);
      if (packet.error) reject(new Error(packet.error.message)); else resolve(packet.result);
    });
    socket.send(JSON.stringify({id:current,method,params,sessionId}));
  });
}
async function evaluate(expression) {
  const r = await send('Runtime.evaluate',{expression,returnByValue:true});
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || expression);
  return r.result.value;
}
async function waitFor(expression) {
  const deadline = Date.now()+20000;
  while (Date.now()<deadline) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error('UI condition failed: '+expression);
}
try {
  socket = await connectBrowser();
  socket.addEventListener('message', event => {
    const packet = JSON.parse(event.data);
    if (pending.has(packet.id)) { pending.get(packet.id)(packet); pending.delete(packet.id); }
    if (packet.method === 'Runtime.exceptionThrown') errors.push(packet.params.exceptionDetails.text);
  });
  const target = await send('Target.createTarget',{url:'about:blank'});
  session = (await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
  await send('Runtime.enable');
  await send('Page.navigate',{url});
  for (const width of [1440,390]) {
    await send('Emulation.setDeviceMetricsOverride',{width,height:860,deviceScaleFactor:1,mobile:false});
    await waitFor("document.querySelector('#metric-pos')?.textContent.match(/[0-9]/) && document.querySelector('#map')?.width > 0");
    await evaluate("document.querySelector('#open-task').click()");
    await waitFor("document.querySelector('#task-modal').open && document.querySelector('#cap-list').textContent.includes('navigate_place')");
    await evaluate("document.querySelector('#close-task').click(); document.querySelector('#nav-events').click()");
    await waitFor("location.hash==='#events' && !document.querySelector('#events').hidden");
    const content = await evaluate('document.documentElement.scrollWidth');
    if (content > width+1) throw new Error('horizontal overflow: '+content+' at '+width);
    await evaluate("document.querySelector('#nav-live').click()");
    await waitFor("!document.querySelector('#monitor').hidden");
    viewports.push({width,document_width:content,live_pose:true,tool_dialog:true,event_navigation:true});
  }
  if (errors.length) throw new Error(errors.join(','));
  writeFileSync(output,JSON.stringify({passed:true,viewports,errors},null,2)+'\n');
} finally {
  if (socket?.readyState === WebSocket.OPEN) {
    try { await send('Browser.close',{},null); } catch { browser.kill(); }
    socket.close();
  } else if (browser.exitCode === null && !browser.signalCode) { browser.kill(); }
  if (browser.exitCode === null && !browser.signalCode) {
    const stopped = await Promise.race([exited.then(()=>true),delay(2000).then(()=>false)]);
    if (!stopped) { browser.kill(); await exited; }
  }
  // Chrome helpers can finish a profile write just after the main process exits.
  // The asynchronous remover retries ENOTEMPTY on Linux as the helpers release it.
  await rm(profile,{recursive:true,force:true,maxRetries:12,retryDelay:200});
}
