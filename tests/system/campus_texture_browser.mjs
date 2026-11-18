import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {writeFileSync} from 'node:fs';
import {dirname, join} from 'node:path';

// Sample the actual rendered canvas; the inspection layers are disabled for the
// comparison so unrelated ROS updates cannot account for the measured change.
function readCanvas(canvasId) {
  const canvas=document.getElementById(canvasId);
  const reduced=document.createElement('canvas');
  reduced.width=120; reduced.height=84;
  const ctx=reduced.getContext('2d');
  ctx.drawImage(canvas,0,0,reduced.width,reduced.height);
  const pixels=ctx.getImageData(0,0,reduced.width,reduced.height).data;
  let hash=2166136261, opaque=0;
  const colors=new Set();
  for(let p=0;p<pixels.length;p+=4) {
    if(pixels[p+3]>220) {
      opaque++;
      colors.add((pixels[p]>>3)*1024+(pixels[p+1]>>3)*32+(pixels[p+2]>>3));
    }
    for(let k=0;k<4;k++) hash=Math.imul(hash^pixels[p+k],16777619);
  }
  return {signature:hash>>>0, opaque, colors:colors.size};
}

async function verifyFiles(url,texture) {
  const evidence={};
  for(const [id,path] of [['color',texture.url],['height',texture.height_url]]) {
    assert.ok(path && path.startsWith('/texture/'+texture.key+'/'), 'missing versioned '+id+' image');
    const res=await fetch(new URL(path,url));
    assert.ok(res.ok, id+' PNG status '+res.status);
    const bytes=Buffer.from(await res.arrayBuffer());
    assert.equal(bytes.subarray(1,4).toString(),'PNG');
    assert.deepEqual([bytes.readUInt32BE(16),bytes.readUInt32BE(20)],[texture.width,texture.height]);
    evidence[id]={sha256:createHash('sha256').update(bytes).digest('hex'),bytes:bytes.length};
  }
  assert.equal(evidence.color.sha256,texture.sha256);
  assert.equal(evidence.height.sha256,texture.height_sha256);
  assert.ok(texture.mesh_url?.startsWith('/texture/'+texture.key+'/'),'registered native campus geometry');
  const response=await fetch(new URL(texture.mesh_url,url));
  assert.equal(response.headers.get('content-type'),'model/gltf-binary');
  assert.match(response.headers.get('cache-control'),/immutable/);
  const bytes=Buffer.from(await response.arrayBuffer());
  assert.equal(bytes.toString('ascii',0,4),'glTF');
  assert.equal(bytes.readUInt32LE(8),bytes.length);
  const document=JSON.parse(bytes.toString('utf8',20,20+bytes.readUInt32LE(12)));
  evidence.geometry={sha256:createHash('sha256').update(bytes).digest('hex'),bytes:bytes.length,
    meshes:document.meshes.length,materials:document.materials.length};
  assert.equal(evidence.geometry.sha256,texture.mesh_sha256);
  assert.ok(evidence.geometry.meshes>=3 && evidence.geometry.materials>=8);
  return evidence;
}

