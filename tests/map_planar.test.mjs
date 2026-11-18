// The independent oracles below use an analytical rotated grid and exact canvas
// coordinates. Browser tests separately exercise dispatch and paint on real DOM.
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import test from "node:test";
import {runInNewContext} from "node:vm";

function view() {
  const handlers=new Map();
  const noOp=()=>{};
  const pixels=(w,h)=>({data:new Uint8ClampedArray(w*h*4)});
  const context={createImageData:pixels,putImageData:noOp,measureText:s=>({width:s.length*10})};
  for (const name of ["setTransform","clearRect","save","restore","translate","scale","rotate","drawImage",
    "strokeRect","fillRect","beginPath","moveTo","lineTo","closePath","stroke","strokeText","fillText","setLineDash"]) {
    context[name]=noOp;
  }
  const canvas={clientWidth:400,clientHeight:400,getContext:()=>context,
    getBoundingClientRect:()=>({left:0,top:0}),setPointerCapture:noOp,
    addEventListener:(name,fn)=>handlers.set(name,fn)};
  const sandbox={window:{},document:{createElement:()=>({getContext:()=>context})}};
  for(const file of ["grid.js","planar.js"]) {
    runInNewContext(readFileSync(new URL("../perception/map3d/web/"+file,import.meta.url),"utf8"),sandbox);
  }
  let readout="";
  const renderer=sandbox.window.createPlanarRenderer(canvas,{invalidate:noOp,onPick:text=>{readout=text;}});
  const input=(name,props)=>handlers.get(name)({button:0,pointerId:1,preventDefault:noOp,...props});
  const click=(x,y)=>{
    input("pointerdown",{clientX:x,clientY:y});
    input("pointerup",{clientX:x,clientY:y});
    return readout;
  };
  const opts={grid:true,cloud:true,scene:true,pose:true,connected:true};
  return {draw:(map,options)=>renderer.render(map,{...opts,...options}),click,input,get readout(){return readout;}};
}

function fixture() {
  // With yaw = +90 deg, +column points up and +row points left on screen.
  return {frame:"odom",generation:3,grid:{frame:"odom",status:"online",width:3,height:3,resolution:1,
    origin:[5,1,Math.PI/2],cells:[-1,0,100,5,67,36,9,8,7]}};
}

test("rotated occupancy cells, anchored wheel zoom, and keyboard reset retain metric values",()=>{
  const v=view(), data=fixture();v.draw(data);
  assert.match(v.click(200,200),/X 3.50 m · Y 2.50 m.*栅格 \(1, 1\).*值 67/);
  // Fitted scale is 112 px per metre. This is one column further up and one row left.
  const anchor=v.click(88,88);
  assert.match(anchor,/X 2.50 m · Y 3.50 m.*栅格 \(2, 2\).*值 7/);
  v.input("wheel",{clientX:88,clientY:88,deltaY:-360,deltaMode:0});v.draw(data);
  assert.equal(v.click(88,88),anchor);
  v.input("keydown",{key:"Home"});v.draw(data);v.input("keydown",{key:"Enter"});
  assert.match(v.readout,/X 3.50 m · Y 2.50 m.*值 67/);
});

test("picks follow cell updates and freshness; generation changes and frame mismatch remove stale facts",()=>{
  const v=view(), data=fixture();
  v.draw(data);v.click(200,200);
  const stale={...data,grid:{...data.grid,status:"stale",cells:Array(9).fill(-1)}};
  v.draw(stale);assert.match(v.readout,/未知 · 值 -1 · 上次观测/);
  const changed={...stale,grid:{...stale.grid,frame:"world"}};
  assert.equal(v.draw(changed).empty,true);
  assert.match(v.readout,/无可见占据栅格/);
  const cloud={frame:"odom",generation:4,cloud:{status:"online",points:[[0,0,.42]]}};
  v.draw(cloud);assert.equal(v.readout,"");
  assert.match(v.click(200,200),/无可见占据栅格.*回波 XY 投影 · Z 0.42 m/);
  assert.equal(v.draw(cloud,{cloud:false}).empty,true);
});
