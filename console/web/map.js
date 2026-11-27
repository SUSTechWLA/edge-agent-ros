/* Render in metres in the published map frame; sensor rays are first transformed
   from the calibrated laser mount and the closest timestamped body pose. */
(function initEdgeMap(root) {
  const byId = id => typeof document !== 'undefined' ? document.getElementById(id) : null;
  const finite = Number.isFinite;
  let state = null, scaleFactor = 1, panX = 0, panY = 0, drag = null;
  let mapPixels = null, pixelGrid = null;
  let clearanceCache = null, clearanceGrid = null;
  let lastView = null;
  const layers = {semantics:true, grid:true, clearance:false, scan:true, trail:true, truth:false};

  function computeClearanceMeters(grid) {
    const {width, height, cells, resolution} = grid || {};
    if (!width || !height || !finite(resolution) || resolution <= 0 || cells?.length !== width * height || width * height > 2097152) {
      return null;
    }
    const stride = width + 2;
    const cap = Math.min(Math.max(width, height) + 1, Math.ceil(2.4 / resolution) + 2);
    const field = new Int32Array(stride * (height + 2)).fill(cap);
    let hasObstacle = false;
    for (let i = 0; i < cells.length; i++) {
      if (cells[i] > 49) {
        field[(Math.floor(i / width) + 1) * stride + (i % width) + 1] = 0;
        hasObstacle = true;
      }
    }
    if (!hasObstacle) return new Float32Array(cells.length).fill(cap * resolution);
    for (let y = 1; y <= height; y++) {
      for (let i = y * stride + 1; i < (y + 1) * stride - 1; i++) {
        const best = Math.min(field[i - 1], field[i - stride - 1], field[i - stride], field[i - stride + 1]);
        if (best + 1 < field[i]) field[i] = best + 1;
      }
    }
    for (let y = height; y >= 1; y--) {
      for (let i = (y + 1) * stride - 2; i > y * stride; i--) {
        const best = Math.min(field[i + 1], field[i + stride - 1], field[i + stride], field[i + stride + 1]);
        if (best + 1 < field[i]) field[i] = best + 1;
      }
    }
    const distances = new Float32Array(cells.length);
    for (let i = 0; i < cells.length; i++) {
      const steps = field[(Math.floor(i / width) + 1) * stride + (i % width) + 1];
      distances[i] = Math.max(0, steps - 1) * resolution;
    }
    return distances;
  }

  function clearanceData(grid) {
    if (clearanceGrid === grid && clearanceCache) return clearanceCache;
    const distances = computeClearanceMeters(grid);
    if (!distances) return null;
    let img = null;
    if (typeof document !== 'undefined') {
      img = document.createElement('canvas');
      img.width = grid.width; img.height = grid.height;
      const ctx = img.getContext('2d'), out = ctx.createImageData(grid.width, grid.height);
      const warnRadius = 0.85;
      for (let i = 0; i < grid.cells.length; i++) {
        const val = grid.cells[i];
        if (val < 0 || val > 49) continue;
        const d = distances[i];
        if (d >= warnRadius) continue;
        const t = Math.max(0, Math.min(1, 1 - d / warnRadius));
        const p = i * 4;
        out.data[p] = Math.round(70 + 175 * t);
        out.data[p + 1] = Math.round(190 - 92 * t);
        out.data[p + 2] = Math.round(135 - 88 * t);
        out.data[p + 3] = Math.round(45 + 145 * t);
      }
      ctx.putImageData(out, 0, 0);
    }
    clearanceGrid = grid;
    clearanceCache = {distances, img};
    return clearanceCache;
  }

  function inspectWorldPoint(wx, wy, snapshot) {
    if (!finite(wx) || !finite(wy)) return null;
    const src = snapshot?.sources || {};
    const grid = src.map?.data;
    const sem = src.semantics?.data;
    const frame = grid?.frame || src.pose?.data?.frame || src.odom?.data?.frame || sem?.frame || '—';
    const parts = [`(${wx.toFixed(2)}, ${wy.toFixed(2)}) m`];
    let cellInfo = null;
    if (grid && grid.width > 0 && grid.height > 0 && grid.resolution > 0 && grid.frame === frame &&
        Array.isArray(grid.cells) && grid.cells.length === grid.width * grid.height) {
      const dx = wx - grid.origin_x, dy = wy - grid.origin_y;
      const c = Math.cos(grid.origin_yaw || 0), s = Math.sin(grid.origin_yaw || 0);
      const u = Math.floor((c * dx + s * dy) / grid.resolution);
      const v = Math.floor((-s * dx + c * dy) / grid.resolution);
      if (u >= 0 && u < grid.width && v >= 0 && v < grid.height) {
        const idx = v * grid.width + u;
        const occ = grid.cells[idx];
        const stateLabel = occ < 0 ? '未知' : occ > 49 ? '障碍占据' : '自由通行';
        const cd = clearanceData(grid);
        const clearanceM = cd ? cd.distances[idx] : null;
        parts.push(`栅格 (${u}, ${v})`, `${stateLabel} (${occ})`);
        if (finite(clearanceM) && occ >= 0 && occ <= 49) {
          parts.push(`障碍净距 ${clearanceM.toFixed(2)} m`);
        }
        cellInfo = {u, v, occ, clearanceM};
      } else {
        parts.push('栅格范围外');
      }
    }
    if (sem && sem.frame === frame && Array.isArray(sem.zones)) {
      const zone = sem.zones.find(z => Array.isArray(z?.bounds) && z.bounds.length === 4 &&
        wx >= z.bounds[0] && wy >= z.bounds[1] && wx <= z.bounds[2] && wy <= z.bounds[3]);
      if (zone?.label) parts.push(`语义区 · ${zone.label}`);
    }
    return {wx, wy, frame, cell: cellInfo, text: parts.join(' · ')};
  }

  function surface(canvas) {
    if (!canvas) return null;
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return null;
    const dpr = Math.min((typeof window !== 'undefined' && window.devicePixelRatio) || 1, 2);
    if (canvas.width !== Math.round(w*dpr) || canvas.height !== Math.round(h*dpr)) {
      canvas.width = Math.round(w*dpr); canvas.height = Math.round(h*dpr);
    }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,w,h);
    return {ctx,w,h};
  }
  function gridImage(grid) {
    if (pixelGrid === grid) return mapPixels;
    const {width,height,cells} = grid;
    if (!width || !height || cells?.length !== width*height || width*height > 2097152) return null;
    const img = document.createElement('canvas'); img.width=width; img.height=height;
    const ctx = img.getContext('2d'), out=ctx.createImageData(width,height);
    for(let i=0;i<cells.length;i++){
      const value=cells[i]; if(value<0) continue;
      const p=i*4, occupied=Math.min(1,value/100);
      out.data[p]=35+occupied*107; out.data[p+1]=73+occupied*106; out.data[p+2]=80+occupied*91; out.data[p+3]=255;
    }
    ctx.putImageData(out,0,0); pixelGrid=grid; mapPixels=img; return img;
  }
  function rotate(x,y,a){return [x*Math.cos(a)-y*Math.sin(a),x*Math.sin(a)+y*Math.cos(a)];}
  function drawScene(ctx,scene,screen,pp){
    const colors={indoor:['#749cde32','#b4cdec'],outdoor:['#82b8663d','#b9df97'],transition:['#dabe642e','#f6df92']};
    const pair=p=>Array.isArray(p)&&p.length===2&&p.every(finite);
    const labels=[];
    const caption=(label,x,y,color,font)=>{
      ctx.font=font;ctx.lineWidth=3;ctx.strokeStyle='#213537';ctx.lineJoin='round';
      const width=ctx.measureText(label).width+4;
      for(const dy of [0,16,-16,32]){
        const baseline=y+dy;
        if(labels.some(box=>x<box.x+box.w && x+width>box.x && baseline-12<box.y+15 && baseline+3>box.y))continue;
        labels.push({x,y:baseline-12,w:width});
        ctx.strokeText(label,x,baseline);ctx.fillStyle=color;ctx.fillText(label,x,baseline);
        break;
      }
    };
    ctx.save();
    for(const z of scene.zones){
      if(!Array.isArray(z?.bounds)||z.bounds.length!==4||!z.bounds.every(finite))continue;
      const [x0,y0,x1,y1]=z.bounds, [x,y]=screen(x0,y1), width=(x1-x0)*pp,height=(y1-y0)*pp;
      const [fill,line]=colors[z.kind]||colors.indoor;
      ctx.fillStyle=fill;ctx.fillRect(x,y,width,height);ctx.strokeStyle=line;ctx.lineWidth=1;ctx.strokeRect(x,y,width,height);
      if(width>50)caption(z.label,x+5,y+16,line,'600 12px system-ui');
    }
    for(const f of scene.features){
      if(!pair(f?.xy)||f.kind==='roof'||!Array.isArray(f.size)||!f.size.slice(0,2).every(finite))continue;
      const [x,y]=screen(...f.xy),[w,h]=f.size;
      ctx.fillStyle=f.kind==='wall'?'#99b3bc99':f.kind==='barrier'?'#dbab6c':'#84ae90';
      ctx.fillRect(x-w*pp/2,y-h*pp/2,w*pp,h*pp);
      if(f.kind!=='wall'&&pp>32)caption(f.label,x-w*pp/2,y-h*pp/2-3,'#eef6ee','11px system-ui');
    }
    const nodes=new Map(scene.nodes.filter(p=>pair(p?.xy)).map(p=>[p.id,p]));
    ctx.setLineDash([3,5]);ctx.strokeStyle='#cbdfaeaa';
    for(const e of scene.edges){
      if(!Array.isArray(e))continue;
      const a=nodes.get(e[0]),b=nodes.get(e[1]);if(!a||!b)continue;
      ctx.beginPath();ctx.moveTo(...screen(...a.xy));ctx.lineTo(...screen(...b.xy));ctx.stroke();
    }
    ctx.setLineDash([]);
    for(const n of nodes.values()){
      const [x,y]=screen(...n.xy);ctx.fillStyle=n.accessible===false?'#f8b792':'#d4eaba';
      ctx.beginPath();ctx.arc(x,y,3,0,2*Math.PI);ctx.fill();
      if(pp>55||!['door_in','door_out'].includes(n.id))caption(n.label,x+5,y+14,'#e7f0ce','11px system-ui');
    }
    ctx.restore();
  }
  function matchedLaser(scan, pose, sources, frame){
    if(!scan || !pose || !scan.mount || scan.mounted_frame !== pose.child_frame ||
        pose.frame !== frame || !finite(sources.scan.stamp_s)) return null;
    const stamp=sources.scan.stamp_s;
    let best=pose, delta=Math.abs(stamp-sources.pose.stamp_s);
    if(state.trail_frame===frame) for(const sample of state.trail || []){
      const age=Math.abs(sample.stamp_s-stamp);
      if(age<delta && finite(sample.yaw)){best=sample;delta=age;}
    }
    if(delta>0.25) return null;
    return best;
  }
  function draw(){
    const setup=surface(byId('map')); if(!setup)return;
    const {ctx,w,h}=setup, src=state?.sources||{};
    const grid=src.map?.data, pose=src.pose?.data, odom=src.odom?.data, truth=src.truth?.data, scan=src.scan?.data;
    const sem=src.semantics?.data;
    const frame=grid?.frame||pose?.frame||odom?.frame||sem?.frame;
    const hasSem=!!sem && sem.frame===frame && Array.isArray(sem.zones)&&Array.isArray(sem.features)&&Array.isArray(sem.nodes)&&Array.isArray(sem.edges);
    const aligned=state?.sim_world_aligned===true && state?.environment==='simulation';
    byId('truth-label').hidden=!aligned; byId('map-frame').textContent=frame ? frame+' · m':'—';
    const gOK=grid && grid.width>0 && grid.height>0 && grid.resolution>0 && grid.frame===frame;
    const points=[];
    if(hasSem)for(const z of sem.zones){
      if(Array.isArray(z?.bounds) && z.bounds.length===4 && z.bounds.every(finite)){
        points.push([z.bounds[0]-.55,z.bounds[1]-.55],[z.bounds[2]+.55,z.bounds[3]+.55]);
      }
    }
    // Fit authored landmarks on initial load; zoom out to inspect outlying raw cells.
    if(gOK && !points.length) for(const [x,y] of [[0,0],[grid.width,0],[0,grid.height],[grid.width,grid.height]]){
      const v=rotate(x*grid.resolution,y*grid.resolution,grid.origin_yaw);
      points.push([grid.origin_x+v[0],grid.origin_y+v[1]]);
    }
    const robot=(pose?.frame===frame?pose:null)||(odom?.frame===frame?odom:null);
    if(robot) points.push([robot.x,robot.y]);
    if(!gOK && state?.trail_frame===frame) for(const row of state?.trail||[])points.push([row.x,row.y]);
    const xBounds=points.map(p=>p[0]),yBounds=points.map(p=>p[1]);
    const left=points.length?Math.min(...xBounds):-3,right=points.length?Math.max(...xBounds):3;
    const bottom=points.length?Math.min(...yBounds):-3,top=points.length?Math.max(...yBounds):3;
    const pp=Math.min((w-35)/Math.max(4.5,right-left),(h-32)/Math.max(4.5,top-bottom))*scaleFactor;
    const ox=w/2-(left+right)*pp/2+panX, oy=h/2+(bottom+top)*pp/2+panY;
    const screen=(x,y)=>[ox+x*pp,oy-y*pp];
    lastView={ox, oy, pp, frame};
    byId('semantics-label').hidden=!hasSem;
    byId('scene-info').hidden=!hasSem;
    if(hasSem){
      byId('scene-zone').textContent='所在区域 · '+(sem.zones.find(z=>z?.id===sem.current_zone)?.label||'待定位');
      byId('scene-source').textContent='场景先验 · '+(sem.source||'语义地图')+' v'+sem.revision+(src.semantics.fresh?'':' · 上次观测');
    }
    if(gOK && layers.grid){
      const img=gridImage(grid);
      if(img){
        const [x,y]=screen(grid.origin_x,grid.origin_y);
        ctx.save();ctx.translate(x,y);ctx.scale(pp,-pp);ctx.rotate(grid.origin_yaw);ctx.imageSmoothingEnabled=false;
        ctx.drawImage(img,0,0,grid.width*grid.resolution,grid.height*grid.resolution);ctx.restore();
      }
    }
    if(gOK && layers.clearance){
      const cdata=clearanceData(grid);
      if(cdata?.img){
        const [x,y]=screen(grid.origin_x,grid.origin_y);
        ctx.save();ctx.translate(x,y);ctx.scale(pp,-pp);ctx.rotate(grid.origin_yaw);ctx.imageSmoothingEnabled=false;
        ctx.drawImage(cdata.img,0,0,grid.width*grid.resolution,grid.height*grid.resolution);ctx.restore();
      }
    }
    if(hasSem&&layers.semantics)drawScene(ctx,sem,screen,pp);
    const step=[.1,.2,.5,1,2,5,10,20,50,100].find(n=>n*pp>=42)||100;
    ctx.font='10px Consolas, monospace';ctx.lineWidth=1;
    const gridLine=(a,b,c,d,axis)=>{ctx.strokeStyle=axis?'#83b2a878':'#82a5a321';ctx.beginPath();ctx.moveTo(a,b);ctx.lineTo(c,d);ctx.stroke();};
    for(let i=Math.ceil(-ox/pp/step);i<=(w-ox)/pp/step;i++){
      const x=ox+i*step*pp;gridLine(x,0,x,h,i===0);ctx.fillStyle='#a2bdb5';ctx.fillText(+(i*step).toFixed(2),x+4,h-8);
    }
    for(let i=Math.ceil((oy-h)/pp/step);i<=oy/pp/step;i++){
      const y=oy-i*step*pp;gridLine(0,y,w,y,i===0);ctx.fillStyle='#a2bdb5';ctx.fillText(+(i*step).toFixed(2),5,y-3);
    }
    byId('scale-line').style.width=Math.min(160,step*pp)+'px';byId('scale-text').textContent=step+' m';
    if(state?.trail_frame===frame && layers.trail && state?.trail?.length){
      ctx.strokeStyle='#c2ea7f';ctx.lineWidth=2;ctx.beginPath();
      state.trail.forEach((r,i)=>{const [x,y]=screen(r.x,r.y);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y);});ctx.stroke();
    }
    const notes=[];
    if(layers.scan && scan){
      const base=matchedLaser(scan,pose,src,frame);
      if(base){
        const [dx,dy]=rotate(scan.mount.x,scan.mount.y,base.yaw);
        ctx.fillStyle='#64eddb';
        scan.ranges.forEach((r,i)=>{if(!finite(r)||r<scan.range_min||r>=scan.range_max)return;
          const a=base.yaw+scan.mount.yaw+scan.angle_min+scan.angle_increment*i;
          const [x,y]=screen(base.x+dx+r*Math.cos(a),base.y+dy+r*Math.sin(a));
          ctx.fillRect(x-1.4,y-1.4,2.8,2.8);
        });
      } else notes.push('激光叠图等待时间对齐的位姿与标定');
    }
    if(robot){
      const [x,y]=screen(robot.x,robot.y);
      ctx.save();ctx.translate(x,y);ctx.rotate(-robot.yaw);
      ctx.beginPath();ctx.arc(0,0,15,0,2*Math.PI);ctx.fillStyle='#c9ef8c28';ctx.fill();
      ctx.beginPath();ctx.moveTo(12,0);ctx.lineTo(-7,-7);ctx.lineTo(-4,0);ctx.lineTo(-7,7);ctx.closePath();
      ctx.fillStyle='#dcff9b';ctx.fill();ctx.strokeStyle='#142d2f';ctx.lineWidth=1.5;ctx.stroke();ctx.restore();
      ctx.font='bold 11px Consolas, monospace';ctx.fillStyle='#edf4e2';ctx.fillText(state.robot_id||'Edge',x+17,y-10);
      if(robot===odom && !pose)notes.push('机器人标记来自轮式里程计');
    }
    if(aligned && truth && layers.truth && (truth.frame===frame || (truth.frame==='world' && frame==='odom'))){
      const [x,y]=screen(truth.x,truth.y);ctx.save();ctx.translate(x,y);ctx.rotate(Math.PI/4);ctx.strokeStyle='#ffbe78';ctx.lineWidth=2;ctx.strokeRect(-5,-5,10,10);ctx.restore();
    }
    if(gOK)notes.unshift(grid.width+' × '+grid.height+' 栅格 · '+Number(grid.resolution.toFixed(3))+' m/格');
    if((gOK && !src.map.fresh) || (pose && !src.pose.fresh))notes.push('含上次观测，数据已过期');
    byId('map-empty').hidden=!!(robot||gOK||(hasSem&&layers.semantics));byId('map-notes').textContent=notes.join('　·　');
  }
  function polar(scan) {
    const setup=surface(byId('polar'));if(!setup)return;
    const {ctx,w,h}=setup, radius=Math.min(h*.43,w*.43),cx=w/2,cy=h/2;
    ctx.strokeStyle='#cddbd0';ctx.lineWidth=1;
    for(const k of [.33,.66,1]){ctx.beginPath();ctx.arc(cx,cy,radius*k,0,2*Math.PI);ctx.stroke();}
    ctx.beginPath();ctx.moveTo(cx-radius,cy);ctx.lineTo(cx+radius,cy);ctx.moveTo(cx,cy-radius);ctx.lineTo(cx,cy+radius);ctx.stroke();
    if(!scan || !scan.ranges?.length)return;
    const max=scan.range_max||1;
    ctx.fillStyle='#007e72';for(let i=0;i<scan.ranges.length;i++){
      const r=scan.ranges[i];if(!finite(r)||r>=max||r<scan.range_min)continue;
      const a=scan.angle_min+i*scan.angle_increment, rr=radius*r/max;
      ctx.fillRect(cx-Math.sin(a)*rr-1,cy-Math.cos(a)*rr-1,2.3,2.3);
    }
    ctx.fillStyle='#204b47';ctx.beginPath();ctx.moveTo(cx,cy-5);ctx.lineTo(cx+3,cy+3);ctx.lineTo(cx-3,cy+3);ctx.fill();
    ctx.font='10px Consolas, monospace';ctx.fillText(max+' m',cx+radius+5,cy+4);
  }
  const map=byId('map');
  if(map){
    const updateInspect=e=>{
      const out=byId('map-inspect');
      if(!out || !lastView || !lastView.pp)return;
      const rect=map.getBoundingClientRect();
      const wx=(e.clientX-rect.left-lastView.ox)/lastView.pp;
      const wy=(lastView.oy-(e.clientY-rect.top))/lastView.pp;
      const info=inspectWorldPoint(wx,wy,state);
      if(info)out.textContent=info.text;
    };
    map.addEventListener('pointerdown',e=>{if(e.button!==0)return;drag=[e.clientX,e.clientY,panX,panY];map.setPointerCapture(e.pointerId);});
    map.addEventListener('pointermove',e=>{
      if(drag){panX=drag[2]+e.clientX-drag[0];panY=drag[3]+e.clientY-drag[1];draw();return;}
      updateInspect(e);
    });
    map.addEventListener('click',updateInspect);
    map.addEventListener('lostpointercapture',()=>{drag=null;});
    byId('zoom-in').onclick=()=>{scaleFactor=Math.min(12,scaleFactor*1.4);draw();};
    byId('zoom-out').onclick=()=>{scaleFactor=Math.max(.35,scaleFactor/1.4);draw();};
    byId('fit').onclick=()=>{scaleFactor=1;panX=panY=0;draw();};
    document.querySelectorAll('[data-layer]').forEach(input=>{input.onchange=()=>{layers[input.dataset.layer]=input.checked;draw();};});
    if(typeof ResizeObserver !== 'undefined'){
      new ResizeObserver(()=>{draw();polar(state?.sources?.scan?.data);}).observe(byId('map-stage'));
    }
  }
  const exported = {update(next){state=next;draw();},polar,computeClearanceMeters,inspectWorldPoint};
  if (root) root.EdgeMap = exported;
  if (typeof module !== 'undefined' && module.exports) module.exports = exported;
})(typeof window !== 'undefined' ? window : globalThis);
