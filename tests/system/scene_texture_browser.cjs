/* Real browser acceptance for the first archived Jetson RGB-D map. Use an isolated replay site. */
'use strict';
const assert=require('node:assert/strict');
const {createHash}=require('node:crypto');
const fs=require('node:fs');
const path=require('node:path');
const {parseArgs}=require('node:util');
const {values:args}=parseArgs({options:Object.fromEntries(
  ['url','out','browser','puppeteer'].map(name=>[name,{type:'string'}]))});
const digest=data=>createHash('sha256').update(data).digest('hex');
const paint=page=>page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));
const ready=page=>page.waitForFunction(()=>!document.querySelector('#open-archive').disabled);

async function planBeforeReadingLayers(page, origin) {
  const requests=[];
  const observe=request=>{
    const path=new URL(request.url()).pathname;
    if(path.startsWith('/api/'))requests.push({method:request.method(),path});
  };
  page.on('request',observe);
  try{
    await page.goto(origin,{waitUntil:'domcontentloaded'});
    await ready(page);
    assert.equal(await page.$eval('#check-registration',e=>e.disabled),true);
    assert.equal(await page.$eval('#run-plan',e=>e.disabled),false);
    await page.select('#plan-case','clear');
    const [response]=await Promise.all([
      page.waitForResponse(r=>r.url()===origin+'/api/plan' && r.request().method()==='POST'),
      page.click('#run-plan')
    ]);
    assert.equal(response.status(),200);
    assert.deepEqual(JSON.parse(response.request().postData()),{case:'clear'});
    const {plan,service,binding,snapshot_ref,request_id,evaluation_seq,scene}=await response.json();
    assert.equal(scene,undefined);
    assert.equal(service,'planning.local_path');
    assert.equal(plan.motion_authorized,false);
    assert.equal(plan.map_anchor,null);
    assert.equal(plan.status,'planned');
    assert.equal(plan.path_body_m.length,2);
    assert(evaluation_seq>=1);
    assert.match(binding,/^[0-9a-f]{64}$/);
    assert.match(snapshot_ref,/^sha256:[0-9a-f]{64}$/);
    await page.waitForFunction(id=>document.querySelector('#plan-detail').title===id,{},request_id);
    await paint(page);
    const canvas=await page.$eval('#plan-map',e=>{
      const pixels=e.getContext('2d').getImageData(0,0,e.width,e.height).data;
      let routePixels=0;
      for(let i=0;i<pixels.length;i+=4){
        if(pixels[i]===183 && pixels[i+1]===255 && pixels[i+2]===145)routePixels++;
      }
      return {visible:!e.hidden,points:Number(e.dataset.routePoints),voxels:Number(e.dataset.voxelCount),routePixels};
    });
    assert(canvas.visible && canvas.routePixels>10);
    assert.equal(canvas.points,plan.path_body_m.length);
    assert.equal(canvas.voxels,0);
    assert.match(await page.$eval('#occupancy-caption',e=>e.textContent),/仅显示车体坐标与规划结果/);
    assert(requests.some(x=>x.path==='/api/state'));
    assert.deepEqual(requests.filter(x=>x.path!=='/api/state'),[{method:'POST',path:'/api/plan'}]);
    return {requests:[...requests],request_id,service,binding,snapshot_ref,evaluation_seq,
      motion_authorized:plan.motion_authorized,map_anchor:plan.map_anchor,canvas};
  }finally{
    page.off('request',observe);
  }
}

