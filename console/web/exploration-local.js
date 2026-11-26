/* Read-only map cameras, in the selected mission's grid frame. */
function initialCamera(local) {
  return {center:null, yaw:local ? -.50 : 0, tilt:local ? .72 : Math.PI / 2, zoom:1};
}
const cameras = {local: initialCamera(true), global: initialCamera(false)};
let cameraView = null;
let lastTiles;

function gridTiles(grid) {
  if (grid === lastTiles?.grid) return lastTiles.canvas;
  const canvas = document.createElement('canvas');
  canvas.width = grid.width;
  canvas.height = grid.height;
  const ctx = canvas.getContext('2d');
  const pixels = ctx.createImageData(grid.width, grid.height);
  const colors = [[34,52,46,255], [100,127,110,255], [191,202,182,255]];
  grid.cells.forEach((cell, i) => pixels.data.set(colors[!Number.isFinite(cell) || cell < 0 ? 0 : cell > 49 ? 2 : 1], 4*i));
  ctx.putImageData(pixels, 0, 0);
  lastTiles = {grid, canvas};
  return canvas;
}

const clampCamera = (n, lo, hi) => Math.max(lo, Math.min(hi,n));

function setCameraView(canvas, mode, follow, baseScale, constrain) {
  const camera = cameras[mode], center = camera.center || follow;
  const scale = baseScale * camera.zoom;
  const ca = Math.cos(camera.yaw), sa = Math.sin(camera.yaw);
  const ct = Math.cos(camera.tilt), st = Math.sin(camera.tilt);
  const ox = canvas.clientWidth * .5, oy = canvas.clientHeight * (mode === 'local' ? .60 : .50);
  const project = ([x,y,z=0]) => [ox + scale*(ca*(x-center[0])+sa*(y-center[1])),
    oy + scale*((sa*(x-center[0])-ca*(y-center[1]))*st-z*ct)];
  // Invert the ground-plane projection so the point under the pointer stays
  // under it at every yaw, tilt and zoom, also on high-DPI displays.
  function groundOffset(x,y) {
    const s = baseScale * camera.zoom, up = y / Math.sin(camera.tilt);
    const cos = Math.cos(camera.yaw), sin = Math.sin(camera.yaw);
    return [(cos*x+sin*up)/s, (sin*x-cos*up)/s];
  }
  cameraView = {mode,camera,follow,ox,oy,groundOffset,constrain};
  Object.assign(canvas.dataset, {cameraX:String(center[0]),cameraY:String(center[1]),
    cameraYaw:String(camera.yaw),cameraTilt:String(camera.tilt),cameraZoom:String(camera.zoom)});
  return {center,scale,project,ca,sa,ct,st};
}

