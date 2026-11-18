// Read-only browser acceptance for the optional map. Use the same headless CDP driver as the Console.
import { writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

export async function checkMap3D({url,width,output,evaluate,waitFor,send}) {
  const snapshot = await (await fetch(new URL('/api/map',url))).json();
  if (snapshot.cloud?.status !== 'online' || !snapshot.cloud.points.length) {
    throw new Error('3D UI requires an actual survey cloud');
  }
  await waitFor("document.querySelector('#viewport')?.width > 0 && Number(document.querySelector('#point-count')?.textContent.replace(/[^0-9]/g,'')) > 0");
  const layout = await evaluate(`(() => {
    const rect=document.querySelector('#viewport').getBoundingClientRect();
    return {document_width:document.documentElement.scrollWidth, canvas:[rect.x,rect.y,rect.width,rect.height]};
  })()`);
  if (layout.document_width > width+1 || layout.canvas[2]<160 || layout.canvas[3]<150) {
    throw new Error('Unusable 3D layout at width '+width+' '+JSON.stringify(layout));
  }
  const canvasSignature = `(() => {
    const map=document.querySelector('#viewport'), c=document.createElement('canvas');
    c.width=96;c.height=96;const ctx=c.getContext('2d');ctx.drawImage(map,0,0,96,96);
    const rgba=ctx.getImageData(0,0,96,96).data;let hash=2166136261;
    for(const value of rgba) hash=Math.imul(hash^value,16777619);
    return hash>>>0;
  })()`;
  const click = selector => evaluate(`document.querySelector(${JSON.stringify(selector)}).click()`);
  await click('#orbit-reset');
  const initial=await evaluate(canvasSignature);
  await click('#view-top');
  await waitFor(canvasSignature+' !== '+initial,'Top projection renders distinct pixels');
  await click('#orbit-reset');
  const layer=await evaluate("document.querySelector('#layer-cloud').checked ?? document.querySelector('#layer-cloud').getAttribute('aria-pressed')");
  await click('#layer-cloud');
  const hidden=await evaluate("document.querySelector('#layer-cloud').checked ?? document.querySelector('#layer-cloud').getAttribute('aria-pressed')");
  if (hidden===layer) throw new Error('Measured cloud layer toggle failed');
  await click('#layer-cloud');
  const [left, top, w, h] = layout.canvas;
  const x=left+w*.45,y=top+h*.49;
  const before=await evaluate(canvasSignature);
  for(const [type,xx,yy,buttons] of [['mousePressed',x,y,1],['mouseMoved',x+45,y+25,1],['mouseReleased',x+45,y+25,0]]) {
    await send('Input.dispatchMouseEvent',{type,x:xx,y:yy,button:'left',buttons,clickCount:1});
  }
  await waitFor(canvasSignature+' !== '+before,'Orbit drag changes rendered perspective');
  const orbit=await evaluate(canvasSignature);
  await send('Input.dispatchMouseEvent',{type:'mouseWheel',x,y,deltaX:0,deltaY:-150});
  await waitFor(canvasSignature+' !== '+orbit,'Zoom changes rendered perspective');
  await click('#orbit-reset');
  if(width===1440) {
    const {data} = await send('Page.captureScreenshot',{format:'png'});
    writeFileSync(join(dirname(output),'map3d-simulation.png'),Buffer.from(data,'base64'));
  }
  return {width,...layout,cloud_frame:snapshot.cloud.source_frame,point_count:snapshot.cloud.points.length,
          top_projection:true,cloud_toggle:true,drag_orbit:true,wheel_zoom:true};
}
