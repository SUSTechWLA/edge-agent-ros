// Exercise the normal sessions route while the task ledger advances independently of the robot archive.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const path = require('node:path');
const puppeteer = require(process.env.PUPPETEER_MODULE || 'puppeteer-core');

async function main() {
  const root = path.resolve(__dirname, '../../console/web');
  const browser = await puppeteer.launch({headless: true, executablePath: process.env.CHROME_PATH});
  const id = 'station-readonly-example';
  const labels = {WAITING: '等待结果', SUCCEEDED: '已完成'};
  const bodies = {WAITING: '正在等待独立测量。', SUCCEEDED: '独立测量已返回，验收完成。'};
  let state = 'WAITING';
  const calls = {robot: 0, list: 0, reader: 0};
  const writes = [], errors = [];
  try {
    const page = await browser.newPage();
    await page.setRequestInterception(true);
    page.on('pageerror', e => errors.push(e.message));
    page.on('request', async req => {
      const url = new URL(req.url());
      const reply = value => req.respond({status: 200, contentType: 'application/json', body: JSON.stringify(value)});
      if (url.origin !== 'http://workbench.test') return req.abort();
      if (req.method() !== 'GET') { writes.push(req.method() + ' ' + url.pathname); return req.abort(); }
      const task = {id, state, text: '读取当前传感器', created: 1700000000, updated: state === 'WAITING' ? 1700000001 : 1700000002};
      if (url.pathname === '/api/state') {
        calls.robot++;
        return reply({robot_id: 'fixture', sources: {}, task_proxy_mode: 'observe', calibration_status_available: false, cursor: 17});
      }
      if (url.pathname === '/api/sessions') {
        calls.list++;
        return reply({counts: {all: 1, active: +(state === 'WAITING'), succeeded: +(state === 'SUCCEEDED'), issues: 0},
          tasks: [task], next_before: null});
      }
      if (url.pathname === '/api/session' && url.searchParams.get('task_id') === id) {
        calls.reader++;
        return reply({task, headline: bodies[state], messages: [{role: 'system', text: bodies[state]}], coverage: {}});
      }
      if (url.pathname.startsWith('/api/')) return reply({events: []});
      const file = url.pathname === '/' ? 'index.html' : url.pathname.slice(1);
      if (!/^[\w.-]+\.(html|js|css)$/.test(file)) return req.abort();
      const type = {html: 'text/html', js: 'application/javascript', css: 'text/css'}[file.split('.').at(-1)];
      const target = file === 'app.js' && process.argv[2] ? path.resolve(process.argv[2]) : path.join(root, file);
      return req.respond({status: 200, contentType: type + '; charset=utf-8', body: await fs.readFile(target)});
    });
    await page.goto('http://workbench.test/#sessions?task=' + id);
    const snapshot = () => page.evaluate(() => ({
      row: document.querySelector('#session-list [aria-current="true"] .badge')?.textContent,
      heading: document.querySelector('#session-state')?.textContent,
      body: document.querySelector('#session-messages .session-message-text p')?.textContent,
      summary: document.querySelector('#session-headline')?.textContent
    }));
    async function expectProjection(expected) {
      const wanted = {row: labels[expected], heading: labels[expected], body: bodies[expected], summary: bodies[expected]};
      const end = Date.now() + 8500;
      let actual;
      do {
        actual = await snapshot();
        if (Object.keys(wanted).every(k => wanted[k] === actual[k])) return actual;
        await new Promise(resolve => setTimeout(resolve, 100));
      } while (Date.now() < end);
      assert.deepEqual(actual, wanted, 'Selected row and reader must both reflect the current task ledger: ' + JSON.stringify({actual, wanted, calls}));
    }
    const before = await expectProjection('WAITING');
    // Wait for at least two identical robot reads before committing the task, so initial load cannot mask a stale view.
    const settledBy = Date.now() + 5000;
    while (calls.robot < 2 && Date.now() < settledBy) await new Promise(resolve => setTimeout(resolve, 100));
    assert.ok(calls.robot >= 2, 'No consecutive reads of the same robot cursor');
    const beforeCalls = {...calls};
    state = 'SUCCEEDED';
    const after = await expectProjection('SUCCEEDED');
    for (const key of Object.keys(calls)) assert.ok(calls[key] > beforeCalls[key], key + ' was not read again');
    assert.deepEqual(writes, []);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({cursor: 17, before, after, beforeCalls, calls, writes}));
  } finally {
    await browser.close();
  }
}
main().catch(e => { console.error(e.message); process.exitCode = 1; });