function bindMapCamera(canvas, redraw) {
  const pointers = new Map();
  const rotateButton = document.getElementById('view-rotate');
  let scheduled = false;
  function invalidate() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => { scheduled = false; redraw(); });
  }
  function orbitBy(dx,dy) {
    const c=cameraView.camera;
    if (!dx && !dy) return;
    if (!c.center) c.center=cameraView.follow.slice();
    c.yaw=(c.yaw+dx*.007)%(2*Math.PI);
    c.tilt=clampCamera(c.tilt+dy*.005,.24,1.43);
    invalidate();
  }
  function moveGround(from,to,factor=1) {
    const {camera,follow,groundOffset,constrain,ox,oy}=cameraView;
    const rect=canvas.getBoundingClientRect();
    const offset = p => groundOffset(p.x-rect.left-ox,p.y-rect.top-oy);
    const old=offset(from), center=camera.center || follow;
    camera.zoom=clampCamera(camera.zoom*factor,.55,cameraView.mode==='local'?2.8:(cameraView.maxZoom || 8));
    const next=offset(to);
    camera.center=constrain([center[0]+old[0]-next[0], center[1]+old[1]-next[1]]);
    invalidate();
  }
  const cursor = e => ({x:e.clientX,y:e.clientY});
  const midpoint = (a,b) => ({x:(a.x+b.x)/2,y:(a.y+b.y)/2});
  const gap = (a,b) => Math.hypot(a.x-b.x,a.y-b.y);
  canvas.addEventListener('pointerdown', e => {
    if (!cameraView || (e.button!==0 && !(e.button===2 && cameraView.mode==='local')) || pointers.size>=2) return;
    e.preventDefault(); canvas.focus({preventScroll:true}); canvas.setPointerCapture(e.pointerId);
    pointers.set(e.pointerId,{...cursor(e),rotate:cameraView.mode==='local' &&
      (e.button===2 || e.shiftKey || rotateButton.getAttribute('aria-pressed')==='true')});
    canvas.classList.add('dragging');
  });
  canvas.addEventListener('pointermove', e => {
    const p=pointers.get(e.pointerId);
    if (!p || !cameraView) return;
    const next=cursor(e), other=[...pointers.values()].find(q => q!==p);
    if (other) {
      const oldGap=gap(p,other), newGap=gap(next,other);
      moveGround(midpoint(p,other),midpoint(next,other),oldGap>1 && newGap>1 ? newGap/oldGap : 1);
    } else if (p.rotate) orbitBy(next.x-p.x,next.y-p.y);
    else moveGround(p,next);
    Object.assign(p,next);
  });
  function release(e) {
    pointers.delete(e.pointerId);
    canvas.classList.toggle('dragging',pointers.size>0);
  }
  for (const name of ['pointerup','pointercancel','lostpointercapture']) canvas.addEventListener(name,release);
  canvas.addEventListener('contextmenu', e => { if (cameraView?.mode==='local') e.preventDefault(); });
  canvas.addEventListener('wheel', e => {
    if (!cameraView) return;
    e.preventDefault();
    const unit=e.deltaMode===1 ? 16 : e.deltaMode===2 ? canvas.clientHeight : 1;
    moveGround(cursor(e),cursor(e),Math.exp(-clampCamera(e.deltaY*unit,-250,250)*.0017));
  },{passive:false});
  function reset() {
    if (cameraView) Object.assign(cameraView.camera,initialCamera(cameraView.mode==='local'));
    rotateButton.setAttribute('aria-pressed','false');
    redraw();
  }
  canvas.addEventListener('keydown', e => {
    if (!cameraView) return;
    const d={ArrowLeft:[-24,0],ArrowRight:[24,0],ArrowUp:[0,-24],ArrowDown:[0,24]}[e.key];
    const rect=canvas.getBoundingClientRect(), at={x:rect.left+cameraView.ox,y:rect.top+cameraView.oy};
    if (d && cameraView.mode==='local' && (e.shiftKey || rotateButton.getAttribute('aria-pressed')==='true')) orbitBy(...d);
    else if (d) moveGround(at,{x:at.x+d[0],y:at.y+d[1]});
    else if (['+','=','-'].includes(e.key)) moveGround(at,at,e.key==='-'?1/1.12:1.12);
    else if (e.key==='Home') reset();
    else return;
    e.preventDefault();
  });
  rotateButton.onclick = () => {
    rotateButton.setAttribute('aria-pressed',String(rotateButton.getAttribute('aria-pressed')!=='true'));
    redraw();
  };
  document.getElementById('view-reset').onclick=reset;
  return () => {
    for (const id of pointers.keys()) if (canvas.hasPointerCapture(id)) canvas.releasePointerCapture(id);
    pointers.clear(); canvas.classList.remove('dragging'); cameraView=null;
  };
}

