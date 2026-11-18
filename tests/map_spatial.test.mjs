// Exercise the perspective depth/pick buffer independently of browser gestures
// and ROS. The surface mock receives the exact ImageData shipped to the canvas.
import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {runInNewContext} from 'node:vm';

function viewer() {
  const sandbox = {window:{},devicePixelRatio:1};
  for (const file of ['grid.js','spatial.js']) {
    const source = new URL('../perception/map3d/web/'+file,import.meta.url);
    runInNewContext(readFileSync(source,'utf8'),sandbox);
  }
  let readout='', pixels;
  const listeners={};
  const context={
    createImageData:(w,h)=>({data:new Uint8ClampedArray(w*h*4)}),
    putImageData:image=>{pixels=image.data;},
    beginPath(){},arc(){},stroke(){},save(){},restore(){},strokeText(){},fillText(){},measureText:s=>({width:s.length*10})
  };
  const canvas={clientWidth:180,clientHeight:180,style:{},getContext:()=>context,
    addEventListener:(name,fn)=>{listeners[name]=fn;},focus(){},setPointerCapture(){},
    getBoundingClientRect:()=>({left:0,top:0,width:180,height:180})};
  const renderer=sandbox.window.createSpatialRenderer(canvas,{invalidate(){},onPick:s=>{readout=s;}});
  const skin={cloud:true,scene:true,roof:false,pose:true,grid:true,connected:true,mutedSources:new Set()};
  return {renderer,
    draw:(map,opts={})=>renderer.render(map,{...skin,...opts}),
    press:(key)=>listeners.keydown({key,preventDefault(){}}),
    click:(x,y)=>{
      const event={button:0,pointerId:1,clientX:x,clientY:y,preventDefault(){}};
      listeners.pointerdown(event); listeners.pointerup({...event,type:'pointerup'});return readout;
    },
    pixel:(x,y)=>[...pixels.slice(4*(180*y+x),4*(180*y+x)+3)],
    get readout(){return readout;}
  };
}

test('top camera picks the forward surface independently of point order and hidden sources',()=>{
  const v=viewer(),sample={frame:'odom',cloud:{source_frame:'lidar',status:'online',points:[[0,0,.75],[0,0,0]]},
    cloud_sources:{other:{name:'第二路',status:'online',points:[[0,0,1.25]]}}};
  v.renderer.top();v.draw(sample);
  assert.match(v.click(90,90),/第二路.*Z 1\.25 m · odom/);
  v.draw(sample,{mutedSources:new Set(['other'])});
  assert.match(v.click(90,90),/lidar.*Z 0\.75 m · odom/);
  const scene={features:[{label:'屋顶',kind:'roof',xyz:[0,0,1.8],size:[2,2,.4]}]};
  v.draw({...sample,scene},{roof:true});
  assert.match(v.click(90,90),/屋顶.*Z 2\.20 m/);
  v.draw({...sample,scene},{roof:false});
  assert.match(v.click(90,90),/第二路.*Z 1\.25 m/);
});

test('raised rotated grid shares color, row-major cell lookup, frame and semantic labels',()=>{
  const v=viewer(),grid={frame:'odom',status:'online',width:3,height:3,resolution:1,
    origin:[9,-3,Math.PI/2],origin_z:2.5,cells:[-1,0,100,0,64,100,-1,0,100]};
  const map={frame:'odom',grid};
  v.renderer.top();v.draw(map);
  assert.match(v.click(90,90),/列 1 行 1 · 占据概率 64% · 值 64.*Z 2\.50 m · odom/);
  assert.deepEqual(v.pixel(90,90),[170,147,114]);
  // The rotated grid's column direction is +world Y, its row direction is -world X.
  const high=v.click(90,56),west=v.click(56,90);
  assert.match(high,/列 2 行 1 · 占据 · 值 100/);
  assert.match(west,/列 1 行 2 · 空闲 · 值 0/);
  assert.match(v.click(125,125),/未知 · 值 -1/);
  v.draw({...map,scene:{zones:[{kind:'outdoor',label:'草坪',bounds:[6,-3,9,0]}]}});
  assert.match(v.click(90,90),/草坪.*列 1 行 1.*Z 2\.50 m/);
  const waiting=v.draw({...map,grid:{...grid,frame:'map'}});
  assert.equal(waiting.empty,true);
  assert.equal(v.click(90,90),'');
});