// Render one recorded triangle in isolation. Its measured interior contains colours
// outside the three vertices' colour range, so vertex interpolation cannot pass.
async function interiorProbe(page,scene) {
  return page.evaluate(async input=>{
    const mapping=input.texture_mapping;
    const pixel=i=>mapping.uv[i].map((uv,axis)=>Math.round(uv*[input.image_width,input.image_height][axis]-.5)).join(',');
    const triangle=input.triangles.find(face=>face.map(pixel).sort().join('|')==='336,0|336,8|344,0');
    if(!triangle)throw Error('Recorded probe triangle is missing');
    const thumb=document.getElementById('texture-image');
    const rgb=document.createElement('canvas');
    rgb.width=thumb.naturalWidth;rgb.height=thumb.naturalHeight;
    const source=rgb.getContext('2d');
    source.drawImage(thumb,0,0);
    const sample=[...source.getImageData(339,4,1,1).data].slice(0,3);
    const canvas=document.createElement('canvas');
    canvas.style.cssText='position:fixed;top:0;left:-9000px;width:640px;height:640px';
    document.body.append(canvas);
    const viewer=window.FieldScene.create(canvas,{requireTextureMapping:true});
    try{
      const vertices=triangle.map(i=>input.vertices[i]);
      viewer.setLayer('rgbGrid',false);
      viewer.setData({...input,grid_width:undefined,stride_px:undefined,vertices,triangles:[[0,1,2]],
        texture_mapping:{...mapping,uv:triangle.map(i=>mapping.uv[i])}},null);
      viewer.zoomBy(2);
      for(let i=0;i<120 && canvas.dataset.texture!=='source_png';i++)await new Promise(done=>requestAnimationFrame(done));
      await new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done)));
      const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
      let close=0;
      for(let i=0;i<data.length;i+=4){
        if(sample.every((channel,j)=>Math.abs(channel-data[i+j])<=8))close++;
      }
      return {pixel:[339,4],source_rgb:sample,vertex_rgb:vertices.map(v=>v.slice(3)),
        matching_render_pixels:close,texture:canvas.dataset.texture};
    }finally{viewer.dispose();canvas.remove();}
  },scene);
}