function renderLocalMap(ctx, width, height, mission, gridSpace, tiles) {
  const grid = mission.grid, canvas = ctx.canvas;
  const pose = mission.pose?.frame === grid.frame && point(xy(mission.pose)) ? mission.pose : null;
  const bounds = mission.bounds;
  const follow = pose ? [pose.x, pose.y] : bounds?.length === 4 && bounds.every(Number.isFinite)
    ? [(bounds[0]+bounds[2])/2, (bounds[1]+bounds[3])/2] : gridSpace.toWorld(grid.width/2, grid.height/2);
  const {center,scale,project,ca,sa,ct,st} = setCameraView(canvas,'local',follow,Math.min(width/6.4,height/3.9),p=>{
    const [u,v]=gridSpace.toGrid(p);
    return gridSpace.toWorld(clampCamera(u,0,grid.width),clampCamera(v,0,grid.height));
  });
  const radius = Math.min(8, 4.6/cameras.local.zoom);
  const [cx, cy] = center;
  const xyz = p => Array.isArray(p) && p.length===3 && p.every(Number.isFinite);
  const inView = p => Math.abs(p[0]-cx)<=radius && Math.abs(p[1]-cy)<=radius;
  function polygon(vertices, fill, stroke) {
    const path = new Path2D();
    vertices.forEach((p,i) => {const [x,y] = project(p); if(i) path.lineTo(x,y); else path.moveTo(x,y);});
    path.closePath();
    if (fill) {ctx.fillStyle=fill;ctx.fill(path);}
    if (stroke) {ctx.lineWidth=1;ctx.strokeStyle=stroke;ctx.stroke(path);}
    return path;
  }

  const clip = polygon([[cx-radius,cy-radius],[cx+radius,cy-radius],[cx+radius,cy+radius],[cx-radius,cy+radius]]);
  ctx.save(); ctx.clip(clip);
  const base = project(gridSpace.toWorld(0,0)), u = project(gridSpace.toWorld(1,0)), v = project(gridSpace.toWorld(0,1));
  ctx.save();
  ctx.transform(u[0]-base[0],u[1]-base[1],v[0]-base[0],v[1]-base[1],...base);
  ctx.imageSmoothingEnabled=false; ctx.globalAlpha=.54; ctx.drawImage(tiles,0,0); ctx.restore();
  for (let x=Math.ceil(cx-radius);x<=cx+radius;x++) pathLine(ctx,[[x,cy-radius],[x,cy+radius]],project,'#8eafa737',1);
  for (let y=Math.ceil(cy-radius);y<=cy+radius;y++) pathLine(ctx,[[cx-radius,y],[cx+radius,y]],project,'#8eafa737',1);
  ctx.restore();

  const scene = mission.scene_geometry;
  const sameMap = scene?.frame === grid.frame && scene.map_ref?.id === mission.map_ref?.id &&
    !!mission.map_ref?.revision && scene.map_ref?.revision === mission.map_ref.revision;
  let featureCount = 0;
  const faces = [];
  for (const box of (sameMap && Array.isArray(scene.features)) ? scene.features : []) {
    if (!point(box.xy) || !xyz(box.size) || !Number.isFinite(box.base_z)) continue;
    const [x,y]=box.xy, [dx,dy,dz]=box.size, z=box.base_z;
    const left=Math.max(cx-radius,x-dx/2),right=Math.min(cx+radius,x+dx/2);
    const bottom=Math.max(cy-radius,y-dy/2),top=Math.min(cy+radius,y+dy/2);
    if (right<=left || top<=bottom || z>3.2) continue;
    const corners = [[left,bottom],[right,bottom],[right,top],[left,top]];
    const low=corners.map(p=>[...p,z]), high=corners.map(p=>[...p,z+dz]);
    for (let i=0;i<4;i++) {
      const j=(i+1)%4;
      faces.push({vertices:[low[i],low[j],high[j],high[i]],fill:'#5c829732'});
    }
    faces.push({vertices:high,fill:'#789da64a'}); featureCount++;
  }
  const planeDepth = face => face.vertices.reduce((sum,[x,y,z])=>sum+(sa*x-ca*y)*ct+z*st,0)/face.vertices.length;
  faces.sort((a,b)=>planeDepth(a)-planeDepth(b)).forEach(face=>polygon(face.vertices,face.fill,'#7b9da754'));
  const observations = mission.local_observation;
  const registered = observations?.frame===grid.frame && Number.isFinite(observations.as_of_ns);
  let minDepthZ=Infinity, maxDepthZ=-Infinity;
  function dots(key) {
    const layer=registered && observations[key];
    if (!layer || !Number.isFinite(layer.stamp_ns) || !Number.isFinite(layer.pose_stamp_ns) ||
        observations.as_of_ns-layer.stamp_ns>650e6 || observations.as_of_ns<layer.stamp_ns ||
        Math.abs(layer.pose_stamp_ns-layer.stamp_ns)>60e6 || !Array.isArray(layer.points)) return 0;
    const laser=key==='laser';
    let count=0;
    for (const p of layer.points) {
      if (!xyz(p) || !inView(p)) continue;
      const [x,y]=project(p);
      if (x<0 || y<0 || x>width || y>height) continue;
      if (!laser) {minDepthZ=Math.min(minDepthZ,p[2]);maxDepthZ=Math.max(maxDepthZ,p[2]);}
      ctx.fillStyle=laser?'#ffac62':p[2]<.12?'#709fae':p[2]<.45?'#74d4c1':p[2]<1.1?'#c4df99':'#eed399';
      const size=laser?3.4:3;
      ctx.fillRect(x-size/2,y-size/2,size,size);
      count++;
    }
    return count;
  }
  const depth=dots('depth'), scan=dots('laser');
  ctx.save();ctx.clip(clip);
  paintReferenceLine(ctx,mission,project);
  const trail=Array.isArray(mission.trajectory) ? mission.trajectory : drawTrail(mission);
  pathLine(ctx,trail,project,'#69d4e3',3,[5,3]);
  ctx.restore();
  const target=mission.goal || mission.frontier;
  if(point(target) && inView(target)) marker(ctx,target,project,{color:'#d9edbd',dashed:true,radius:7,label:mission.goal?'目标停靠位':'观察点'});
  const look = mission.frontier;
  if (mission.goal && point(look) && inView(look) && Math.hypot(look[0]-mission.goal[0],look[1]-mission.goal[1])>.12) {
    marker(ctx,look,project,{color:'#e7c67d',radius:4,label:'当前前视点'});
  }
  if (pose && Number.isFinite(pose.yaw)) {
    const [x,y]=project([pose.x,pose.y,.03]);
    const [fx,fy]=project([pose.x+Math.cos(pose.yaw),pose.y+Math.sin(pose.yaw),.03]);
    ctx.save();ctx.translate(x,y);ctx.rotate(Math.atan2(fy-y,fx-x));
    ctx.beginPath();ctx.moveTo(13,0);ctx.lineTo(-8,8);ctx.lineTo(-4,0);ctx.lineTo(-8,-8);ctx.closePath();
    ctx.fillStyle='#a7e8f3';ctx.fill();ctx.strokeStyle='#153a43';ctx.lineWidth=2;ctx.stroke();ctx.restore();
  }
  const bar=scale*(scale>130?.5:1);
  pathLine(ctx,[[16,height-17],[16+bar,height-17]],p=>p,'#ceddd3',2);
  ctx.font='11px system-ui';ctx.fillStyle='#ceddd3';ctx.fillText(scale>130?'0.5 m':'1 m',20+bar,height-13);
  Object.assign(canvas.dataset,{depthPoints:String(depth),scanPoints:String(scan),sceneFeatures:String(featureCount),
    observationStamp:registered?String(observations.as_of_ns):''});
  const phase = phases[String(mission.phase||'').toLowerCase()] || '查看任务地图';
  show('local-phase',Number.isFinite(pose?.speed) ? `${phase} · ${pose.speed.toFixed(2)} m/s` : phase);
  const counts=[depth?`深度 ${depth} 点 · 高度 ${minDepthZ.toFixed(2)} 至 ${maxDepthZ.toFixed(2)} m`:'无已配准深度点',
    scan?`平面激光 ${scan} 点`:'无已配准激光点'];
  if (registered) counts.push(`仿真 ${(observations.as_of_ns/1e9).toFixed(1)} s`);
  if (!connected) counts.unshift('连接中断 · 最近快照');
  else if (['SUCCEEDED','BLOCKED','CANCELED','FAILED'].includes(mission.status)) counts.unshift('任务存档');
  show('local-evidence',mission.kind==='map_import'?'已登记校园底图':counts.join(' · '));
}

