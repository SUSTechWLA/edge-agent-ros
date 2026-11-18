import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {dirname,join} from 'node:path';

export async function checkCampusReview({url,width,output,evaluate,waitFor,send}) {
  const destinations={};
  let task;
  for(const view of ['sessions','review','events']){
    await send('Page.navigate',{url});
    const selector='#workbench a[data-view="'+view+'"]';
    await waitFor("document.querySelector("+JSON.stringify(selector)+")?.getClientRects().length>0");
    assert.ok(await evaluate('document.documentElement.scrollWidth')<=width+1);
    const href=await evaluate("document.querySelector("+JSON.stringify(selector)+").href");
    assert.equal(new URL(href).hostname,new URL(url).hostname);
    await evaluate("document.querySelector("+JSON.stringify(selector)+").click()");
    await waitFor("location.href.startsWith("+JSON.stringify(href)+") && document.getElementById("+JSON.stringify(view)+")?.hidden===false");
    destinations[view]=href;
    if(view==='sessions'){
      await waitFor("document.getElementById('session-messages')?.querySelector('details') && document.getElementById('session-goal').textContent.trim().length>0");
      const archiveUrl=await evaluate("document.getElementById('session-markdown').href");
      const archive=await fetch(archiveUrl);
      assert.equal(archive.status,200);
      const goal=await evaluate("document.getElementById('session-goal').textContent");
      assert.ok((await archive.text()).includes(goal));
      task={id:new URL(archiveUrl).searchParams.get('task_id'),goal};
      const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(join(dirname(output),'workbench-session-'+width+'.png'),Buffer.from(shot.data,'base64'));
    }
  }
  return {width,destinations,task};
}
