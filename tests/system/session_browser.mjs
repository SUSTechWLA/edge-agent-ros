import assert from 'node:assert/strict';
import { randomUUID } from 'node:crypto';

export async function checkSessions({url, width, evaluate, waitFor, send, screenshot}) {
  const element = selector => 'document.querySelector('+JSON.stringify(selector)+')';
  const click = async selector => {
    await waitFor(element(selector)+' && !'+element(selector)+'.disabled && '+element(selector)+'.getClientRects().length>0');
    await evaluate(element(selector)+'.click()');
  };
  const api = async path => {
    const response = await fetch(new URL(path, url));
    assert.ok(response.ok, 'session reference HTTP '+response.status);
    return response.json();
  };
  const report = {states:[], widths:width};
  await click('#nav-sessions');
  await waitFor('!'+element('#sessions')+'.hidden');
  const page = await api('/api/review/tasks?limit=100');
  for (const group of ['succeeded', 'issues']) {
    const candidates = await api('/api/review/tasks?state='+group+'&limit=1');
    if (!candidates.tasks.length) continue;
    const task = candidates.tasks[0];
    await send('Page.navigate', {url:new URL('#sessions?task='+encodeURIComponent(task.id),url).href});
    await waitFor(element('#session-reader')+".getAttribute('aria-busy')!=='true' && "+
                  element('#session-goal')+'.textContent==='+JSON.stringify(task.text));
    const expected = await api('/api/session?task_id='+encodeURIComponent(task.id));
    const actual = await evaluate(element('#session-reader')+'.innerText');
    assert.ok(actual.includes(expected.headline), 'Reader must show the evidenced headline');
    assert.ok(expected.service?.plugin_id && expected.service?.plugin_version, 'Session provider identity is missing');
    assert.equal(await evaluate(element('#session-provider')+'.textContent'),
      'Session 插件 '+expected.service.plugin_id+' · v'+expected.service.plugin_version);
    assert.equal(await evaluate(element('#session-provider')+'.hidden'),false);
    assert.ok((await evaluate(element('#session-identities')+'.textContent')).includes(expected.service.binding),
      'Expandable provenance must identify the actual plugin binding');
    assert.ok(expected.messages.length>0 && expected.messages.length<=14, 'Initial narrative must be bounded');
    assert.ok(actual.length<14000, 'Session initially exposed too much detail');
    for (const message of expected.messages) {
      if (message.text) assert.ok(actual.includes(message.text), 'Narrative missing '+message.title);
    }
    const markdown = await evaluate(element('#session-markdown')+'.href');
    const download = await fetch(markdown);
    assert.ok(download.ok);
    const downloaded = await download.text();
    // Independent requests may observe a newer global cutoff; factual content
    // for a finished task must remain identical while archive metadata advances.
    assert.ok(downloaded.includes(expected.headline));
    for (const message of expected.messages) {
      if (message.text) assert.ok(downloaded.includes(message.text), 'LLM download omitted a session fact');
    }
    assert.ok(await evaluate('document.documentElement.scrollWidth')<=width+1, 'Session horizontal overflow');
    if (group==='issues') await screenshot('session');
    const references = expected.messages.flatMap(row=>row.refs||[]);
    const first = await evaluate(element('#session-messages [data-seq]')+'?.dataset.seq');
    if (first) {
      assert.ok(references.some(r=>r.seq===Number(first)), 'Unknown citation in reader');
      await evaluate("document.querySelector('#session-messages [data-seq]').closest('details').open=true");
      await click('#session-messages [data-seq]');
      await waitFor(element('#event-modal')+'.open');
      assert.equal(JSON.parse(await evaluate(element('#event-json')+'.textContent')).seq,Number(first));
      await click('#close-modal');
      report.evidence_dialog=true;
    }
    report.states.push(task.state);
    report.markdown_download=true;
  }
  if (page.tasks.length) {
    const first = page.tasks[0];
    await send('Page.navigate',{url:new URL('#sessions?task='+encodeURIComponent(first.id),url).href});
    await waitFor(element('#session-goal')+'.textContent==='+JSON.stringify(first.text));
    await evaluate(`window.sessionOriginalFetch=window.fetch;
      window.fetch=(resource, options)=>{
        const path=new URL(resource instanceof Request?resource.url:resource,location.href).pathname;
        if(path==='/api/session')return Promise.resolve(Response.json({error:'session-test-offline'},{status:503}));
        return window.sessionOriginalFetch(resource,options);
      };`);
    try {
      await click('#session-refresh');
      await waitFor(element('#session-reader')+".getAttribute('aria-busy')!=='true' && !"+
                    element('#session-notice')+'.hidden && '+element('#session-notice')+'.textContent.length>0');
      assert.ok(!(await evaluate(element('#session-notice')+'.textContent')).includes('会话插件不可用'),
        'An unclassified server error must not be diagnosed as a plugin failure');
    } finally {
      await evaluate('window.fetch=window.sessionOriginalFetch; delete window.sessionOriginalFetch');
    }
    await click('#session-refresh');
    await waitFor(element('#session-reader')+".getAttribute('aria-busy')!=='true' && "+
                  element('#session-goal')+'.textContent==='+JSON.stringify(first.text)+' && '+element('#session-notice')+'.hidden');
    report.read_recovered=true;
    report.plugin_unavailable=[];
    for(const code of ['session.service_missing','session.draining','session.transport']){
      await evaluate(`window.sessionOriginalFetch=window.fetch;
        window.fetch=(resource, options)=>{
          const path=new URL(resource instanceof Request?resource.url:resource,location.href).pathname;
          if(path==='/api/session')return Promise.resolve(Response.json(
            {error:'测试中的会话服务暂不可用',code:${JSON.stringify(code)}},{status:503}));
          return window.sessionOriginalFetch(resource,options);
        };`);
      try{
        await click('#session-refresh');
        await waitFor(element('#session-reader')+".getAttribute('aria-busy')!=='true' && "+
          element('#session-notice')+".textContent.includes('会话插件不可用，任务和原始事件仍可查看')");
        assert.equal(await evaluate(element('#session-markdown')+'.hidden'),true);
        assert.equal(await evaluate(element('#session-provider')+'.hidden'),true);
        assert.equal(await evaluate(element('#session-fallback')+'.hidden'),false);
        assert.equal(new URL(await evaluate(element('#session-fallback-review')+'.href')).hash,
          '#review?'+new URLSearchParams({task:first.id}));
        assert.equal(await evaluate(element('#session-fallback-events')+".getAttribute('href')"),'#events');
        report.plugin_unavailable.push(code);
      }finally{
        await evaluate('window.fetch=window.sessionOriginalFetch; delete window.sessionOriginalFetch');
      }
    }
    await click('#session-fallback-review');
    await waitFor(element('#review-task-goal')+'.textContent==='+JSON.stringify(first.text));
    await click('#nav-events');
    await waitFor('!'+element('#events')+'.hidden && '+element('#event-rows')+'.innerText.length>0');
    await send('Page.navigate',{url:new URL('#sessions?task='+encodeURIComponent(first.id),url).href});
    await waitFor(element('#session-reader')+".getAttribute('aria-busy')!=='true' && "+
      element('#session-goal')+'.textContent==='+JSON.stringify(first.text)+' && '+element('#session-fallback')+'.hidden');
    report.plugin_read_recovered=true;
  }
  const missing='session-missing-'+randomUUID();
  await send('Page.navigate',{url:new URL('#sessions?task='+missing,url).href});
  await waitFor(element('#session-reader')+".getAttribute('aria-busy')!=='true' && "+
                element('#session-reader')+'.innerText.match(/未|不存在|无法|没有|404/)');
  report.missing_task=true;
  return report;
}