function paintSpeedTrace(samples) {
  const rows = (Array.isArray(samples) ? samples : []).filter(p => point(p));
  const strip = document.getElementById('speed-strip');
  strip.hidden = rows.length<2;
  if (strip.hidden) return;
  const last = rows.at(-1)[0], start = Math.max(last-15e9, rows[0][0]);
  const span = last-start;
  if (!(span>0)) {strip.hidden=true;return;}
  const canvas = document.getElementById('speed-trace');
  const w=canvas.clientWidth,h=canvas.clientHeight,dpr=window.devicePixelRatio||1;
  canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr);
  const ctx=canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0);
  const visible=rows.filter(p=>p[0]>=start);
  const maximum=Math.max(.3,...visible.map(p=>p[1])), minimum=Math.min(0,...visible.map(p=>p[1]));
  // Keep a visible zero line and discontinuities for gaps in acquisition.
  const toScreen=([t,v])=>[31+(t-start)/span*(w-34), 5+(h-10)*(maximum-v)/(maximum-minimum)];
  const zeroY=toScreen([start,0])[1];
  pathLine(ctx,[[30,zeroY],[w-2,zeroY]],p=>p,'#547064',1);
  ctx.fillStyle='#9eafa4';ctx.font='10px system-ui';
  ctx.fillText(maximum.toFixed(2),1,9);ctx.fillText(minimum.toFixed(2),1,h-2);
  let part=[];
  for(const p of visible) {
    if (part.length && p[0]-part.at(-1)[0]>1.5e9) {pathLine(ctx,part,toScreen,'#70d2d9',2);part=[];}
    part.push(p);
  }
  pathLine(ctx,part,toScreen,'#70d2d9',2);
  canvas.dataset.samples=String(visible.length);
  show('speed-window',`近 ${(span/1e9).toFixed(1)} s · m/s`);
}
