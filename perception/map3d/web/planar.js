/* Read-only metric XY inspector. Coordinates refer to the ROS fixed frame.
   The camera stays level even if the OccupancyGrid origin is rotated. */
"use strict";
window.createPlanarRenderer = function createPlanarRenderer(canvas, {invalidate, onPick}) {
  const ctx = canvas.getContext("2d");
  const gridMath = window.EdgeGrid;
  const pointers = new Map();
  let snapshot, options, grid, camera, selection, lastReadout = "";
  let bitmap, bitmapSource, bitmapOverTexture, texture, frameKey, fitSource, requestedBounds, interacted = false;
  let width = 1, height = 1;
  let clouds = [], zones = [], features = [], robot = null;
  const ready = layer => layer && layer.status !== "error" && layer.status !== "waiting" && !layer.error;
  const recent = layer => options.connected && layer.status === "online";
  const xy = point => Array.isArray(point) && Number.isFinite(point[0]) && Number.isFinite(point[1]);
  const fmt = n => (Math.abs(n) < 0.005 ? 0 : n).toFixed(2);
  const screen = ([x,y]) => [width/2 + (x-camera.x)*camera.scale, height/2 - (y-camera.y)*camera.scale];
  const world = ([u,v]) => [camera.x + (u-width/2)/camera.scale, camera.y - (v-height/2)/camera.scale];

  function layers(data, opt) {
    snapshot = data;
    options = opt;
    const nextTexture = opt.texture?.frame === data?.frame ? opt.texture : null;
    if (texture && !nextTexture) interacted=true;
    texture=nextTexture;
    grid = opt.grid && gridMath.usable(data?.grid, data?.frame) ? data.grid : null;
    const scene = opt.scene && ready(data?.scene) ? data.scene : null;
    zones = scene?.zones || [];
    features = (scene?.features || []).filter(f => (opt.roof || f.kind !== "roof") && xy(f.xyz) && xy(f.size));
    clouds = opt.cloud ? [data?.cloud, ...Object.entries(data?.cloud_sources || {})
      .filter(([id]) => !opt.mutedSources?.has(id)).map(([,source]) => source)]
      .filter(source => ready(source) && source.points?.length) : [];
    robot = opt.pose && ready(data?.pose) && xy(data.pose.xyz) ? data.pose : null;
  }

  function frameBounds([xmin,ymin,xmax,ymax]) {
    camera={x:(xmin+xmax)/2, y:(ymin+ymax)/2,
      scale:Math.min(width*.94/Math.max(xmax-xmin,1),height*.90/Math.max(ymax-ymin,1))};
  }
  function focusBounds(bounds) {
    requestedBounds=bounds; selection=null; pointers.clear(); publishPick(); invalidate();
  }
  function fit() {
    if (texture) {frameBounds(texture.bounds); return;}
    let xmin=Infinity, xmax=-Infinity, ymin=Infinity, ymax=-Infinity;
    function include(x,y) {
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      xmin=Math.min(xmin,x); xmax=Math.max(xmax,x);
      ymin=Math.min(ymin,y); ymax=Math.max(ymax,y);
    }
    if (grid) {
      for (const col of [0,grid.width]) for (const row of [0,grid.height]) {
        include(...gridMath.toWorld(grid,col,row));
      }
    }
    for (const {bounds:[x0,y0,x1,y1]} of zones) { include(x0,y0); include(x1,y1); }
    for (const {xyz:[x,y],size:[w,h]} of features) { include(x-w/2,y-h/2); include(x+w/2,y+h/2); }
    for (const source of clouds) for (const [x,y] of source.points) include(x,y);
    if (robot) include(...robot.xyz);
    if (!Number.isFinite(xmin)) { xmin=ymin=-2; xmax=ymax=2; }
    camera = {
      x:(xmin+xmax)/2, y:(ymin+ymax)/2,
      scale:Math.min(width*.88/Math.max(xmax-xmin,1),height*.84/Math.max(ymax-ymin,1))
    };
  }

  function rasterize(source) {
    const image=document.createElement("canvas");
    image.width=source.width; image.height=source.height;
    const context=image.getContext("2d"), pixels=context.createImageData(image.width,image.height);
    for (let i=0; i<source.cells.length; ++i) {
      const offset=4*i, rgb=gridMath.color(source.cells[i]);
      pixels.data[offset]=rgb[0]; pixels.data[offset+1]=rgb[1]; pixels.data[offset+2]=rgb[2];
      const value=source.cells[i];
      pixels.data[offset+3]=texture ? (value<0 ? 0 : 68+value*1.8) : 255;
    }
    context.putImageData(pixels,0,0);
    return image;
  }
  function drawRaster() {
    if (!grid) { bitmap=null; bitmapSource=null; return; }
    if (bitmapSource !== grid || bitmapOverTexture !== !!texture) {
      bitmap=rasterize(grid); bitmapSource=grid; bitmapOverTexture=!!texture;
    }
    const pixel=grid.resolution*camera.scale;
    ctx.save();
    ctx.translate(...screen(grid.origin));
    ctx.scale(pixel,-pixel);
    ctx.rotate(grid.origin[2]);
    ctx.imageSmoothingEnabled=false;
    ctx.globalAlpha=recent(grid) ? 1 : .66;
    ctx.drawImage(bitmap,0,0);
    ctx.lineWidth=1/pixel; ctx.strokeStyle="#9ebbb6";
    ctx.strokeRect(0,0,grid.width,grid.height);
    ctx.restore();
  }

  function drawAppearance() {
    if (!texture) return;
    const [xmin,ymin,xmax,ymax]=texture.bounds;
    const [u,v]=screen([xmin,ymax]);
    ctx.save(); ctx.imageSmoothingEnabled=true;
    ctx.drawImage(texture.image,u,v,(xmax-xmin)*camera.scale,(ymax-ymin)*camera.scale);
    ctx.restore();
  }

  function drawRoute() {
    if(!options.route)return;
    const points=options.route.points.map(screen);
    ctx.save();ctx.beginPath();ctx.moveTo(...points[0]);
    for(const p of points.slice(1))ctx.lineTo(...p);
    ctx.lineJoin="round";ctx.lineCap="round";
    ctx.lineWidth=5;ctx.strokeStyle="#0f3b48";ctx.stroke();
    ctx.lineWidth=2.8;ctx.strokeStyle="#22dfe9";ctx.stroke();ctx.restore();
  }
  function drawAreaNames() {
    if (!texture || !options.scene) return;
    ctx.save(); ctx.font="600 13px system-ui"; ctx.textAlign="center";
    const placed=[];
    for (const area of texture.areas || []) {
      const [a,b,c,d]=area.bounds;
      const [x,y]=screen([(a+c)/2,(b+d)/2]);
      const size=ctx.measureText(area.label).width;
      if (x-size/2<4 || x+size/2>width-4 || y<26 || y>height-18) continue;
      if (placed.some(p=>Math.abs(p.x-x)<(p.size+size)/2+6 && Math.abs(p.y-y)<17)) continue;
      ctx.lineWidth=4; ctx.strokeStyle="#192d28e6"; ctx.fillStyle="#fffbe9";
      ctx.strokeText(area.label,x,y); ctx.fillText(area.label,x,y); placed.push({x,y,size});
    }
    ctx.restore();
  }

  function drawSemantic() {
    ctx.save();
    ctx.globalAlpha=recent(snapshot?.scene || {}) ? 1 : .65;
    for (const {bounds:[x0,y0,x1,y1],kind,label} of zones) {
      const [x,y]=screen([x0,y1]), w=(x1-x0)*camera.scale, h=(y1-y0)*camera.scale;
      ctx.fillStyle=kind==="indoor" ? "#79a8dc19" : "#91c38c1a";
      ctx.strokeStyle=kind==="indoor" ? "#90b0cfbd" : "#95be91bb";
      ctx.setLineDash([4,4]); ctx.fillRect(x,y,w,h); ctx.strokeRect(x,y,w,h);
      if (label && h>28) {
        ctx.font="600 12px system-ui";
        const left=Math.max(x,0)+8, top=Math.max(y,0)+17;
        if (left+ctx.measureText(label).width<Math.min(x+w,width)-4 && top<Math.min(y+h,height)-4) {
          ctx.fillStyle="#e2ede7"; ctx.strokeStyle="#30454b"; ctx.lineWidth=3;
          ctx.strokeText(label,left,top); ctx.fillText(label,left,top); ctx.lineWidth=1;
        }
      }
    }
    ctx.setLineDash([]);
    for (const f of features) {
      const [x,y]=screen([f.xyz[0]-f.size[0]/2,f.xyz[1]+f.size[1]/2]);
      const w=f.size[0]*camera.scale, h=f.size[1]*camera.scale;
      ctx.fillStyle=f.kind==="wall" ? "#adc5c4a0" : "#b8d29a8a";
      ctx.strokeStyle="#d6debf9c"; ctx.fillRect(x,y,w,h); ctx.strokeRect(x,y,w,h);
    }
    ctx.restore();
  }

  function drawCloudAndPose() {
    for (const source of clouds) {
      ctx.fillStyle=recent(source) ? "#66e4d3bd" : "#a3b0b3a0";
      for (const point of source.points) {
        const [x,y]=screen(point);
        ctx.fillRect(x-1,y-1,2,2);
      }
    }
    if (!robot) return;
    const [x,y]=screen(robot.xyz);
    ctx.strokeStyle="#20302f"; ctx.lineWidth=2; ctx.fillStyle=recent(robot) ? "#ffc56c" : "#bfad90";
    ctx.beginPath(); ctx.arc(x,y,5.5,0,Math.PI*2); ctx.fill(); ctx.stroke();
    ctx.font="600 12px system-ui"; ctx.fillText("机器人",x+9,y-8);
  }

  function drawScale() {
    // Label a physical distance and fixed axes, without assuming true north.
    const target=Math.min(85,width*.22)/camera.scale;
    const decade=10**Math.floor(Math.log10(target));
    const meters=([5,2,1].find(n => n*decade<=target) || 1)*decade;
    const length=meters*camera.scale;
    const x=16, y=height-15;
    ctx.save(); ctx.lineWidth=2; ctx.strokeStyle="#bbd5d0";
    ctx.beginPath(); ctx.moveTo(x,y-4); ctx.lineTo(x,y); ctx.lineTo(x+length,y); ctx.lineTo(x+length,y-4); ctx.stroke();
    ctx.font="11px ui-monospace, monospace"; ctx.textAlign="left"; ctx.fillStyle="#d7ebe7";
    ctx.strokeStyle="#20383e"; ctx.lineWidth=3;
    const label=Number(meters.toPrecision(3))+" m";
    ctx.strokeText(label,x,y-7); ctx.fillText(label,x,y-7);
    ctx.strokeText("↑ +Y    → +X",15,21); ctx.fillText("↑ +Y    → +X",15,21);
    ctx.restore();
  }

  function selectionText() {
    if (!selection) return "";
    const [x,y]=selection;
    const parts=[`X ${fmt(x)} m · Y ${fmt(y)} m`];
    if (texture && x>=texture.bounds[0] && y>=texture.bounds[1] && x<=texture.bounds[2] && y<=texture.bounds[3]) {
      const area=(texture.areas || []).filter(({bounds:[a,b,c,d]})=>x>=a && x<=c && y>=b && y<=d)
        .sort((p,q)=>{const size=({bounds:[a,b,c,d]})=>(c-a)*(d-b);return size(p)-size(q);})[0];
      parts.push("仿真外观"+(area ? " · "+area.label : "")+" · "+texture.source);
    }
    const cell=grid && gridMath.cellAt(grid,x,y);
    if (cell) parts.push(`栅格 (${cell.col}, ${cell.row}) · ${gridMath.describe(cell.value)} · 值 ${cell.value}`
      +(recent(grid)?"":" · 上次观测"));
    else if (grid) parts.push("栅格范围外");
    else parts.push("无可见占据栅格");
    const feature=features.find(f => Math.abs(x-f.xyz[0])<=f.size[0]/2 && Math.abs(y-f.xyz[1])<=f.size[1]/2);
    const zone=zones.find(({bounds:[x0,y0,x1,y1]}) => x>=x0 && x<=x1 && y>=y0 && y<=y1);
    if (feature || zone) parts.push("场景先验 · "+(feature?.label || feature?.id || zone?.label || zone?.kind || "区域"));
    if (!cell && clouds.length) {
      let closest, dist=7/camera.scale, origin;
      for (const source of clouds) for (const p of source.points) {
        const d=Math.hypot(x-p[0],y-p[1]);
        if (d<=dist) {closest=p; dist=d; origin=source;}
      }
      if (closest) parts.push(`回波 XY 投影 · Z ${fmt(closest[2])} m`+(recent(origin)?"":" · 上次观测"));
    }
    return parts.join(" ｜ ");
  }

  function publishPick() {
    const next=selectionText();
    if (next!==lastReadout) { lastReadout=next; onPick(next); }
  }
  function markSelection() {
    if (!selection) return;
    const [x,y]=screen(selection);
    ctx.save(); ctx.strokeStyle="#f7da75"; ctx.lineWidth=1.5;
    if (grid) {
      const cell=gridMath.cellAt(grid,...selection);
      if (cell) {
        const corners=[[0,0],[1,0],[1,1],[0,1]].map(([dc,dr])=>screen(gridMath.toWorld(grid,cell.col+dc,cell.row+dr)));
        ctx.beginPath(); ctx.moveTo(...corners[0]); corners.slice(1).forEach(c=>ctx.lineTo(...c));
        ctx.closePath(); ctx.stroke();
      }
    }
    ctx.beginPath(); ctx.moveTo(x-6,y); ctx.lineTo(x+6,y); ctx.moveTo(x,y-6); ctx.lineTo(x,y+6); ctx.stroke();
    ctx.restore();
  }

  function render(data, opt) {
    const resized=width!==canvas.clientWidth || height!==canvas.clientHeight;
    width=canvas.clientWidth; height=canvas.clientHeight;
    if (!width || !height) return {empty:true};
    const dpr=Math.min(2,window.devicePixelRatio || 1);
    const w=Math.round(width*dpr), h=Math.round(height*dpr);
    if (canvas.width!==w || canvas.height!==h) {canvas.width=w; canvas.height=h;}
    ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height);
    layers(data,opt);
    const key=JSON.stringify([data?.frame,data?.generation]);
    if (frameKey!==key) { frameKey=key; interacted=false; selection=null; camera=null; }
    const source=texture || (grid?"grid":zones.length || features.length?"scene":clouds.length?"cloud":robot?"pose":"none");
    if (requestedBounds) {frameBounds(requestedBounds); requestedBounds=null; interacted=true;}
    else if (!camera || (!interacted && (fitSource!==source || resized))) fit();
    fitSource=source;
    drawAppearance(); drawRaster(); drawSemantic(); drawRoute(); drawAreaNames(); drawCloudAndPose(); drawScale(); markSelection(); publishPick();
    return {empty:source==="none"};
  }

  function reset() {
    camera=null; requestedBounds=null; interacted=false; selection=null; pointers.clear(); publishPick(); invalidate();
  }
  function zoomAt(factor, anchor) {
    if (!camera) return;
    const before=world(anchor);
    camera.scale=Math.max(.00001, Math.min(100000, camera.scale*factor));
    const after=world(anchor);
    camera.x+=before[0]-after[0]; camera.y+=before[1]-after[1];
    interacted=true; invalidate();
  }
  const center=()=>[width/2,height/2];
  const zoomBy=factor=>zoomAt(factor,center());
  function pan(dx,dy) {
    camera.x-=dx/camera.scale; camera.y+=dy/camera.scale;
    interacted=true; invalidate();
  }
  function pick(position) {
    selection=world(position); publishPick(); invalidate();
  }
  function position(event) {
    const rect=canvas.getBoundingClientRect();
    return [event.clientX-rect.left,event.clientY-rect.top];
  }
  function pair() {
    const [a,b]=[...pointers.values()].map(p=>p.at);
    return {at:[(a[0]+b[0])/2,(a[1]+b[1])/2],distance:Math.hypot(b[0]-a[0],b[1]-a[1])};
  }
  canvas.addEventListener("pointerdown", event=>{
    if (!camera || event.button!==0 || pointers.size>=2) return;
    const at=position(event);
    pointers.set(event.pointerId,{at, start:at, moved:false});
    if (pointers.size===2) for (const p of pointers.values()) p.moved=true;
    canvas.setPointerCapture(event.pointerId);
  });
  canvas.addEventListener("pointermove", event=>{
    const pointer=pointers.get(event.pointerId);
    if (!pointer || !camera) return;
    const next=position(event), previous=pointer.at;
    const before=pointers.size===2 ? pair() : null;
    pointer.at=next;
    if (before) {
      const after=pair();
      if (before.distance>2 && after.distance>2) zoomAt(after.distance/before.distance,before.at);
      pan(after.at[0]-before.at[0],after.at[1]-before.at[1]);
    } else if (pointer.moved) {
      pan(next[0]-previous[0],next[1]-previous[1]);
    } else if (Math.hypot(next[0]-pointer.start[0],next[1]-pointer.start[1])>4) {
      pointer.moved=true; pan(next[0]-pointer.start[0],next[1]-pointer.start[1]);
    }
  });
  canvas.addEventListener("pointerup", event=>{
    const pointer=pointers.get(event.pointerId);
    if (!pointer) return;
    pointers.delete(event.pointerId);
    if (!pointer.moved && camera) pick(position(event));
  });
  for (const name of ["lostpointercapture","pointercancel"]) {
    canvas.addEventListener(name,event=>pointers.delete(event.pointerId));
  }
  canvas.addEventListener("wheel",event=>{
    event.preventDefault();
    const unit=event.deltaMode===1?16:event.deltaMode===2?height:1;
    zoomAt(Math.exp(-Math.max(-450,Math.min(450,event.deltaY*unit))*.0016),position(event));
  },{passive:false});
  canvas.addEventListener("keydown",event=>{
    if (!camera) return;
    const delta=event.shiftKey ? 75 : 24;
    switch(event.key) {
      case "ArrowLeft": pan(delta,0); break;
      case "ArrowRight": pan(-delta,0); break;
      case "ArrowUp": pan(0,delta); break;
      case "ArrowDown": pan(0,-delta); break;
      case "+": case "=": zoomBy(1.35); break;
      case "-": zoomBy(1/1.35); break;
      case "Home": reset(); break;
      case "Enter": pick(center()); break;
      case "Escape": selection=null; publishPick(); invalidate(); break;
      default: return;
    }
    event.preventDefault();
  });
  return {render,reset,zoomBy,focusBounds,clearPick() { selection=null; publishPick(); invalidate(); }};
};
