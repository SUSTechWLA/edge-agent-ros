// Read archived tasks already present in the target simulation. No test task is dispatched.
import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';

export async function checkReview({url, width, evaluate, waitFor, send, screenshot}) {
  const q = selector => 'document.querySelector('+JSON.stringify(selector)+')';
  const click = selector => evaluate(q(selector)+'.click()');
  const value = (selector, property) => evaluate(q(selector)+'.'+property);
  const api = async path => {
    const response = await fetch(new URL(path,url));
    assert.ok(response.ok, 'Review reference API returned HTTP '+response.status);
    return response.json();
  };
  const overflow = async label => {
    const pixels = await evaluate('document.documentElement.scrollWidth');
    assert.ok(pixels <= width+1, label+' overflows at '+width+'px ('+pixels+'px)');
  };
  const report = {tasks:{}, history:{}, detail:{}, task_page_checked:false};
  // Faults affect only this browser tab. The simulator, owner DB and archive remain untouched.
  async function simulateRead(path, parameters, status, response, run) {
    await evaluate(`window.reviewTestFetch=window.fetch;
      window.fetch=(resource, opts)=>{
        const uri=new URL(resource instanceof Request ? resource.url : resource,location.href);
        if (uri.pathname===${JSON.stringify(path)} &&
          Object.entries(${JSON.stringify(parameters)}).every(([k,v])=>uri.searchParams.get(k)===v)) {
          return Promise.resolve(Response.json(${JSON.stringify(response)},{status:${status}}));
        }
        return window.reviewTestFetch(resource,opts);
      }`);
    try { await run(); } finally {
      await evaluate('window.fetch=window.reviewTestFetch; delete window.reviewTestFetch');
    }
  }
  const taskIds = "Array.from(document.querySelectorAll('#review-task-list a[href*=\"task=\"]'),"+
                  "a=>new URLSearchParams(a.hash.split('?')[1]).get('task'))";
  const records = selector => 'Array.from(document.querySelectorAll('+JSON.stringify(selector+' [data-seq]')+
                              '), row => Number(row.dataset.seq))';
  await click('#nav-review');
  await waitFor("location.hash==='#review' && !document.querySelector('#review').hidden");
  await waitFor(q('#review-now')+".textContent.match(/\\d/)");
  await waitFor(q('#nav-review')+'.getBoundingClientRect().top>=0','Review navigation visible at top of page');
  await overflow('review');
  const baseline = await api('/api/review/tasks?limit=1');
  for (const [group,count] of Object.entries(baseline.counts)) {
    const filter = '[data-review-state="'+group+'"]';
    await waitFor(q(filter+' strong')+'.textContent.trim()==='+JSON.stringify(String(count)));
  }

  for (const group of ['all','active','succeeded','issues']) {
    const candidates = await api('/api/review/tasks?state='+group+'&limit=100');
    await click('[data-review-state="'+group+'"]');
    const task = candidates.tasks[0];
    if (!task) {
      await waitFor(taskIds+'.length===0 && '+q('#review-task-list')+'.innerText.trim().length>0');
      report.tasks[group] = 'empty';
      continue;
    }
    await waitFor(taskIds+'[0]==='+JSON.stringify(task.id),'Rendered '+group+' task');
    const visibleIds = await evaluate(taskIds);
    if (group==='all') await screenshot('overview');
    assert.deepEqual(visibleIds,candidates.tasks.slice(0,visibleIds.length).map(t=>t.id),
                     'Rendered task IDs differ from the '+group+' ledger page');
    report.tasks[group] = visibleIds.length;
    if (group==='all' && candidates.tasks.length>visibleIds.length) {
      await click('#review-next');
      await waitFor(taskIds+'[0]==='+JSON.stringify(candidates.tasks[visibleIds.length].id),'Next task page');
      const next = await evaluate(taskIds);
      assert.ok(next.every(id=>!visibleIds.includes(id)),'Task pagination repeated IDs');
      await click('#review-prev');
      await waitFor(taskIds+'[0]==='+JSON.stringify(task.id),'Previous task page');
      report.task_page_checked = true;
    }
    if (!['succeeded','issues'].includes(group)) continue;
    await click('#review-task-list a[href*="task="]');
    await waitFor(q('#review-task-goal')+'.textContent==='+JSON.stringify(task.text),'Task goal from owner ledger');
    const terminalText = await value('#review-result','innerText');
    assert.ok((group==='succeeded' ? /完成|成功|SUCCEEDED/ : /阻塞|受阻|异常|失败|恢复|核对|复核|BLOCKED|RECOVERY_REQUIRED|FAILED/).test(terminalText),
              'Task result should communicate the owner state to a human');
    if (group==='issues' && task.detail) {
      const code = task.detail.match(/\[code:([^\]]+)\]/)?.[1];
      assert.ok((await value('#review-task-detail','textContent')).includes(code || task.detail),
                'Blocked task diagnostic code or source diagnosis is missing');
    }
    await overflow('task detail');
    const scope = new URL(await value('#review-export','href'));
    assert.equal(scope.pathname,'/api/export');
    assert.equal(scope.searchParams.get('task_id'),task.id,'Task export scope');
    const reference = await api('/api/events?task_id='+encodeURIComponent(task.id)+'&limit=1');
    const outcome = {evidence:reference.events.length>0};
    if (reference.events.length) {
      const timeline = records('#review-timeline');
      await waitFor(timeline+'.length>0');
      if (group==='issues') await screenshot('task-issue');
      const seqs = await evaluate(timeline);
      assert.ok(seqs.every((seq,i)=>Number.isSafeInteger(seq) && seq>0 && (i===0||seqs[i-1]<seq)),
                'Timeline must be ordered and contain unique archive sequences');
      await evaluate(q('#review-timeline [data-seq]')+".closest('details:not([open])')?.querySelector('summary').click()");
      await waitFor(q('#review-timeline [data-seq]')+'.getClientRects().length>0');
      await click('#review-timeline [data-seq]');
      await waitFor(q('#event-modal')+'.open');
      assert.equal(JSON.parse(await value('#event-json','textContent')).seq,seqs[0]);
      await click('#replay');
      await waitFor('!'+q('#history-bar')+'.hidden && '+q('#history-msg')+'.textContent.includes('+JSON.stringify('#'+seqs[0])+')');
      await click(group==='issues' ? '#replay-review' : '#resume');
      await waitFor(q('#history-bar')+'.hidden');
      if (group==='issues') {
        await waitFor(q('#review-task-goal')+'.textContent==='+JSON.stringify(task.text),
                      'Replay returned to the originating task');
        outcome.replay_return = true;
      }
      outcome.replayed = seqs[0];
    }
    const direct = new URL('#review?task='+encodeURIComponent(task.id),url).href;
    await send('Page.navigate',{url:direct});
    await waitFor('location.href==='+JSON.stringify(direct)+' && '+q('#review-task-goal')+'?.textContent==='+JSON.stringify(task.text),
                  'Opened task by deep link');
    await send('Page.reload',{ignoreCache:true});
    await waitFor("document.readyState==='complete' && performance.getEntriesByType('navigation')[0]?.type==='reload'");
    await waitFor(q('#review-task-goal')+'?.textContent==='+JSON.stringify(task.text),'Restored task by deep link');
    await evaluate(q('#review-scope')+".value='trace_id'; "+q('#review-scope')+".dispatchEvent(new Event('change'))");
    await waitFor("new URL("+q('#review-export')+".href).searchParams.get('trace_id')==="+JSON.stringify(task.trace_id),'Trace export scope');
    report.detail[group] = outcome;
    await click('#nav-review');
    await waitFor("location.hash==='#review' && !document.querySelector('#review-home').hidden");
  }

  for (const view of ['changes','issues']) {
    const expected = await api('/api/events?view='+view+'&limit=1');
    const tab = '#review-history-tabs [data-view="'+view+'"]';
    await click(tab);
    if (expected.events.length) {
      const seq = expected.events[0].seq;
      await waitFor(records('#review-history-rows')+'.includes('+seq+')','Visible '+view+' event');
      await click('#review-history-rows [data-seq="'+seq+'"]');
      await waitFor(q('#event-modal')+'.open');
      assert.equal(JSON.parse(await value('#event-json','textContent')).kind,expected.events[0].kind);
      await click('#close-modal');
      report.history[view] = seq;
    } else {
      await waitFor(records('#review-history-rows')+'.length===0 && '+q('#review-history-rows')+'.innerText.trim().length>0');
      report.history[view] = 'empty';
    }
  }

  if (baseline.tasks.length) {
    const task = baseline.tasks[0];
    await simulateRead('/api/events',{task_id:task.id},200,{events:[],has_more:false},async()=>{
      await send('Page.navigate',{url:new URL('#review?task='+encodeURIComponent(task.id),url).href});
      await waitFor(q('#review-task-goal')+'?.textContent==='+JSON.stringify(task.text),'Owner task still readable');
      await waitFor(q('#review-timeline')+'.innerText.match(/无|未|没有/) && '+records('#review-timeline')+'.length===0');
    });
    report.partial_archive = true;
  }
  await simulateRead('/api/review/tasks',{},503,{error:'review-test-ledger-read-unavailable'},async()=>{
    await click('#nav-events');
    await waitFor('!'+q('#events')+'.hidden');
    await click('#nav-review');
    await waitFor('!'+q('#review-list-notice')+'.hidden && '+q('#review-list-notice')+'.innerText.length>0');
    assert.notEqual(await value('[data-review-state="all"] strong','textContent'),'0',
                    'Unavailable ledger displayed as zero tasks');
  });
  await click('[data-review-state="all"]');
  await waitFor(q('[data-review-state="all"] strong')+'.textContent==='+JSON.stringify(String(baseline.counts.all))+
                ' && '+q('#review-list-notice')+'.hidden');
  report.ledger_read_recovered = true;

  const missing = 'review-nonexistent-'+randomUUID();
  await send('Page.navigate',{url:new URL('#review?task='+missing,url).href});
  await waitFor('!'+q('#review-detail')+'.hidden && '+q('#review-task-detail')+'.innerText.match(/未找到|不存在|无法|不可用|读取失败|404/)');
  await waitFor(records('#review-timeline')+'.length===0 && '+q('#review-timeline')+'.innerText.trim().length>0');
  report.missing_deep_link = true;
  await click('#nav-events');
  await waitFor(q('#event-rows')+'.innerText.trim().length>0 && !'+q('#events')+'.hidden');
  assert.ok(['','all'].includes(await value('#events-view','value')),'Raw event archive must default to all records');
  const sample = await api('/api/events?kind=telemetry.sample&limit=1');
  if (sample.events.length) {
    await evaluate(q('#kind-filter')+".value='telemetry.sample'");
    await click('#filter');
    await waitFor("document.querySelector('#event-rows .event-key') && "+
                  "Array.from(document.querySelectorAll('#event-rows .event-key')).every(k=>k.textContent==='telemetry.sample')");
    await click('#event-rows button');
    await waitFor(q('#event-modal')+'.open');
    assert.equal(JSON.parse(await value('#event-json','textContent')).kind,'telemetry.sample');
    await click('#close-modal');
    await click('#clear-filter');
    report.raw_periodic_sample = true;
  }
  return report;
}