export async function checkCampusTexture({url,width,output,evaluate,waitFor,send}) {
  await waitFor("document.querySelector('#texture-info')?.textContent.includes('已显示') && !document.querySelector('#robot-fit').disabled",
                'texture decoded, georegistered and campus robot online');
  const {texture,environment,scene,pose,grid}=await (await fetch(new URL('/api/map',url))).json();
  assert.equal(environment,'simulation');
  assert.equal(texture?.status,'ready');
  assert.equal(texture.scene_id,'campus_large');
  assert.equal(scene.id,texture.scene_id);
  assert.equal(grid.frame,texture.frame);
  assert.ok(texture.height_scale_m>0 && pose.xyz?.length===3);
  const files=await verifyFiles(url,texture);
  const outcomes={};
  const click=id=>evaluate('document.getElementById('+JSON.stringify(id)+').click()');
  async function paint() {
    await evaluate('window.__campusPaint=false;requestAnimationFrame(()=>requestAnimationFrame(()=>{window.__campusPaint=true}));true');
    await waitFor('window.__campusPaint','campus map repainted');
  }
  async function setLayer(name,wanted) {
    await evaluate(`{const n=document.getElementById("layer-${name}");if(n.checked!==${wanted})n.click();}`);
  }
  const initialLayers=await evaluate("Object.fromEntries(['grid','scene','pose','cloud'].map(n=>[n,document.getElementById('layer-'+n).checked]))");
  for(const [mode,canvas] of [['planar','planar-viewport'],['spatial','viewport']]) {
    await click('mode-'+mode);
    await waitFor(`document.getElementById("mode-${mode}").getAttribute('aria-pressed')==='true'`);
    await click('campus-fit');
    if(mode==='spatial') await waitFor("document.querySelector('#viewport').dataset.surface==='mesh'",'native 3D scene has painted');
    await paint();
    const layout=await evaluate(`(()=>{const plot=document.getElementById("${canvas}").getBoundingClientRect();return {page:document.documentElement.scrollWidth,plot_width:plot.width,plot_height:plot.height,height:document.documentElement.scrollHeight}})()`);
    assert.ok(layout.page<=width+1 && layout.plot_width>=200 && layout.plot_height>=220,
              'campus map layout at '+width+': '+JSON.stringify(layout));
    const screenshot='campus-texture-'+(mode==='planar'?'2d':'3d')+'-'+width+'.png';
    const {data:image}=await send('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,
      clip:{x:0,y:0,width:layout.page,height:layout.height,scale:1}});
    writeFileSync(join(dirname(output),screenshot),Buffer.from(image,'base64'));
    for(const name of Object.keys(initialLayers)) await setLayer(name,false);
    await paint();
    const pixels=()=>evaluate('('+readCanvas.toString()+')('+JSON.stringify(canvas)+')');
    const atlas=await pixels();
    assert.ok(atlas.colors>=18 && atlas.opaque>600,'textured pixels and distinct materials for '+mode+': '+JSON.stringify(atlas));
    await setLayer('texture',false);
    await waitFor("document.getElementById('texture-info').textContent.includes('已隐藏')");
    await paint();
    const hidden=await pixels();
    assert.ok(atlas.opaque-hidden.opaque>500 && hidden.signature!==atlas.signature,'independent texture toggle: '+mode);
    await setLayer('texture',true);
    await click('campus-fit');
    await paint();
    assert.equal((await pixels()).signature,atlas.signature,'campus texture recovered: '+mode);
    await evaluate("{const select=document.getElementById('place-fit'); select.value='track'; select.dispatchEvent(new Event('change'));}");
    await paint();
    const track=await pixels();
    assert.notEqual(track.signature,atlas.signature,'track camera moves inside campus in '+mode);
    let trackPick;
    if(mode==='planar') {
      await evaluate('document.getElementById("planar-viewport").focus({preventScroll:true})');
      await send('Input.dispatchKeyEvent',{type:'rawKeyDown',key:'Enter',code:'Enter'});
      await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Enter',code:'Enter'});
      await waitFor("document.getElementById('pick-result').textContent.includes('田径场')","georeferenced ground under center of track");
      trackPick=await evaluate("document.getElementById('pick-result').textContent");
      assert.match(trackPick,/仿真外观/);
      assert.doesNotMatch(trackPick,/实测点/);
      await send('Input.dispatchKeyEvent',{type:'rawKeyDown',key:'Escape',code:'Escape'});
      await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape'});
    }
    await click('zoom-in');
    await paint();
    const zoom=await pixels();
    assert.notEqual(zoom.signature,track.signature,'zoom changes the athletics field markings');
    await click('robot-fit');
    await paint();
    const robot=await pixels();
    assert.notEqual(robot.signature,track.signature,'robot and track viewpoints differ');
    await click('campus-fit');
    await paint();
    assert.equal((await pixels()).signature,atlas.signature,'whole-campus fit restores world extent');
    const landmarks={};
    if(mode==='spatial') {
      for(const id of ['campus','teaching','track']) {
        const option=texture.areas.find(a=>a.id===id);
        assert.equal(option.camera.eye.length,3);
        await evaluate(`{const select=document.getElementById('place-fit');select.value=${JSON.stringify(id)};select.dispatchEvent(new Event('change'));}`);
        await paint();
        assert.equal(await evaluate("document.querySelector('#viewport').dataset.surface"),'mesh');
        const px=await pixels();
        assert.ok(px.colors>=25 && px.opaque>850,'discernible street geometry: '+id+' '+JSON.stringify(px));
        const filename='campus-geometry-'+id+'-'+width+'.png';
        const {data}=await send('Page.captureScreenshot',{format:'png'});
        writeFileSync(join(dirname(output),filename),Buffer.from(data,'base64'));
        landmarks[id]={...px,camera:option.camera,screenshot:filename};
      }
      assert.equal(new Set(Object.values(landmarks).map(p=>p.signature)).size,3);
    }
    outcomes[mode]={layout,screenshot,atlas,hidden,track,robot,zoom,landmarks,track_pick:trackPick};
    for(const [name,value] of Object.entries(initialLayers)) await setLayer(name,value);
    await paint();
  }
  return {width,scene:scene.id,frame:texture.frame,revision:texture.revision,
          dimensions:[texture.width,texture.height],files,areas:texture.areas.map(a=>a.id),modes:outcomes};
}
