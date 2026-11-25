/* Verify an actual replay site in Chromium. This client only reads the frozen map and PLY. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {createHash}=require('node:crypto');
const {parseArgs}=require('node:util');
const {values:args}=parseArgs({options:Object.fromEntries(
  ['url','out','browser','puppeteer'].map(k=>[k,{type:'string'}]))});
const paint=page=>page.evaluate(()=>new Promise(done=>requestAnimationFrame(()=>requestAnimationFrame(done))));

async function main(){
  const host=new URL(args.url);
  assert.equal(host.protocol,'http:');assert(['127.0.0.1','localhost'].includes(host.hostname));
  const out=path.resolve(args.out);
  fs.mkdirSync(out,{recursive:true});
  const browser=await require(args.puppeteer||'puppeteer-core').launch({
    executablePath:args.browser,headless:true,args:['--no-first-run','--disable-background-networking'],
    defaultViewport:{width:1440,height:900,deviceScaleFactor:1}});
  const audit={passed:false}, errors=[];
  try{
    const page=await browser.newPage();
    page.on('pageerror',e=>errors.push(e.message));
    page.on('request',r=>{if(r.method()!=='GET'||new URL(r.url()).origin!==host.origin)errors.push(r.method()+' '+r.url());});
    const read=page.waitForResponse(r=>r.url()===host.origin+'/api/fusion/map');
    await page.goto(host.origin+'/fusion');
    const data=await (await read).json(), scene=data.scene;
    const total=scene.points.length, colored=scene.colors.filter(c=>c!==null).length;
    assert(colored>0 && colored<total);
    await page.waitForFunction(()=>!document.querySelector('#tools').hidden);
    await paint(page);
    const qty=()=>page.$eval('#shown',e=>Number(e.dataset.count));
    assert.equal(await qty(),total);
    assert.equal(await page.$eval('#percent',e=>e.textContent),(colored*100/total).toFixed(1)+'%');
    if(scene.windows?.length){
      const stationRows=await page.$$('#stations div');
      assert.equal(stationRows.length,scene.windows.length);
      assert.equal(await page.$eval('#clock',e=>e.textContent),[...new Set(scene.windows.map(w=>w.clock_id))].join(' / '));
    }
    const rgbSeen=await page.$eval('#cloud',(e,colors)=>{
      const ctx=e.getContext('2d');
      const pixels=ctx.getImageData(0,0,e.width,e.height).data;
      const keys=new Set(colors.filter(c=>c && Math.max(...c)-Math.min(...c)>55).map(c=>c.join(',')));
      const matched=new Set();let gray=0, count=0, click=null;
      for(let i=0;i<pixels.length;i+=4){
        if(!pixels[i+3])continue;
        if(pixels[i]===106 && pixels[i+1]===129 && pixels[i+2]===135)gray++;
        const colour=[pixels[i],pixels[i+1],pixels[i+2]].join(',');
        if(keys.has(colour)){
          matched.add(colour);count++;
          if(!click){
            const box=e.getBoundingClientRect();
            click={x:box.left+((i/4)%e.width+.5)*box.width/e.width,
              y:box.top+(Math.floor(i/(4*e.width))+.5)*box.height/e.height};
          }
        }
      }
      return {source_colors:matched.size,projected_pixels:count,unknown_pixels:gray,click};
    },scene.colors);
    assert(rgbSeen.source_colors>=2 && rgbSeen.projected_pixels>10 && rgbSeen.unknown_pixels>0);
    await page.screenshot({path:path.join(out,'fusion-map.png')});
    await page.mouse.click(rgbSeen.click.x,rgbSeen.click.y);await paint(page);
    const picked=await page.$eval('#pick',e=>e.textContent);
    assert.match(picked,/归档点 · .*RGB \(\d+, \d+, \d+\).*X .*Y .*Z /);
    delete rgbSeen.click;
    await page.click('#unknown');await paint(page);
    assert.equal(await qty(),colored);
    const inputs=await page.$$('#sources input');
    assert(inputs.length>=2);
    const first=await inputs[0].evaluate(e=>e.value);
    const remaining=scene.contributors.filter((source,i)=>source[0]!==first && scene.colors[i]!==null).length;
    await inputs[0].click();await paint(page);assert.equal(await qty(),remaining);
    await inputs[0].click();await page.click('#unknown');await paint(page);
    const canvas=await page.$('#cloud');
    const content=async()=>createHash('sha256').update(await canvas.evaluate(e=>e.toDataURL())).digest('hex');
    const orbit=await content(), box=await canvas.boundingBox();
    await page.mouse.move(box.x+box.width*.47,box.y+box.height*.55);
    await page.mouse.down();await page.mouse.move(box.x+box.width*.59,box.y+box.height*.51,{steps:6});await page.mouse.up();
    await paint(page);assert.notEqual(await content(),orbit);
    await page.click('#top');await paint(page);
    assert.notEqual(await content(),orbit);
    await page.screenshot({path:path.join(out,'fusion-top.png')});
    await page.click('#reset');await paint(page);
    assert.equal(await content(),orbit);
    const download=await page.$eval('#download',e=>e.href);
    assert.equal(new URL(download).pathname,'/api/fusion/'+data.scene_ref.slice(7)+'/'+data.layer_ref.slice(7)+'.ply');
    const reply=await fetch(download), file=Buffer.from(await reply.arrayBuffer());
    assert.equal(reply.status,200);assert.equal(file.subarray(0,3).toString(),'ply');
    assert.match(file.subarray(0,1000).toString(),new RegExp('element vertex '+total+'(?:\\r?\\n)'));
    await page.setViewport({width:390,height:800,deviceScaleFactor:1});
    await paint(page);
    assert(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth));
    assert.deepEqual(errors,[]);
    Object.assign(audit,{passed:true,frame:scene.frame_id,source_count:inputs.length,point_count:total,
      colored,uncolored:total-colored,acquisition:scene.windows?.map(w=>({station:w.id,clock:w.clock_id,range:w.acquisition}))||scene.acquisition,
      render:rgbSeen,filtered_count:remaining,
      ply_sha256:createHash('sha256').update(file).digest('hex'),download_bytes:file.length});
  }finally{
    await browser.close();fs.writeFileSync(path.join(out,'browser.json'),JSON.stringify(audit,null,2)+'\n');
  }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
