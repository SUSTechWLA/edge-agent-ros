/* Perspective inspection of the map snapshot. A bounded software depth buffer keeps
   the same visible surface for drawing and picking, including on GPU-less browsers. */
(() => {
  "use strict";
  const clamp = (x, low, high) => Math.max(low, Math.min(x, high));
  const xyz = p => Array.isArray(p) && p.length === 3 && p.every(Number.isFinite);
  const dot = (a, b) => a[0]*b[0] + a[1]*b[1] + a[2]*b[2];
  const halfFov = 25 * Math.PI / 180;
  const maxPixels = 800000;
  const littleEndian = new Uint8Array(new Uint32Array([1]).buffer)[0] === 1;
  function rgb(r, g, b) {
    return littleEndian ? (255 << 24 | b << 16 | g << 8 | r) >>> 0 :
      (r << 24 | g << 16 | b << 8 | 255) >>> 0;
  }
  const colors = {
    grid:rgb(59, 81, 83), indoor:rgb(42, 66, 75), outdoor:rgb(41, 73, 67),
    box:[rgb(74, 101, 109), rgb(104, 132, 139), rgb(123, 147, 151)],
    landmark:[rgb(70, 105, 91), rgb(88, 135, 111), rgb(118, 161, 134)],
    barrier:[rgb(117, 94, 67), rgb(150, 127, 91), rgb(180, 154, 111)],
    pose:rgb(255, 204, 99), stalePose:rgb(170, 154, 123), stale:rgb(141, 160, 163)
  };
  const stops = [[88, 173, 241], [69, 209, 201], [170, 220, 136], [255, 203, 105]];
  const heights = Array.from({length:128}, (_, i) => {
    const t = (i / 127) * (stops.length - 1), j = Math.min(stops.length-2, Math.floor(t));
    return rgb(...stops[j].map((channel, k) => Math.round(channel + (t-j)*(stops[j+1][k]-channel))));
  });
  const occupancy = Array.from({length:102}, (_, i) => {
    const channels=window.EdgeGrid.color(i-1);
    const tint=base=>rgb(...channels.map((c,k)=>Math.round(c*.77+base[k]*.23)));
    return [rgb(...channels),tint([75,121,161]),tint([83,151,97])];
  });
  function blend(base, over, opacity) {
    if (!base || opacity>=1) return over;
    if (opacity<=0) return base;
    // Both packed values use native RGBA byte order and solid alpha.
    const mix=shift=>Math.round(((base>>>shift)&255)*(1-opacity)+((over>>>shift)&255)*opacity);
    return (mix(24)<<24 | mix(16)<<16 | mix(8)<<8 | mix(0))>>>0;
  }

  // An orthophoto plus top-surface heights describes one visible skin per XY.
  // Limit tessellation independently of the source image resolution; the color
  // sampler below still uses the original pixels for every visible triangle.
  function appearanceMesh(texture) {
    const [xmin,ymin,xmax,ymax]=texture.bounds, hasHeight=!!texture.heights;
    const stride=Math.max(1,Math.max(texture.width,texture.height)/160);
    const cols=hasHeight?Math.ceil(texture.width/stride):1;
    const rows=hasHeight?Math.ceil(texture.height/stride):1;
    const vertices=[];
    let zMin=Infinity,zMax=-Infinity;
    for (let j=0;j<=rows;j++) for (let i=0;i<=cols;i++) {
      const u=i/cols, v=j/rows;
      const pixel=Math.min(texture.height-1,Math.floor(v*texture.height))*texture.width+
                  Math.min(texture.width-1,Math.floor(u*texture.width));
      const z=hasHeight ? texture.height_offset_m+texture.heights[pixel*4]*texture.height_scale_m : 0;
      vertices.push([xmin+u*(xmax-xmin),ymax-v*(ymax-ymin),z,u*texture.width,v*texture.height]);
      zMin=Math.min(zMin,z); zMax=Math.max(zMax,z);
    }
    return {vertices,cols,rows,zMin,zMax,width:texture.width,height:texture.height,
      colors:new Uint32Array(texture.pixels.buffer,texture.pixels.byteOffset,texture.pixels.length/4)};
  }

  // Sutherland–Hodgman clipping before the perspective divide. A face crossing
  // the camera must stay visible, even if one or more corners are behind it.
  function clippedFace(face, near) {
    const result = [];
    let previous = face[face.length - 1];
    for (const vertex of face) {
      if ((previous[2] >= near) !== (vertex[2] >= near)) {
        const t = (near-previous[2]) / (vertex[2]-previous[2]);
        const cut=previous.map((n,i)=>n+t*(vertex[i]-n)); cut[2]=near; result.push(cut);
      }
      if (vertex[2] >= near) result.push(vertex);
      previous = vertex;
    }
    return result;
  }

  class Surface {
    constructor(canvas) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
    }
    begin() {
      const cw = this.canvas.clientWidth, ch = this.canvas.clientHeight;
      if (!(cw && ch)) return false;
      const ratio = Math.min(devicePixelRatio || 1, 2, Math.sqrt(maxPixels/(cw*ch)));
      const w = Math.max(1, Math.floor(cw*ratio)), h = Math.max(1, Math.floor(ch*ratio));
      if (!this.image || w !== this.w || h !== this.h) {
        this.w = this.canvas.width = w;
        this.h = this.canvas.height = h;
        this.image = this.ctx.createImageData(w, h);
        this.pixels = new Uint32Array(this.image.data.buffer);
        this.depth = new Float32Array(w*h);
        this.ids = new Int32Array(w*h);
      }
      this.pixels.fill(0);
      this.depth.fill(0);
      this.ids.fill(0);
      this.scale = w/cw;
      return true;
    }
    project([x,y,z]) {
      return [this.w/2+x*this.focal/z, this.h/2-y*this.focal/z, 1/z];
    }
    // Reciprocal camera depth is linear in screen space. Per-pixel interpolation
    // handles long wall faces occluding points on one end but not the other.
    triangle(a, b, c, color, id, texture=null, opacity=1) {
      const bx=b[0]-a[0], by=b[1]-a[1], cx=c[0]-a[0], cy=c[1]-a[1];
      const det=bx*cy-by*cx;
      if (Math.abs(det)<1e-6) return;
      const x0=Math.max(0,Math.floor(Math.min(a[0],b[0],c[0]))), x1=Math.min(this.w-1,Math.ceil(Math.max(a[0],b[0],c[0])));
      const y0=Math.max(0,Math.floor(Math.min(a[1],b[1],c[1]))), y1=Math.min(this.h-1,Math.ceil(Math.max(a[1],b[1],c[1])));
      if(x0>x1 || y0>y1)return;
      const du=cy/det, dv=-by/det, db=b[2]-a[2], dc=c[2]-a[2];
      for (let y=y0; y<=y1; y++) {
        const dx=x0+.5-a[0], dy=y+.5-a[1];
        let u=(dx*cy-dy*cx)/det, v=(bx*dy-by*dx)/det;
        for (let x=x0, p=y*this.w+x0; x<=x1; x++,p++,u+=du,v+=dv) {
          if (u < -1e-8 || v < -1e-8 || u+v > 1.00000001) continue;
          const z=a[2]+u*db+v*dc;
          if (z >= this.depth[p]*(texture ? 1 : .9999998)) {
            if (texture) {
              // Interpolate U/Z and V/Z, then undo the perspective divide.
              const tx=clamp(Math.floor((a[3]+u*(b[3]-a[3])+v*(c[3]-a[3]))/z),0,texture.width-1);
              const ty=clamp(Math.floor((a[4]+u*(b[4]-a[4])+v*(c[4]-a[4]))/z),0,texture.height-1);
              color=texture.colors[ty*texture.width+tx];
            }
            this.depth[p]=z; this.ids[p]=id; this.pixels[p]=blend(this.pixels[p],color,opacity);
          }
        }
      }
    }
    face(vertices, near, color, id, opacity=1) {
      const polygon=clippedFace(vertices,near).map(v=>this.project(v));
      for (let i=2; i<polygon.length; i++) this.triangle(polygon[0],polygon[i-1],polygon[i],color,id,null,opacity);
    }
    line(a,b,near,color) {
      if(a[2]<near && b[2]<near) return;
      if(a[2]<near || b[2]<near) {
        const t=(near-a[2])/(b[2]-a[2]);
        const cut=[a[0]+t*(b[0]-a[0]),a[1]+t*(b[1]-a[1]),near];
        if(a[2]<near)a=cut;else b=cut;
      }
      a=this.project(a);b=this.project(b);
      let lo=0,hi=1;
      const dx=b[0]-a[0],dy=b[1]-a[1];
      for(const [start,delta,extent] of [[a[0],dx,this.w-1],[a[1],dy,this.h-1]]) {
        if(!delta) {if(start<0||start>extent)return;continue;}
        const t0=-start/delta,t1=(extent-start)/delta;
        lo=Math.max(lo,Math.min(t0,t1));hi=Math.min(hi,Math.max(t0,t1));
      }
      if(lo>hi)return;
      const steps=Math.max(1,Math.ceil((hi-lo)*Math.max(Math.abs(dx),Math.abs(dy))));
      for(let i=0;i<=steps;i++){
        const t=lo+(hi-lo)*i/steps;
        const p=Math.round(a[1]+t*dy)*this.w+Math.round(a[0]+t*dx);
        // Grid lines decorate the ground, retaining the pickable surface ID.
        if(a[2]+t*(b[2]-a[2])>=this.depth[p]*.99999)this.pixels[p]=color;
      }
    }
    marker(camera, near, radius, color, id) {
      if(camera[2]<near)return;
      const [x,y,z]=this.project(camera);
      const r=Math.max(1,radius*this.scale);
      for(let j=Math.max(0,Math.floor(y-r));j<=Math.min(this.h-1,Math.ceil(y+r));j++){
        for(let i=Math.max(0,Math.floor(x-r));i<=Math.min(this.w-1,Math.ceil(x+r));i++){
          const p=j*this.w+i;
          if((i+.5-x)**2+(j+.5-y)**2<=r*r && z>=this.depth[p]*.999999){
            this.pixels[p]=color;this.depth[p]=z;this.ids[p]=id;
          }
        }
      }
    }
  }

  function boundsFor(snapshot, options) {
    const min=[Infinity,Infinity,Infinity],max=[-Infinity,-Infinity,-Infinity];
    function bound(p){for(let i=0;i<3;i++){min[i]=Math.min(min[i],p[i]);max[i]=Math.max(max[i],p[i]);}}
    const clouds=options.cloud ? [snapshot?.cloud,...Object.entries(snapshot?.cloud_sources||{})
      .filter(([id])=>!options.mutedSources?.has(id)).map(([,v])=>v)].filter(Boolean) : [];
    let zMin=Infinity,zMax=-Infinity;
    for(const source of clouds)for(const p of source.points||[])if(xyz(p)){
      bound(p);zMin=Math.min(zMin,p[2]);zMax=Math.max(zMax,p[2]);
    }
    const usableLayer=layer=>layer && layer.status!=="error" && layer.status!=="waiting" && !layer.error;
    const scene=options.scene && usableLayer(snapshot?.scene) ? snapshot.scene : null;
    const boxes=(scene?.features||[])
      .filter(box=>(options.roof||box.kind!=="roof")&&xyz(box.xyz)&&xyz(box.size));
    for(const {xyz:[x,y,z],size:[dx,dy,dz]} of boxes){
      bound([x-dx/2,y-dy/2,z]);bound([x+dx/2,y+dy/2,z+dz]);
    }
    const zones=(scene?.zones||[])
      .filter(v=>v.bounds?.length===4 && v.bounds.every(Number.isFinite));
    const grid=options.grid && window.EdgeGrid.usable(snapshot?.grid,snapshot?.frame) ? snapshot.grid : null;
    const groundZ=grid?.origin_z??0;
    if(grid){
      for(const col of [0,grid.width])for(const row of [0,grid.height])
        bound([...window.EdgeGrid.toWorld(grid,col,row),groundZ]);
    }
    for(const {bounds:[x,y,u,v]} of zones){bound([x,y,groundZ]);bound([u,v,groundZ]);}
    const pose=options.pose && usableLayer(snapshot?.pose) && xyz(snapshot.pose.xyz) ? snapshot.pose : null;
    if(pose)bound(pose.xyz);
    const empty=!Number.isFinite(min[0]);
    return {min:empty?[-2,-2,0]:min,max:empty?[2,2,0]:max,clouds,boxes,zones,grid,groundZ,pose,empty,zMin,zMax};
  }

  window.createSpatialRenderer = (canvas,{invalidate,onPick}) => {
    const surface=new Surface(canvas), pointers=new Map();
    let yaw=-.65, pitch=.65, distance=8, target=[0,0,0], autoFit=true, scaleRadius=1;
    let right,up,out, items=[], frame="", selected=null, selectedName="", readout="", renderedCamera=null;
    let lastTexture=null, mesh=null, activeTexture=null, requestedBounds=null;
    canvas.style.touchAction="none";
    function axes(){
      const c=Math.cos(yaw),s=Math.sin(yaw),h=Math.cos(pitch),v=Math.sin(pitch);
      right=[c,s,0];up=[-s*v,c*v,h];out=[s*h,-c*h,v];
    }
    function camera(p){
      const x=p[0]-target[0], y=p[1]-target[1], z=p[2]-target[2];
      return [right[0]*x+right[1]*y, up[0]*x+up[1]*y+up[2]*z,
        distance-(out[0]*x+out[1]*y+out[2]*z)];
    }
    function fit(min,max){
      target=min.map((n,i)=>(n+max[i])/2);
      const span=max.map((n,i)=>Math.max(n-min[i],.75)/2);
      scaleRadius=Math.hypot(...span);
      distance=0;
      for(const i of [-1,1])for(const j of [-1,1])for(const k of [-1,1]){
        const v=[i*span[0],j*span[1],k*span[2]];
        const reserve=surface.focal*Math.max(Math.abs(dot(v,right))/(surface.w*.45),Math.abs(dot(v,up))/(surface.h*.43));
        distance=Math.max(distance,dot(v,out)+reserve);
      }
      distance=Math.max(distance,1.1*scaleRadius);
    }
    function record(name,position,stale=false){items.push({name,position,stale});return items.length-1;}
    // Intersect screen rays with the horizontal grid once per row. Depth is
    // constant along that scanline, so columns/rows advance by a fixed increment.
    // Work scales with the bounded viewport, even for a million-cell ROS grid.
    function gridPlane(grid,groundZ,near,stale){
      const service=window.EdgeGrid;
      const [ox,oy]=service.toWorld(grid,0,0);
      const basis=(col,row)=>{
        const [x,y]=service.toWorld(grid,col,row);
        return [(x-ox)/grid.resolution**2,(y-oy)/grid.resolution**2];
      };
      const u=basis(1,0),v=basis(0,1);
      const along=(a,x,y)=>a[0]*x+a[1]*y;
      const eye=target.map((n,i)=>n+distance*out[i]);
      const plane=groundZ-eye[2];
      if(Math.abs(plane)<1e-8)return;
      const eyeU=along(u,eye[0]-ox,eye[1]-oy),eyeV=along(v,eye[0]-ox,eye[1]-oy);
      const stepU=along(u,right[0],right[1])/surface.focal,stepV=along(v,right[0],right[1])/surface.focal;
      const x=(.5-surface.w/2)/surface.focal;
      const dx=x*right[0]-out[0],dy=x*right[1]-out[1];
      const id=record("占据栅格",null,stale);
      items[id].grid=grid;
      for(let j=0;j<surface.h;j++){
        const y=(surface.h/2-j-.5)/surface.focal;
        const invDepth=(y*up[2]-out[2])/plane;
        if(invDepth<=0||invDepth>1/near)continue;
        const depth=1/invDepth, worldX=dx+y*up[0],worldY=dy+y*up[1];
        let col=eyeU+depth*along(u,worldX,worldY),row=eyeV+depth*along(v,worldX,worldY);
        const du=depth*stepU,dv=depth*stepV;
        for(let i=0,p=j*surface.w;i<surface.w;i++,p++,col+=du,row+=dv){
          if(col<0||row<0||col>=grid.width||row>=grid.height)continue;
          const value=grid.cells[Math.floor(row)*grid.width+Math.floor(col)];
          const zone=items[surface.ids[p]];
          if(activeTexture && surface.depth[p]>invDepth*1.000001)continue;
          const tint=zone?.kind==="outdoor"?2:zone?.kind?1:0;
          const opacity=activeTexture && surface.pixels[p] ? (value<0?0:.27+value*.007) : 1;
          surface.pixels[p]=blend(surface.pixels[p],occupancy[value+1][tint],opacity);surface.depth[p]=invDepth;
          surface.ids[p]=zone?.gridId||id;
        }
      }
    }
    function drawTexture(near) {
      const id=record("仿真外观");
      items[id].texture=activeTexture;
      const projectUV=point=>{
        const projected=surface.project(point), inv=projected[2];
        projected.push(point[3]*inv,point[4]*inv);return projected;
      };
      const projected=[], transformed=[];
      for(const vertex of mesh.vertices){
        const point=camera(vertex);point.push(vertex[3],vertex[4]);transformed.push(point);
        projected.push(point[2]>=near ? projectUV(point) : null);
      }
      function paint(i,j,k) {
        const a=projected[i],b=projected[j],c=projected[k];
        if(a && b && c){ surface.triangle(a,b,c,0,id,mesh);return; }
        if(!a && !b && !c)return;
        const poly=clippedFace([transformed[i],transformed[j],transformed[k]],near).map(projectUV);
        for(let n=2;n<poly.length;n++)surface.triangle(poly[0],poly[n-1],poly[n],0,id,mesh);
      }
      const step=mesh.cols+1;
      for(let j=0;j<mesh.rows;j++)for(let i=0,p=j*step;i<mesh.cols;i++,p++){
        paint(p,p+1,p+step);paint(p+step,p+1,p+step+1);
      }
    }
    function drawAreaNames(texture,near) {
      const ctx=surface.ctx, placed=[], scale=surface.scale;
      const [xmin,ymin,xmax,ymax]=texture.bounds;
      ctx.save();ctx.textAlign="center";ctx.font="600 "+Math.max(10,13*scale)+"px system-ui";
      ctx.lineWidth=3*scale;ctx.strokeStyle="#1b322be0";ctx.fillStyle="#fffbe7";
      for(const area of texture.areas || []){
        const [a,b,c,d]=area.bounds, x=(a+c)/2,y=(b+d)/2;
        const col=clamp(Math.floor((x-xmin)*texture.width/(xmax-xmin)),0,texture.width-1);
        const row=clamp(Math.floor((ymax-y)*texture.height/(ymax-ymin)),0,texture.height-1);
        const z=texture.heights ? texture.height_offset_m+texture.heights[4*(row*texture.width+col)]*texture.height_scale_m : 0;
        const spot=camera([x,y,z]);
        if(spot[2]<=near)continue;
        const [u,v,depth]=surface.project(spot), width=ctx.measureText(area.label).width;
        if(u-width/2<2 || u+width/2>surface.w-2 || v<26*scale || v>surface.h-20*scale)continue;
        const drawnDepth=surface.depth[Math.floor(v)*surface.w+Math.floor(u)];
        if(depth < drawnDepth*.98)continue;
        if(placed.some(p=>Math.abs(u-p.u)<(width+p.width)/2+4 && Math.abs(v-p.v)<17*scale))continue;
        ctx.strokeText(area.label,u,v);ctx.fillText(area.label,u,v);placed.push({u,v,width});
      }
      ctx.restore();
    }
    function render(snapshot,options){
      const geometry=boundsFor(snapshot,options);
      const tex=options.texture?.frame===snapshot?.frame ? options.texture : null;
      if(activeTexture && !tex)autoFit=false;
      activeTexture=tex;
      if(tex) {
        if(tex!==lastTexture) {mesh=appearanceMesh(tex); lastTexture=tex;}
        geometry.empty=false;
      } else {mesh=null;lastTexture=null;}
      if(!surface.begin())return {empty:geometry.empty};
      axes();surface.focal=surface.h/(2*Math.tan(halfFov));
      if(requestedBounds){
        const [a,b,c,d]=requestedBounds;
        fit([a,b,0],[c,d,Math.min(4,.1*Math.max(c-a,d-b))]);requestedBounds=null;autoFit=false;
      }else if(autoFit && tex){
        const [a,b,c,d]=tex.bounds;
        fit([a,b,mesh.zMin],[c,d,mesh.zMax]);
      }else if(autoFit)fit(geometry.min,geometry.max);
      renderedCamera={right,up,out,target,distance};
      const near=Math.max(.0001,distance*.003);
      items=[null];frame=snapshot?.frame||"";
      if(tex)drawTexture(near);
      const groundZ=geometry.groundZ;
      const staleGrid=!options.connected || geometry.grid?.status==="stale";
      const staleScene=!options.connected || snapshot?.scene?.status==="stale";
      for(const {kind,label,bounds:[x,y,u,v]} of geometry.zones){
        const name=label||(kind==="outdoor"?"室外区域":"场景区域");
        const id=record(name,null,staleScene);
        items[id].kind=kind;
        if(geometry.grid){
          items[id].gridId=record("占据栅格 · "+name,null,staleGrid || staleScene);
          items[items[id].gridId].grid=geometry.grid;
        }
        surface.face([[x,y,groundZ],[u,y,groundZ],[u,v,groundZ],[x,v,groundZ]].map(camera),
          near,kind==="outdoor"?colors.outdoor:colors.indoor,id,tex ? .15 : 1);
      }
      if(geometry.grid)gridPlane(geometry.grid,groundZ,near,staleGrid);
      else if(options.grid!==false&&!geometry.empty&&!tex){
        const [a,b]=geometry.min,[c,d]=geometry.max;
        const step=10**Math.floor(Math.log10(Math.max(1,c-a,d-b)/4));
        const x0=Math.floor(a/step)*step,x1=Math.ceil(c/step)*step,y0=Math.floor(b/step)*step,y1=Math.ceil(d/step)*step;
        for(let x=x0;x<=x1+.01*step;x+=step)surface.line(camera([x,y0,0]),camera([x,y1,0]),near,colors.grid);
        for(let y=y0;y<=y1+.01*step;y+=step)surface.line(camera([x0,y,0]),camera([x1,y,0]),near,colors.grid);
      }
      for(const box of geometry.boxes){
        const [x,y,z]=box.xyz,[dx,dy,dz]=box.size;
        const corners=[[-1,-1],[1,-1],[1,1],[-1,1]];
        const base=corners.map(([i,j])=>camera([x+i*dx/2,y+j*dy/2,z]));
        const top=corners.map(([i,j])=>camera([x+i*dx/2,y+j*dy/2,z+dz]));
        const id=record(box.label||box.id||"场景表面",null,staleScene);
        const palette=box.kind==="landmark"?colors.landmark:box.kind==="barrier"?colors.barrier:colors.box;
        surface.face(base,near,palette[0],id);
        for(let i=0;i<4;i++){
          const j=(i+1)%4;
          surface.face([base[i],base[j],top[j],top[i]],near,palette[i%2],id);
        }
        surface.face(top,near,palette[2],id);
      }
      const range=Math.max(.01,geometry.zMax-geometry.zMin);
      for(const source of geometry.clouds){
        const label="实测点 · "+(source.name||source.source_frame||source.topic||"主点云");
        const live=options.connected && source.status==="online" && !source.error;
        for(const p of source.points||[])if(xyz(p)){
          const index=clamp(Math.round(127*(p[2]-geometry.zMin)/range),0,127);
          surface.marker(camera(p),near,1.85,live?heights[index]:colors.stale,record(label,p,!live));
        }
      }
      if(geometry.pose){
        const p=geometry.pose.xyz;
        const live=options.connected && geometry.pose.status==="online";
        surface.marker(camera(p),near,6,live?colors.pose:colors.stalePose,record("机器人位姿",p,!live));
      }
      surface.ctx.putImageData(surface.image,0,0);
      if(tex && options.scene && tex.areas?.length)drawAreaNames(tex,near);
      if(selected){
        const current=items.find(item=>item && item.name===selectedName &&
          (!item.position || item.position.every((n,i)=>n===selected[i])));
        if(current)publish(current);
        else clearPick();
      }
      if(selected){
        const v=camera(selected);
        if(v[2]>near) {
          const [x,y,z]=surface.project(v),i=Math.floor(x),j=Math.floor(y);
          if(i>=0&&i<surface.w&&j>=0&&j<surface.h && z>=surface.depth[j*surface.w+i]*.995){
            const ctx=surface.ctx;ctx.strokeStyle="#fff2b6";ctx.lineWidth=2*surface.scale;
            ctx.beginPath();ctx.arc(x,y,8*surface.scale,0,2*Math.PI);ctx.stroke();
          }
        }
      }
      return {empty:geometry.empty,zMin:Number.isFinite(geometry.zMin)?geometry.zMin:undefined,
        zMax:Number.isFinite(geometry.zMax)?geometry.zMax:undefined};
    }
    function publish(item) {
      const position=selected.map((n,i)=>"XYZ"[i]+" "+n.toFixed(2)).join("  ");
      const cell=item.grid&&window.EdgeGrid.cellAt(item.grid,selected[0],selected[1]);
      if(item.grid&&!cell){clearPick();return;}
      const gridInfo=cell?" · 列 "+cell.col+" 行 "+cell.row+" · "+window.EdgeGrid.describe(cell.value)+" · 值 "+cell.value:"";
      let appearance="";
      if(item.texture){
        const area=(item.texture.areas||[]).filter(({bounds:[a,b,c,d]})=>selected[0]>=a && selected[0]<=c && selected[1]>=b && selected[1]<=d)
          .sort((p,q)=>{const size=({bounds:[a,b,c,d]})=>(c-a)*(d-b);return size(p)-size(q);})[0];
        appearance=(area?" · "+area.label:"")+" · "+item.texture.source;
      }
      const next=item.name+appearance+gridInfo+" · "+position+" m"+(frame?" · "+frame:"")+(item.stale?" · 上次观测":"");
      if(next!==readout){readout=next;onPick(next);}
    }
    function clearPick(){selected=null;selectedName="";readout="";onPick("");invalidate();}
    function pick(clientX,clientY,nearest=false){
      if(!surface.image)return;
      const rect=canvas.getBoundingClientRect();
      const x=(clientX-rect.left)*surface.w/rect.width,y=(clientY-rect.top)*surface.h/rect.height;
      const r=nearest?Math.max(surface.w,surface.h):7*surface.scale;
      let best=-1,dist=r*r;
      for(let j=Math.max(0,Math.floor(y-r));j<=Math.min(surface.h-1,Math.ceil(y+r));j++){
        for(let i=Math.max(0,Math.floor(x-r));i<=Math.min(surface.w-1,Math.ceil(x+r));i++){
          const p=j*surface.w+i,ds=(x-i-.5)**2+(y-j-.5)**2;
          if(surface.ids[p]&&ds<=dist){best=p;dist=ds;}
        }
      }
      if(best<0){clearPick();return;}
      const item=items[surface.ids[best]];
      const u=((best%surface.w)+.5-surface.w/2)/surface.focal;
      const v=(surface.h/2-Math.floor(best/surface.w)-.5)/surface.focal;
      // A wheel/pan event can precede the next animation frame. Unproject from
      // the pose that actually produced the visible pixels.
      const cam=renderedCamera;
      let depth=1/surface.depth[best];
      if(item.grid){
        // The ground plane is exact. Avoid quantizing its intersection through
        // the Float32 z-buffer at the edge of two neighboring occupancy cells.
        const plane=(item.grid.origin_z??0)-cam.target[2]-cam.distance*cam.out[2];
        depth=plane/(v*cam.up[2]-cam.out[2]);
      }
      selected=item.position||cam.target.map((n,i)=>n+depth*(u*cam.right[i]+v*cam.up[i])+(cam.distance-depth)*cam.out[i]);
      selectedName=item.name;publish(item);invalidate();
    }
    function orbit(dx,dy){autoFit=false;yaw-=dx*.007;pitch=clamp(pitch+dy*.007,-1.54,Math.PI/2);invalidate();}
    function pan(dx,dy){
      if(!surface.focal)return;
      autoFit=false;
      const units=distance*surface.scale/surface.focal;
      target=target.map((n,i)=>n+(dy*up[i]-dx*right[i])*units);invalidate();
    }
    function zoomBy(factor){
      if(!Number.isFinite(factor)||factor<=0)return;
      autoFit=false;distance=clamp(distance/factor,Math.max(.01,.005*scaleRadius),Math.max(32,80*scaleRadius));invalidate();
    }
    function reset(){yaw=-.65;pitch=.65;requestedBounds=null;autoFit=true;clearPick();}
    function focusBounds(bounds){requestedBounds=bounds;clearPick();}
    function top(){yaw=0;pitch=Math.PI/2;autoFit=true;invalidate();}
    const center=([a,b])=>[(a.x+b.x)/2,(a.y+b.y)/2];
    const gap=([a,b])=>Math.hypot(a.x-b.x,a.y-b.y);
    canvas.addEventListener("pointerdown",e=>{
      if(e.button!==0&&e.button!==1&&e.button!==2)return;
      canvas.focus({preventScroll:true});canvas.setPointerCapture(e.pointerId);
      pointers.set(e.pointerId,{x:e.clientX,y:e.clientY,x0:e.clientX,y0:e.clientY,
        pan:e.button===2||e.shiftKey,middle:e.button===1,moved:e.button!==0});
      if(pointers.size>1)for(const p of pointers.values())p.moved=true;
      canvas.style.cursor="grabbing";e.preventDefault();
    });
    canvas.addEventListener("pointermove",e=>{
      const p=pointers.get(e.pointerId);if(!p)return;
      const pair=Array.from(pointers.values()),oldCenter=pair.length===2?center(pair):null;
      const oldGap=oldCenter?gap(pair):0,dx=e.clientX-p.x,dy=e.clientY-p.y;
      p.x=e.clientX;p.y=e.clientY;
      if(oldCenter){
        const now=center(pair),newGap=gap(pair);
        if(oldGap>1 && newGap>1)zoomBy(newGap/oldGap);
        pan(now[0]-oldCenter[0],now[1]-oldCenter[1]);
      }else if(pair.length===1){
        if(!p.moved&&Math.hypot(p.x-p.x0,p.y-p.y0)<4)return;
        p.moved=true;
        if(p.middle)zoomBy(Math.exp(-dy*.01));else if(p.pan||e.shiftKey)pan(dx,dy);else orbit(dx,dy);
      }
    });
    function release(e){
      const p=pointers.get(e.pointerId);if(!p)return;
      pointers.delete(e.pointerId);if(!pointers.size)canvas.style.cursor="grab";
      if(e.type==="pointerup" && !p.moved && !p.pan &&
         Math.hypot(e.clientX-p.x0,e.clientY-p.y0)<4)pick(e.clientX,e.clientY);
    }
    for(const type of ["pointerup","pointercancel","lostpointercapture"])canvas.addEventListener(type,release);
    canvas.addEventListener("contextmenu",e=>e.preventDefault());
    canvas.addEventListener("wheel",e=>{
      e.preventDefault();const unit=e.deltaMode===1?16:e.deltaMode===2?canvas.clientHeight:1;
      zoomBy(Math.exp(-clamp(e.deltaY*unit,-400,400)*.0015));
    },{passive:false});
    canvas.addEventListener("keydown",e=>{
      const directions={ArrowLeft:[-1,0],ArrowRight:[1,0],ArrowUp:[0,1],ArrowDown:[0,-1]};
      if(directions[e.key]){
        const [dx,dy]=directions[e.key];if(e.shiftKey)pan(dx*18,-dy*18);else orbit(dx*14,dy*14);
      }else if(e.key==="+"||e.key==="=")zoomBy(1.15);
      else if(e.key==="-")zoomBy(1/1.15);
      else if(e.key==="Home")reset();
      else if(e.key==="Escape")clearPick();
      else if(e.key==="Enter"){
        const r=canvas.getBoundingClientRect();pick(r.left+r.width/2,r.top+r.height/2,true);
      }else return;
      e.preventDefault();
    });
    return {render,reset,top,zoomBy,focusBounds,clearPick};
  };
})();
