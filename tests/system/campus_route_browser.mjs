import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import {dirname,join} from 'node:path';

// Counts the cyan stroke actually drawn by each renderer, including clipping.
// A checked box or a world-coordinate list alone cannot prove the map painted it.
function painted(id) {
  const c=document.getElementById(id),w=c.width,h=c.height;
  const rgba=c.getContext('2d').getImageData(0,0,w,h).data;
  let pixels=0,hash=2166136261,xmin=w,ymin=h,xmax=0,ymax=0;
  for(let i=0;i<rgba.length;i+=4){
    if(Math.abs(rgba[i]-34)>4 || Math.abs(rgba[i+1]-223)>4 || Math.abs(rgba[i+2]-233)>4)continue;
    const idx=i/4,x=idx%w,y=Math.floor(idx/w);
    pixels++;hash=Math.imul(hash^idx,16777619);
    xmin=Math.min(xmin,x);xmax=Math.max(xmax,x);ymin=Math.min(ymin,y);ymax=Math.max(ymax,y);
  }
  return {pixels,hash:hash>>>0,bounds:[xmin,ymin,xmax,ymax],size:[w,h]};
}

export async function checkCampusRoute({url,width,output,evaluate,waitFor,send}){
  const snapshot=await(await fetch(new URL('/api/map',url))).json();
  const r=snapshot.texture.route;
  assert.equal(r.frame,snapshot.frame);assert.equal(r.scene_revision,snapshot.scene.revision);
  const length=r.points.slice(1).reduce((n,[x,y],i)=>n+Math.hypot(x-r.points[i][0],y-r.points[i][1]),0);
  assert.ok(Math.abs(r.length_m-length)<.002);
  await waitFor("!document.getElementById('route-row').hidden && document.getElementById('viewport').dataset.surface==='mesh'");
  const outcomes={width,length_m:r.length_m,legs:r.points.length-1};
  const click=id=>evaluate("document.getElementById("+JSON.stringify(id)+").click()");
  for(const [mode,id] of [['spatial','viewport'],['planar','planar-viewport']]){
    await click('mode-'+mode);await click('route-fit');
    const pixels='('+painted.toString()+')('+JSON.stringify(id)+')';
    await waitFor(pixels+'.pixels>60','visible '+mode+' walk');
    const on=await evaluate(pixels);
    if(mode==='spatial'){
      const shot=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:false});
      writeFileSync(join(dirname(output),'campus-walk-3d-'+width+'.png'),Buffer.from(shot.data,'base64'));
    }
    await click('layer-route');
    await waitFor(pixels+'.pixels<'+Math.max(12,on.pixels*.08),'walk disappears');
    const off=await evaluate(pixels);
    await click('layer-route');
    await waitFor(pixels+'.pixels>60','walk reappears');
    // Use each real camera's keyboard interaction (rotation in 3D, zoom in 2D).
    await evaluate("document.getElementById("+JSON.stringify(id)+").dispatchEvent(new KeyboardEvent('keydown',{key:"+
      JSON.stringify(mode==='spatial'?'ArrowRight':'+')+",bubbles:true}))");
    await waitFor(pixels+'.hash!=='+on.hash+' && '+pixels+'.pixels>45','route follows camera');
    outcomes[mode]={on,off,changed:await evaluate(pixels)};
  }
  await click('mode-spatial');
  return outcomes;
}
