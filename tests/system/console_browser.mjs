// Exercise the shipped browser UI against an isolated live simulation.
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { once } from 'node:events';
import { setTimeout as delay } from 'node:timers/promises';
import { checkReview } from './review_browser.mjs';
import { checkSessions } from './session_browser.mjs';
import { checkMap3D } from './map3d_browser.mjs';
import { checkCampusTexture } from './campus_texture_browser.mjs';
import { checkRgbd } from './rgbd_browser.mjs';

const [url, output, screenshotOption] = process.argv.slice(2);
if (screenshotOption && !['--screenshots','--map3d','--rgbd','--sensor-views','--campus','--campus-texture'].includes(screenshotOption)) throw new Error('Unknown browser test option');
const campusTexture = screenshotOption === '--campus-texture';
const campusOnly = screenshotOption === '--campus';
const mapOnly = screenshotOption === '--map3d';
const rgbdOnly = screenshotOption === '--rgbd';
const sensorViews = screenshotOption === '--sensor-views';
const binary = [process.env.CHROME_PATH, '/usr/bin/google-chrome', '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  join(process.env.ProgramFiles || '', 'Google/Chrome/Application/chrome.exe'),
  join(process.env['ProgramFiles(x86)'] || '', 'Microsoft/Edge/Application/msedge.exe')]
  .find(file => file && existsSync(file));
if (!binary) throw new Error('Set CHROME_PATH to an installed Chrome or Edge browser.');
if (!mapOnly && !rgbdOnly && !campusOnly && !campusTexture) {
  const state = await (await fetch(new URL('/api/state', url))).json();
  if (state.environment !== 'simulation') throw new Error('A simulation endpoint is required.');
}
const profile = mkdtempSync(join(tmpdir(), 'edge-browser-test-'));
const browser = spawn(binary, ['--headless=new','--disable-gpu','--no-first-run','--disable-extensions',
  '--no-sandbox','--remote-debugging-address=127.0.0.1','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],
  {stdio:['ignore','ignore','pipe'],windowsHide:true});
let diagnostics = '';
browser.stderr.on('data', chunk => { diagnostics = (diagnostics + chunk).slice(-3000); });
const exited = once(browser,'exit');
const pending = new Map();
let id = 0, session, socket;
const errors = [], viewports = [], writeRequests = [];
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
async function waitFor(expression, label=expression) {
  const deadline = Date.now()+20000;
  while (Date.now()<deadline) {
    if (await evaluate(expression)) return;
    await delay(150);
  }
  throw new Error('UI condition failed: '+label);
}
try {
  socket = await connectBrowser();
  socket.addEventListener('message', event => {
    const packet = JSON.parse(event.data);
    if (pending.has(packet.id)) { pending.get(packet.id)(packet); pending.delete(packet.id); }
    if (packet.method === 'Runtime.exceptionThrown') errors.push(packet.params.exceptionDetails.text);
    if (packet.method === 'Network.requestWillBeSent' &&
        !['GET','HEAD','OPTIONS'].includes(packet.params.request.method)) {
      writeRequests.push(packet.params.request.method);
    }
  });
  const target = await send('Target.createTarget',{url:'about:blank'});
  session = (await send('Target.attachToTarget',{targetId:target.targetId,flatten:true})).sessionId;
  await send('Runtime.enable');
  await send('Network.enable');
  await send('Page.navigate',{url});
  if (!mapOnly && !rgbdOnly && !campusOnly && !campusTexture) {
    await waitFor("document.querySelector('#sessions') && !document.querySelector('#sessions').hidden",
                  'Readable sessions are the default landing page');
  }
  for (const width of [1440,390]) {
    await send('Emulation.setDeviceMetricsOverride',{width,height:860,deviceScaleFactor:1,mobile:false});
    if (campusTexture) {
      viewports.push(await checkCampusTexture({url,width,output,evaluate,waitFor,send}));
      continue;
    }
    if (campusOnly) {
      await waitFor("document.querySelectorAll('.choices button').length === 7","seven campus views");
      const imageWidths = {};
      for (const id of ['campus','teaching','track','woods','road','lawn','overview']) {
        await evaluate(`document.querySelector('button[data-id="${id}"]').click()`);
        await waitFor(`!document.querySelector('#shot').hidden && new URL(document.querySelector('#shot').src).pathname.endsWith('/${id}.png') && document.querySelector('#shot').naturalWidth >= 300`);
        imageWidths[id] = await evaluate("document.querySelector('#shot').naturalWidth");
      }
      const content = await evaluate('document.documentElement.scrollWidth');
      if (content > width+1) throw new Error('campus horizontal overflow at '+width);
      if (width === 1440) {
        const screenshot = await send('Page.captureScreenshot',{format:'png'});
        writeFileSync(join(dirname(output),'campus-overview.png'), Buffer.from(screenshot.data,'base64'));
      }
      viewports.push({width, imageWidths, document_width:content});
      continue;
    }
    if (mapOnly) {
      viewports.push(await checkMap3D({url,width,output,evaluate,waitFor,send}));
      continue;
    }
    if (rgbdOnly) {
      viewports.push(await checkRgbd({width,output,evaluate,waitFor,send}));
      continue;
    }
    await evaluate("document.querySelector('#nav-live').click()");
    if (sensorViews) {
      await waitFor("['map3d','camera','rgbd'].every(id => document.querySelector('#'+id+'-link')?.offsetWidth > 0)",
                    'independently registered map, camera and RGB-D views are visible');
      const links = await evaluate("Object.fromEntries(['map3d','camera','rgbd'].map(id=>[id,document.getElementById(id+'-link').href]))");
      for (const [name, href] of Object.entries(links)) {
        if (new URL(href).hostname !== new URL(url).hostname) throw new Error('view on unexpected host: '+name);
      }
      const content = await evaluate('document.documentElement.scrollWidth');
      if (content > width+1) throw new Error('horizontal overflow at '+width);
      viewports.push({width, links, document_width:content});
      continue;
    }
    await waitFor("document.querySelector('#metric-pos')?.textContent.match(/[0-9]/) && document.querySelector('#map')?.width > 0");
    await evaluate("document.querySelector('#open-task').click()");
    await waitFor("document.querySelector('#task-modal').open && document.querySelector('#cap-list').textContent.includes('navigate_place')");
    await evaluate("document.querySelector('#close-task').click(); document.querySelector('#nav-events').click()");
    await waitFor("location.hash==='#events' && !document.querySelector('#events').hidden");
    const content = await evaluate('document.documentElement.scrollWidth');
    if (content > width+1) throw new Error('horizontal overflow: '+content+' at '+width);
    const screenshot = async name => {
      if (!screenshotOption) return;
      const {data} = await send('Page.captureScreenshot',{format:'png'});
      writeFileSync(join(dirname(output),'review-'+name+'-'+width+'.png'),Buffer.from(data,'base64'));
    };
    const review = await checkReview({url, width, evaluate, waitFor, send, screenshot});
    const sessions = await checkSessions({url, width, evaluate, waitFor, send, screenshot});
    await evaluate("document.querySelector('#nav-live').click()");
    await waitFor("!document.querySelector('#monitor').hidden");
    viewports.push({width,document_width:content,live_pose:true,tool_dialog:true,event_navigation:true,review,sessions});
  }
  if (errors.length) throw new Error(errors.join(','));
  if (writeRequests.length) throw new Error('Read-only browsing made a write request: '+writeRequests);
  writeFileSync(output,JSON.stringify({passed:true,viewports,errors,writeRequests},null,2)+'\n');
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
