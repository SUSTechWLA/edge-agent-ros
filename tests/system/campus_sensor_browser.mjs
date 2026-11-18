import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {dirname,join} from 'node:path';

export async function checkCampusSensors({url,width,output,evaluate,waitFor,send}) {
  await send('Page.navigate',{url:new URL('#live',url).href});
  await waitFor("document.getElementById('nav-live')");
  await evaluate("document.getElementById('nav-live').click()");
  await waitFor("['camera','rgbd'].every(name=>document.getElementById(name+'-link')?.offsetWidth>0)");
  const links=await evaluate("Object.fromEntries(['camera','rgbd'].map(name=>[name,document.getElementById(name+'-link').href]))");
  await send('Page.navigate',{url:links.camera});
  await waitFor("document.querySelectorAll('#source option').length===3");
  const frames={};
  for(const robot of ['edge_rover','crazyflie','g1']){
    await evaluate(`document.getElementById('source').value=${JSON.stringify(robot)};document.getElementById('source').dispatchEvent(new Event('change'))`);
    await waitFor(`!document.getElementById('preview').hidden && document.getElementById('frameStamp').textContent.includes(${JSON.stringify(robot+'/')})`);
    frames[robot]=await evaluate("document.getElementById('frameStamp').textContent");
    assert.ok(await evaluate("document.getElementById('preview').naturalWidth")>0);
    if(width===1440){
      const shot=await send('Page.captureScreenshot',{format:'png'});
      writeFileSync(join(dirname(output),robot+'-camera.png'),Buffer.from(shot.data,'base64'));
    }
  }
  await send('Page.navigate',{url:links.rgbd});
  await waitFor("document.getElementById('status')?.dataset.state==='online' && document.getElementById('depth-image').naturalWidth>0");
  const data=await (await fetch(new URL('/api/rgbd',links.rgbd))).json();
  assert.equal(data.frame,'edge_rover/rgbd_optical');
  assert.ok(data.skew_ms<=data.max_skew_ms);
  let pixel, empty;
  // Read a bounded grid so the click can be reproduced at different rover poses.
  // Omitting pair here chooses the latest live pair for each candidate location.
  for(const y of [.85,.64,.42,.15])for(const x of [.5,.25,.75]){
    const u=Math.floor(x*data.width),v=Math.floor(y*data.height);
    const reply=await fetch(new URL('/api/sample?u='+u+'&v='+v,links.rgbd));
    const sample=await reply.json();
    if(reply.ok && sample.depth_m>0 && !pixel)pixel=sample;
    if(reply.status===422 && sample.error==='no_depth_return' && !empty)empty=sample;
  }
  assert.ok(pixel,'at least one measured surface in the rover camera');
  const tap=async (sample,id)=>{
    const rect=await evaluate(`(() => {
      const e=document.getElementById('${id}');e.scrollIntoView({block:'center'});const r=e.getBoundingClientRect();
      return {x:r.left+r.width*(${sample.u}+.5)/${data.width},y:r.top+r.height*(${sample.v}+.5)/${data.height}};
    })()`);
    for(const type of ['mousePressed','mouseReleased'])
      await send('Input.dispatchMouseEvent',{type,...rect,button:'left',buttons:type==='mousePressed'?1:0,clickCount:1});
    await waitFor(`document.getElementById('pixel-value')?.textContent.includes('(${sample.u}, ${sample.v}) · ')`);
    const reading=await evaluate("document.getElementById('pixel-value').textContent");
    assert.ok(sample.error?reading.includes('无有效回波'):reading.includes(' m · RGB'),reading);
    return reading;
  };
  const measured=await tap(pixel,'depth-image');
  if(width===1440){
    const shot=await send('Page.captureScreenshot',{format:'png'});
    writeFileSync(join(dirname(output),'rover-depth.png'),Buffer.from(shot.data,'base64'));
  }
  const noReturn=empty?await tap(empty,'color-image'):null;
  return {width,links,frames,pair:data,measured,sample:pixel,noReturn};
}
