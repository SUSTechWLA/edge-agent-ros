/** Read only a selected Gazebo task and compare its map rendering with the planner and odometry records. */
'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs/promises');
const {setTimeout:sleep}=require('node:timers/promises');
const {parseArgs}=require('node:util');
const {values}=parseArgs({options:{url:{type:'string'},browser:{type:'string'},
  puppeteer:{type:'string',default:'puppeteer-core'},output:{type:'string'},screenshot:{type:'string'},watch:{type:'boolean'}}});
const url=new URL(values.url);
assert.equal(url.hostname,'127.0.0.1');
assert.equal(url.pathname,'/exploration-mission.html');
const id=url.searchParams.get('task_id');
assert(id && values.browser);

async function check(){
  if(values.watch){
    const source=new URL('/api/exploration-mission',url);source.searchParams.set('task_id',id);
    const end=Date.now()+60*60*1000;
    while(true){
      try{
        const response=await fetch(source,{signal:AbortSignal.timeout(2500)});
        if(response.ok && (await response.json()).mission?.id===id)break;
      }catch{/* The site can be restarted before this task is submitted. */}
      assert(Date.now()<end,'No recorded task appeared on the console');
      await sleep(1500);
    }
  }
  const browser=await require(values.puppeteer).launch({headless:true,executablePath:values.browser});
  try{
    const page=await browser.newPage(), errors=[], writes=[];
    await page.setViewport({width:1600,height:1000});
    await page.setRequestInterception(true);
    page.on('request',request=>{
      if(request.method()==='GET' && new URL(request.url()).origin===url.origin)return void request.continue();
      if(request.method()!=='GET')writes.push(request.method());
      return void request.abort();
    });
    page.on('pageerror',e=>errors.push(String(e)));
    await page.goto(url.href,{waitUntil:'domcontentloaded'});
    await page.waitForFunction(()=>!document.querySelector('#route-inset').hidden &&
      document.querySelector('#map').dataset.overlayRegistered==='true',{timeout:30000});
    let underway=null;
    if(values.watch){
      await page.waitForFunction(()=>{
        const row=displayed;
        if(row?.status==='BLOCKED' || row?.status==='FAILED')throw Error(row.status+' · '+row.phase);
        if(!row?.initial_pose || !row.goal || row.status!=='RUNNING' || row.pose?.speed<.04)return false;
        const origin=row.initial_pose;
        return row.initial_global_path && row.global_path && !document.querySelector('#leg-legend').hidden &&
          Math.hypot(row.pose.x-origin.x,row.pose.y-origin.y)>.3*Math.hypot(row.goal[0]-origin.x,row.goal[1]-origin.y);
      },{timeout:20*60*1000});
      underway=await page.evaluate(()=>{
        const row=displayed, labels=['route-legend','initial-legend','leg-legend'];
        return {phase:row.phase,status:row.status,pose:row.pose,operation:row.global_path.operation_id,
          edition:row.map_ref,firstPlanningPoints:row.initial_global_path.points.length,remainingPlanningPoints:row.global_path.points.length,
          legPoints:row.path.length,trajectoryPoints:row.trajectory.length,inset:document.querySelector('#inset-distance').textContent,
          legends:labels.map(id=>document.getElementById(id)).filter(e=>!e.hidden).map(e=>e.textContent)};
      });
      process.stdout.write(JSON.stringify({underway})+'\n');
      await page.waitForFunction(()=>['BLOCKED','FAILED','SUCCEEDED'].includes(displayed?.status),{timeout:25*60*1000});
    }
    const report=await page.evaluate(async id=>{
      const {mission}=await (await fetch('/api/exploration-mission?task_id='+encodeURIComponent(id))).json();
      const route=mission.initial_global_path?.points || mission.global_path?.points ||
        (mission.path?.length ? mission.path : mission.last_navigation_path.points);
      const length=route.reduce((sum,p,i)=>sum+(i?Math.hypot(p[0]-route[i-1][0],p[1]-route[i-1][1]):0),0);
      const canvas=document.querySelector('#route-inset-map');
      const pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
      const coverage=(rgb)=>{
        let count=0,minX=Infinity,maxX=-Infinity,minY=Infinity,maxY=-Infinity;
        for(let i=0;i<pixels.length;i+=4){
          if(rgb.some((c,k)=>c!==pixels[i+k]))continue;
          const x=i/4%canvas.width,y=Math.floor(i/4/canvas.width);
          minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);count++;
        }
        return {count,span:count?Math.hypot(maxX-minX,maxY-minY):0};
      };
      const plan=mission.initial_global_path || mission.global_path || mission.path?.length ? [165,241,184] : [167,201,157];
      return {id:mission.id,status:mission.status,phase:mission.phase,initialPose:mission.initial_pose,finalPose:mission.pose,
        target:mission.goal,map:mission.map_ref,planMetres:length,
        distanceLabel:document.querySelector('#inset-distance').textContent,legend:document.querySelector('#route-legend').textContent,
        planPixels:coverage(plan),trailPixels:coverage([105,212,227]),
        trajectoryPoints:mission.trajectory.length,campusBounds:JSON.parse(document.querySelector('#map').dataset.atlasBounds)};
    },id);
    assert.equal(report.id,id);assert.equal(report.status,'SUCCEEDED');
    if(underway){
      report.underway=underway;
      assert.deepEqual(underway.edition,report.map);
      assert.equal(report.phase,'ARRIVED');
      assert(Math.abs(report.finalPose.speed)<.02);
    }
    assert(report.distanceLabel.includes(report.planMetres.toFixed(1)+' m'));
    assert(report.planPixels.span>40 && report.trailPixels.span>40,'Planner reference and odometry must be separately legible');
    // Exercise reject conditions against copies inside this browser tab, without modifying the task or the site.
    const contract=await page.evaluate(async id=>{
      const {mission}=await (await fetch('/api/exploration-mission?task_id='+encodeURIComponent(id))).json();
      const first=mission.initial_global_path || {...mission.last_navigation_path,map_ref:mission.map_ref};
      const latest={...first,points:first.points.slice(-2),operation_id:id+'/leg/999'};
      const row={...mission,status:'RUNNING',phase:'NAVIGATING',path:latest.points,initial_global_path:first,global_path:latest};
      const progress=campusRouteLayers(row), arrived=campusRouteLayers({...row,status:'SUCCEEDED'});
      return {current:progress.current===latest.points,original:progress.original===first.points,
        terminal:arrived.current===first.points && !arrived.leg,
        unrelated:globalNavigationPath(row,{...latest,operation_id:'different/leg/1'})===null,
        incompatibleMap:globalNavigationPath(row,{...latest,map_ref:{...row.map_ref,digest:'different'}})===null,
        incompatibleFrame:globalNavigationPath(row,{...latest,frame:'different'})===null};
    },id);
    assert(Object.values(contract).every(Boolean),JSON.stringify(contract));
    report.browserContract=contract;
    if(values.screenshot)await page.screenshot({path:values.screenshot,fullPage:true});
    await page.click('#route-inset');
    await page.waitForFunction(()=>document.querySelector('#route-inset').hidden &&
      Number(document.querySelector('#map').dataset.cameraZoom)>1);
    report.focusZoom=Number(await page.$eval('#map',e=>e.dataset.cameraZoom));
    await page.click('#view-reset');
    await page.waitForFunction(()=>!document.querySelector('#route-inset').hidden);
    await page.click('#view-local');
    await page.waitForFunction(()=>document.querySelector('#route-inset').hidden &&
      document.querySelector('#map').dataset.view==='local');
    assert.deepEqual(errors,[]);assert.deepEqual(writes,[]);
    const json=JSON.stringify({...report,errors,writes},null,2);
    if(values.output)await fs.writeFile(values.output,json+'\n');
    process.stdout.write(json+'\n');
  }finally{await browser.close();}
}
check().catch(error=>{console.error(error);process.exitCode=1;});
