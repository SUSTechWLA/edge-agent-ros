/* Render in metres in the published map frame; sensor rays are first transformed
   from the calibrated laser mount and the closest timestamped body pose. */
window.EdgeMap = (() => {
  const byId = id => document.getElementById(id);
  const finite = Number.isFinite;
  let state = null, scaleFactor = 1, panX = 0, panY = 0, drag = null;
  let mapPixels = null, pixelGrid = null;
  const layers = {grid:true, scan:true, trail:true, truth:false};

  function surface(canvas) {
    const w = canvas.clientWidth, h = canvas.clientHeight;
    if (!w || !h) return null;
    const dpr = Math.min(devicePixelRatio || 1, 2);
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
    const frame=grid?.frame||pose?.frame||odom?.frame;
    const aligned=state?.sim_world_aligned===true && state?.environment==='simulation';
    byId('truth-label').hidden=!aligned; byId('map-frame').textContent=frame ? frame+' · m':'—';
    const gOK=grid && grid.width>0 && grid.height>0 && grid.resolution>0 && grid.frame===frame;
    const points=[];
    if(gOK) for(const [x,y] of [[0,0],[grid.width,0],[0,grid.height],[grid.width,grid.height]]){
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
    if(gOK && layers.grid){
      const img=gridImage(grid);
      if(img){
        const [x,y]=screen(grid.origin_x,grid.origin_y);
        ctx.save();ctx.translate(x,y);ctx.scale(pp,-pp);ctx.rotate(grid.origin_yaw);ctx.imageSmoothingEnabled=false;
        ctx.drawImage(img,0,0,grid.width*grid.resolution,grid.height*grid.resolution);ctx.restore();
      }
    }
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
    byId('map-empty').hidden=!!(robot||gOK);byId('map-notes').textContent=notes.join('　·　');
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
  map.addEventListener('pointerdown',e=>{if(e.button!==0)return;drag=[e.clientX,e.clientY,panX,panY];map.setPointerCapture(e.pointerId);});
  map.addEventListener('pointermove',e=>{if(!drag)return;panX=drag[2]+e.clientX-drag[0];panY=drag[3]+e.clientY-drag[1];draw();});
  map.addEventListener('lostpointercapture',()=>{drag=null;});
  byId('zoom-in').onclick=()=>{scaleFactor=Math.min(12,scaleFactor*1.4);draw();};
  byId('zoom-out').onclick=()=>{scaleFactor=Math.max(.35,scaleFactor/1.4);draw();};
  byId('fit').onclick=()=>{scaleFactor=1;panX=panY=0;draw();};
  document.querySelectorAll('[data-layer]').forEach(input=>{input.onchange=()=>{layers[input.dataset.layer]=input.checked;draw();};});
  new ResizeObserver(()=>{draw();polar(state?.sources?.scan?.data);}).observe(byId('map-stage'));
  return {update(next){state=next;draw();},polar};
})();