async function main() {
  const url=new URL(args.url);
  assert(url.protocol==='http:'&&['localhost','127.0.0.1'].includes(url.hostname)&&!url.username&&!url.password);
  const out=path.resolve(args.out);
  fs.mkdirSync(out,{recursive:true});
  const image=name=>path.join(out,name+'.png');
  const browser=await require(args.puppeteer||'puppeteer-core').launch({headless:true,executablePath:args.browser,
    args:['--no-first-run','--disable-background-networking'],defaultViewport:{width:1440,height:900,deviceScaleFactor:1}});
  const audit={passed:false},errors=[],changes=[];
  try{
    const page=await browser.newPage();
    page.on('pageerror',err=>errors.push(err.message));
    page.on('request',r=>{if(new URL(r.url()).pathname==='/api/texture')changes.push(r.postData());});
    audit.plan_before_layers=await planBeforeReadingLayers(page,url.origin);
    await page.click('#open-archive');
    await page.waitForFunction(()=>document.querySelector('#occupancy-map').dataset.voxelCount==='330' &&
      document.querySelector('#texture-map').dataset.appearance==='rgb');
    assert.equal(await page.$eval('#occupancy-map',e=>e.hidden),false);
    const deep=url.origin+'/?view=texture#texture-card';
    const capture=async selector=>Buffer.from((await page.$eval(selector,e=>e.toDataURL())).split(',')[1],'base64');
    const view=()=>page.$eval('#texture-map',e=>[e.dataset.yaw,e.dataset.pitch,e.dataset.zoom,e.dataset.panX,e.dataset.panY]);
    const layer=page.waitForResponse(r=>r.url().endsWith('/api/layers')&&JSON.parse(r.request().postData()).layer==='texture');
    await page.goto(deep,{waitUntil:'domcontentloaded'});
    const scene=(await (await layer).json()).scene;
    await page.waitForFunction(()=>document.querySelector('#texture-map').dataset.appearance==='rgb');
    await ready(page);await paint(page);
    assert.deepEqual(changes,[],'The deep link edited plugin state');
    audit.coverage=await page.$eval('#texture-coverage',e=>e.textContent);
    assert.match(audit.coverage,/39\.8%/);
    audit.probe=await interiorProbe(page,scene);
    assert.equal(audit.probe.texture,'source_png');
    assert.deepEqual(audit.probe.source_rgb,[165,171,152]);
    assert(audit.probe.vertex_rgb.every(rgb=>rgb[0]<audit.probe.source_rgb[0]-15));
    assert(audit.probe.matching_render_pixels>=3,'Renderer did not sample the original image inside the mesh');
    await page.click('#check-registration');
    await page.waitForFunction(()=>document.querySelector('#query-title').textContent==='拒绝合并');
    await page.screenshot({path:image('workbench')});
    const radar=digest(await capture('#occupancy-map'));
    await page.click('#surface-geometry');await paint(page);
    assert.equal(digest(await capture('#occupancy-map')),radar);
    await page.click('#surface-rgb');await paint(page);
    await page.click('#texture-expand');
    await page.waitForFunction(()=>document.fullscreenElement?.id==='texture-card');
    await ready(page);await paint(page);
    const start=await view();
    const rgb=digest(await capture('#texture-map'));
    const original=await page.evaluateHandle(()=>{
      const c=document.getElementById('texture-map');
      return c.getContext('2d').getImageData(0,0,c.width,c.height);
    });
    await page.screenshot({path:image('rgb-surface')});
    await page.click('#surface-geometry');await paint(page);
    const geometry=digest(await capture('#texture-map'));
    assert.notEqual(geometry,rgb);assert.deepEqual(await view(),start);
    await page.screenshot({path:image('depth-surface')});
    await page.click('#surface-rgb');await paint(page);
    const replot=await page.evaluate(first=>{
      const c=document.getElementById('texture-map');
      if(c.width!==first.width||c.height!==first.height)throw Error('Viewport changed');
      const data=c.getContext('2d').getImageData(0,0,c.width,c.height).data;
      let diff=0;
      for(let i=0;i<data.length;i++)if(Math.abs(first.data[i]-data[i])>12)diff++;
      return diff/data.length;
    },original);
    await original.dispose();
    assert(replot<0.001,'The RGB surface did not return to its original appearance');
    await page.click('#texture-expand');await ready(page);await paint(page);
    audit.appearance={rgb,geometry,radar,view:start,same_view:true,replot_changed_fraction:replot};
    const download=await page.$eval('#download-texture-map',e=>e.href);
    assert.match(new URL(download).pathname,/^\/api\/maps\/[0-9a-f]{64}\/[0-9a-f]{64}\.glb$/);
    const model=await page.evaluate(async href=>{const r=await fetch(href);return [r.status,r.headers.get('Content-Type'),new DataView(await r.arrayBuffer()).getUint32(0,true)];},download);
    assert.equal(model[0],200);assert.equal(model[2],0x46546c67);
    await page.click('#toggle-texture');await ready(page);
    await page.reload();await ready(page);
    assert.equal(await page.$eval('#texture-state',e=>e.textContent),'已停用');
    assert.equal(await page.$eval('#download-texture-map',e=>e.hasAttribute('href')),false);
    assert.equal(await page.$eval('#texture-map',e=>e.dataset.triangleCount),'0');
    assert.equal(await page.evaluate(async href=>(await fetch(href)).status,download),409);
    assert.deepEqual(changes,['{"enabled":false}']);
    await page.click('#toggle-texture');await ready(page);
    await page.waitForFunction(()=>document.querySelector('#texture-map').dataset.appearance==='rgb');
    audit.hot_reload={stopped_download:409,restored:true,deep_link_read_only:true};
    // Degrade the browser inputs independently of provider admission: a failed
    // PNG or invalid UV must leave geometry, its controls and the lidar usable.
    let fault='uv';
    await page.setRequestInterception(true);
    page.on('request',async r=>{
      const u=new URL(r.url());
      if(fault==='png'&&u.pathname.startsWith('/camera/'))return r.respond({status:404,body:''});
      if(fault==='uv'&&u.pathname==='/api/layers'&&JSON.parse(r.postData()).layer==='texture'){
        const response=await fetch(r.url(),{method:'POST',headers:{'Content-Type':'application/json'},body:r.postData()});
        const row=await response.json();row.scene.texture_mapping.image_ref='sha256:'+'0'.repeat(64);
        return r.respond({status:200,contentType:'application/json',body:JSON.stringify(row)});
      }
      return r.continue();
    });
    for(fault of ['uv','png']){
      await page.reload();await ready(page);
      await page.waitForFunction(()=>document.querySelector('#texture-map').dataset.texture==='unavailable');
      assert.equal(await page.$eval('#texture-map',e=>e.dataset.appearance),'geometry');
      assert.equal(await page.$eval('#surface-rgb',e=>e.disabled),true);
      assert.equal(await page.$eval('#texture-photo',e=>e.hidden),true);
      assert.equal(await page.$eval('#download-texture-map',e=>e.hasAttribute('href')),false);
      assert.equal(await page.$eval('#occupancy-map',e=>e.dataset.voxelCount),'330');
    }
    assert.deepEqual(errors,[]);
    audit.passed=true;
  }finally{
    await browser.close();
    fs.writeFileSync(path.join(out,'browser.json'),JSON.stringify(audit,null,2)+'\n');
  }
}
main().catch(err=>{console.error(err);process.exitCode=1;});