test('keyboard inspection can find an isolated point, and clear it',()=>{
  const v=viewer();
  v.draw({frame:'odom',cloud:{topic:'/points',points:[[0,0,1.25]]}});
  v.press('Enter');
  assert.match(v.readout,/实测点 · \/points.*Z 1\.25 m/);
  v.press('Escape');assert.equal(v.readout,'');
});

test('north-up UV, relief depth and survey provenance survive perspective and toggles',()=>{
  const v=viewer(),size=16,pixels=new Uint8ClampedArray(4*size*size),heights=new Uint8ClampedArray(pixels.length);
  for(let y=0;y<size;y++)for(let x=0;x<size;x++){
    const k=4*(y*size+x);
    pixels.set(y<8?[40,170,70,255]:[160,60,40,255],k);
    heights[k]=x>=5&&x<=11&&y>=5&&y<=11 ? 8 : 0;
  }
  const texture={frame:'odom',bounds:[-8,-8,8,8],width:size,height:size,pixels,heights,
    height_scale_m:.5,height_offset_m:0,source:'Gazebo',areas:[{label:'教学楼',bounds:[-3,-3,3,3]}]};
  const map={frame:'odom',grid:{frame:'odom',status:'online',origin:[-8,-8,0],resolution:1,
    width:16,height:16,cells:Array(256).fill(-1)}};
  v.renderer.top();v.draw(map,{texture});
  assert.match(v.click(90,90),/仿真外观.*教学楼.*Gazebo.*Z 4\.00/);
  assert.deepEqual(v.pixel(90,49),[40,170,70]);
  assert.deepEqual(v.pixel(90,130),[160,60,40]);
  assert.match(v.click(90,49),/未知 · 值 -1.*Z 0\.00/);
  assert.equal(v.draw({frame:'map'},{texture}).empty,true);
  v.draw(map,{texture:null});assert.match(v.click(90,90),/未知 · 值 -1/);
  v.renderer.focusBounds([-.5,-.5,.5,.5]);v.draw({frame:'odom'},{texture});
  v.renderer.reset();v.draw({frame:'odom'},{texture});
  assert.match(v.click(90,90),/仿真外观/);
});

test('failed pose and semantic inputs disappear, retained measurements are marked as historical',()=>{
  const v=viewer(),cloud={name:'雷达',status:'online',points:[[0,0,.5]]};
  const feature={id:'wall',label:'墙面',kind:'wall',xyz:[0,0,1],size:[2,2,1]};
  const map={frame:'odom',cloud,scene:{status:'online',features:[feature]},pose:{status:'online',xyz:[0,0,3]}};
  v.renderer.top();v.draw(map);
  assert.match(v.click(90,90),/机器人位姿/);
  map.pose={...map.pose,status:'error',error:'pose_frame_mismatch'};
  v.draw(map);assert.equal(v.readout,'');assert.match(v.click(90,90),/墙面/);
  map.scene={...map.scene,status:'waiting'};
  v.draw(map);assert.equal(v.readout,'');assert.match(v.click(90,90),/实测点.*Z 0\.50/);
  assert.doesNotMatch(v.readout,/上次观测/);
  map.cloud={...cloud,status:'error',error:'no_valid_returns'};
  v.draw(map);assert.match(v.readout,/雷达.*上次观测/);
  assert.deepEqual(v.pixel(90,90),[141,160,163]);
  v.draw({...map,cloud});assert.doesNotMatch(v.readout,/上次观测/);
  const grid={frame:'odom',status:'online',width:1,height:1,resolution:1,origin:[0,0,0],cells:[0]};
  v.draw({frame:'odom',grid});
  assert.match(v.click(90,90),/空闲/);
  v.draw({frame:'odom',grid},{connected:false});assert.match(v.readout,/上次观测/);
  v.draw({frame:'odom',grid:{...grid,status:'stale'}});assert.match(v.readout,/上次观测/);
  v.draw({frame:'odom',grid});assert.doesNotMatch(v.readout,/上次观测/);
});
