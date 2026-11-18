(() => {
  "use strict";
  const el = id => document.getElementById(id);
  const canvas = el("viewport"), ctx = canvas.getContext("2d");
  const checked = name => el("layer-"+name).checked;
  const statuses = {waiting:"等待", stale:"已过期", online:"新鲜", error:"输入异常"};
  let data, connected=false, yaw=-.69, tilt=.67, zoom=1, pan=[0,0], drag, scheduled=false;
  const mutedSources=new Set();
  let sourceSignature=null;

  function render() {
    scheduled=false;
    const w=canvas.clientWidth,h=canvas.clientHeight,dpr=Math.min(2,devicePixelRatio||1);
    if (!w || !h) return;
    if (canvas.width!==Math.round(w*dpr)||canvas.height!==Math.round(h*dpr)) {
      canvas.width=Math.round(w*dpr); canvas.height=Math.round(h*dpr);
    }
    ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,w,h);
    const scene=data?.scene, cloud=data?.cloud, pose=data?.pose;
    const clouds=[cloud,...Object.entries(data?.cloud_sources||{}).filter(([id])=>!mutedSources.has(id)).map(([,value])=>value)].filter(Boolean);
    const points=clouds.flatMap(source=>source.points), boxes=scene?.features||[], zones=scene?.zones||[];
    const xyz=pose?.xyz;
    let x0=Infinity,x1=-Infinity,y0=Infinity,y1=-Infinity,z0=Infinity,z1=-Infinity;
    const bound=(x,y,z)=>{x0=Math.min(x0,x);x1=Math.max(x1,x);y0=Math.min(y0,y);y1=Math.max(y1,y);z0=Math.min(z0,z);z1=Math.max(z1,z)};
    for(const z of zones){const [a,b,c,d]=z.bounds;bound(a,b,0);bound(c,d,0)}
    for(const p of points) bound(...p);
    for(const b of boxes){const [x,y,z]=b.xyz,[dx,dy,dz]=b.size;bound(x-dx/2,y-dy/2,z);bound(x+dx/2,y+dy/2,z+dz)}
    if(xyz)bound(...xyz);
    if(!Number.isFinite(x0)){x0=y0=-3;x1=y1=3;z0=z1=0}
    const cx=(x0+x1)/2,cy=(y0+y1)/2,cz=(z0+z1)/2;
    const a=Math.cos(yaw),b=Math.sin(yaw),c=Math.cos(tilt),s=Math.sin(tilt);
    const extentX=Math.abs(a)*(x1-x0)+Math.abs(b)*(y1-y0);
    const extentY=(Math.abs(b)*(x1-x0)+Math.abs(a)*(y1-y0))*s+(z1-z0)*c;
    const k=Math.min(w/Math.max(extentX+1,4),h/Math.max(extentY+1,4))*.88*zoom;
    const proj=([x,y,z])=>[w/2+pan[0]+((x-cx)*a-(y-cy)*b)*k,
      h/2+pan[1]+(-(x-cx)*b*s-(y-cy)*a*s-(z-cz)*c)*k];
    function path(vertices,stroke,fill) {
      ctx.beginPath();vertices.forEach((v,i)=>{const [x,y]=proj(v);if(i)ctx.lineTo(x,y);else ctx.moveTo(x,y)});
      if (vertices.length>2)ctx.closePath();
      if(fill){ctx.fillStyle=fill;ctx.fill()} if(stroke){ctx.strokeStyle=stroke;ctx.stroke()}
    }
    if(checked("scene"))for(const zone of zones){
      const [x,y,u,v]=zone.bounds;
      path([[x,y,0],[u,y,0],[u,v,0],[x,v,0]],"#6a949355",zone.kind==="outdoor"?"#65938935":"#718aa332");
    }
    const step=Math.max(1,10**Math.floor(Math.log10(Math.max(x1-x0,y1-y0,1)/5)));
    const edge=[Math.floor(x0/step)*step,Math.ceil(x1/step)*step,Math.floor(y0/step)*step,Math.ceil(y1/step)*step];
    ctx.lineWidth=1;
    for(let i=edge[0];i<=edge[1];i+=step)path([[i,edge[2],0],[i,edge[3],0]],"#72949a42");
    for(let i=edge[2];i<=edge[3];i+=step)path([[edge[0],i,0],[edge[1],i,0]],"#72949a42");
    if(checked("scene")){
      const faces=[];
      for(const box of boxes){
        if(box.kind==="roof" && !checked("roof"))continue;
        const [x,y,z]=box.xyz,[dx,dy,dz]=box.size;
        const base=[[-1,-1],[1,-1],[1,1],[-1,1]].map(([u,v])=>[x+u*dx/2,y+v*dy/2,z]);
        const top=base.map(([px,py])=>[px,py,z+dz]);
        const polygons=[top,...base.map((p,i)=>[p,base[(i+1)%4],top[(i+1)%4],top[i]])];
        for(const vertices of polygons){
          const depth=vertices.reduce((d,[px,py,pz])=>d+(-px*b-py*a)*c+pz*s,0)/4;
          faces.push({vertices,depth,fill:box.kind==="landmark"?"#78a78d35":"#8ea6b32e"});
        }
      }
      for(const face of faces.sort((p,q)=>p.depth-q.depth))path(face.vertices,"#a4bdc060",face.fill);
    }
    let lo=Infinity,hi=-Infinity;
    for(const p of points){lo=Math.min(lo,p[2]);hi=Math.max(hi,p[2])}
    if(checked("cloud")) {
      for(const source of clouds) for(const p of source.points){
        const live=connected && source.status==="online";
        const t=(p[2]-lo)/Math.max(hi-lo,.01);
        ctx.fillStyle=live?`hsl(${205-157*t} 76% ${68+t*4}%)`:"#91a4a6";
        const [px,py]=proj(p);ctx.fillRect(px-1.25,py-1.25,2.5,2.5);
      }
    }
    if(checked("pose") && xyz){
      const [px,py]=proj(xyz);
      ctx.fillStyle=connected&&pose.status==="online"?"#ffcd68":"#ae9e83";
      ctx.beginPath();ctx.arc(px,py,6,0,2*Math.PI);ctx.fill();ctx.strokeStyle="#253630";ctx.stroke();
      ctx.font="600 13px sans-serif";ctx.fillText("机器人",px+9,py-8);
    }
    el("empty").hidden=!!((checked("cloud") && points.length)||(checked("scene") && (boxes.length||zones.length))||(checked("pose") && xyz));
    el("z-min").textContent=isFinite(lo)?lo.toFixed(2):"—";el("z-max").textContent=isFinite(hi)?hi.toFixed(2):"—";
  }
  function draw(){if(!scheduled){scheduled=true;requestAnimationFrame(render)}}
  function update(){
    const extras=Object.entries(data.cloud_sources||{});
    const picker=el("inspect-source");
    const signature=JSON.stringify(extras.map(([id,v])=>[id,v.name,v.status,v.error]));
    if(sourceSignature!==signature){
      sourceSignature=signature;
      const selected=picker.value;
      picker.replaceChildren(new Option("主点云",""));
      const list=el("lidar-sources");list.replaceChildren();
      for(const [id,v] of extras){
        picker.add(new Option(v.name,id));
        const label=document.createElement("label"),check=document.createElement("input");
        check.type="checkbox";check.checked=!mutedSources.has(id);label.className="sub";
        check.onchange=()=>{if(check.checked)mutedSources.delete(id);else mutedSources.add(id);draw()};
        label.append(check,document.createTextNode(v.name+" · "+(statuses[v.status]||v.status)));
        list.append(label);
      }
      el("inspect-source-row").hidden=!extras.length;
      picker.value=selected && data.cloud_sources?.[selected]?selected:
          (!data.cloud.points.length && extras.length?extras[0][0]:"");
    }
    const c=data.cloud_sources?.[picker.value] || data.cloud,s=data.scene,p=data.pose;
    el("env").textContent=data.environment==="simulation"?"GAZEBO  /  ROS 2":"ROS 2  /  机器人";
    el("frame").textContent=data.frame+" · m";
    el("point-count").textContent=c.points.length.toLocaleString();
    const ok=connected && c.status==="online";
    el("cloud-status").dataset.ok=String(ok);
    el("cloud-status").textContent=connected?(c.error==="no_valid_returns"&&c.status==="error"?"本帧无有效回波":statuses[c.status]||c.status):"快照";
    el("sensor").textContent=data.environment==="simulation" && c.source_frame==="map3d_survey"?"固定仿真台站 · map3d_survey":c.source_frame||"等待点云";
    el("topic").textContent=c.topic;el("sample").textContent=c.stamp_s?.toFixed(3)??"—";
    el("resolution").textContent=(c.voxel_m*100).toFixed(0)+" cm";
    el("sources").textContent="先验 "+(statuses[s.status]||"—")+(s.source?" · "+s.source:"")+"。融合位姿 "+(statuses[p.status]||"—")+"。"+(c.error?"点云诊断 "+c.error+"。":"")+(data.input_catalog_error?"清单目录 "+data.input_catalog_error:"");
    draw();
  }
  async function poll(){
    try {
      const res=await fetch("/api/map",{cache:"no-store",signal:AbortSignal.timeout(4500)});
      if(!res.ok)throw Error(); data=await res.json();connected=true;
    } catch {connected=false}
    el("connection").dataset.ok=String(connected);el("connection").textContent=connected?"已连接":"连接中断";
    if(data)update();else draw();
    setTimeout(poll,1000);
  }
  const clamp=(n,lo,hi)=>Math.max(lo,Math.min(hi,n));
  el("inspect-source").onchange=()=>{if(data)update()};
  function reset(){yaw=-.69;tilt=.67;zoom=1;pan=[0,0];draw()}
  el("orbit-reset").onclick=reset;
  el("view-top").onclick=()=>{yaw=0;tilt=Math.PI/2;pan=[0,0];draw()};
  document.querySelectorAll('input[type=checkbox]').forEach(box=>box.onchange=draw);
  canvas.onpointerdown=e=>{if(e.button!==0)return;canvas.setPointerCapture(e.pointerId);drag=[e.clientX,e.clientY];canvas.style.cursor="grabbing"};
  canvas.onpointermove=e=>{if(!drag)return;const dx=e.clientX-drag[0],dy=e.clientY-drag[1];drag=[e.clientX,e.clientY];
    if(e.shiftKey){pan[0]+=dx;pan[1]+=dy}else{yaw+=dx*.009;tilt=clamp(tilt+dy*.007,.08,Math.PI/2)}draw()};
  canvas.onlostpointercapture=()=>{drag=null;canvas.style.cursor="grab"};
  canvas.addEventListener("wheel",e=>{e.preventDefault();zoom=clamp(zoom*Math.exp(-Math.sign(e.deltaY)*.12),.25,8);draw()},{passive:false});
  canvas.onkeydown=e=>{switch(e.key){case "ArrowLeft":yaw-=.1;break;case "ArrowRight":yaw+=.1;break;case "ArrowUp":tilt=clamp(tilt+.1,.08,Math.PI/2);break;case "ArrowDown":tilt=clamp(tilt-.1,.08,Math.PI/2);break;case "+":case "=":zoom=clamp(zoom*1.12,.25,8);break;case "-":zoom=clamp(zoom/1.12,.25,8);break;default:return}e.preventDefault();draw()};
  new ResizeObserver(draw).observe(canvas);
  poll();
})();
