/* Metric orthographic inspection of one body-frame scan. This viewer only
   consumes the observation and the planner's recorded proposal. */
window.FieldScene = (() => {
  'use strict';
  const TAU = 2 * Math.PI;
  const CAMERA_CELLS_PER_METRE = 10;
  const clamp = (x, low, high) => Math.min(high, Math.max(low, x));
  const point = p => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]);
  const cssFont = 'system-ui, "Microsoft YaHei", sans-serif';
  const colors = {ground:'#21403f', grid:'#294240', major:'#385450', block:'#ffa474',
    route:'#b7ff91', vehicle:'#d8f4e7', ink:'#112121', line:'#9db8ac'};
  const face = (ctx, vertices, fill, stroke) => {
    ctx.beginPath();
    vertices.forEach(([x,y], i) => i ? ctx.lineTo(x,y) : ctx.moveTo(x,y));
    ctx.closePath();
    if (fill) {ctx.fillStyle=fill; ctx.fill();}
    if (stroke) {ctx.strokeStyle=stroke; ctx.lineWidth=.7; ctx.stroke();}
  };
  function line(ctx, points, color, width=1, dash=[]) {
    ctx.strokeStyle=color; ctx.lineWidth=width; ctx.setLineDash(dash);
    ctx.beginPath(); points.forEach(([x,y],i)=> i ? ctx.lineTo(x,y) : ctx.moveTo(x,y)); ctx.stroke();
    ctx.setLineDash([]);
  }
  function dot(ctx, p, radius, fill, stroke) {
    ctx.beginPath(); ctx.arc(p[0],p[1],radius,0,TAU); ctx.fillStyle=fill; ctx.fill();
    if(stroke){ctx.strokeStyle=stroke;ctx.lineWidth=1.5;ctx.stroke();}
  }
  function tint(rgb, amount) {return 'rgb('+rgb.map(v=>Math.round(clamp(v*amount,0,255))).join(',')+')';}
  function heightTint(z) {
    const t=clamp(z/1.65,0,1);
    return [184+49*t,139+58*t,105+55*t];
  }
  function cube(ctx, project, x0,y0,z0,x1,y1,z1, dx,dy, rgb, outline) {
    const a=project(x0,y0,z1), b=project(x1,y0,z1), c=project(x1,y1,z1), d=project(x0,y1,z1);
    // Each visible side touches the face nearest the orbit camera.
    if(Math.abs(dx)>.0001) {
      const x=dx<0 ? x0:x1, lo=dx<0?a:b, hi=dx<0?d:c;
      face(ctx,[project(x,y0,z0),project(x,y1,z0),hi,lo],tint(rgb,.58),outline);
    }
    if(Math.abs(dy)>.0001) {
      const y=dy<0 ? y0:y1, lo=dy<0?a:d, hi=dy<0?b:c;
      face(ctx,[project(x0,y,z0),project(x1,y,z0),hi,lo],tint(rgb,.74),outline);
    }
    face(ctx,[a,b,c,d],tint(rgb,1),outline);
  }

  function cameraMesh(scene) {
    if(scene?.registration!=='camera_only'||scene.axes!=='x_right_y_down_z_forward'||
       !Array.isArray(scene.vertices)||scene.vertices.length>100000||!Array.isArray(scene.triangles))return null;
    // Optical right/down/forward expressed in viewer forward/left/up axes.
    const vertices=scene.vertices.map(v=>Array.isArray(v)&&v.length===6&&v.every(Number.isFinite)&&v[2]>0?
      [v[2],-v[0],-v[1],v[3],v[4],v[5]]:null);
    const valid=vertices.filter(Boolean);
    if(!valid.length)return {vertices,triangles:[],center:[0,0,0],span:[2,2,2],colorCells:0};
    const occupied=new Set();
    for(const [forward,left,up] of valid)
      occupied.add([-left,-up,forward].map(n=>Math.floor(n*CAMERA_CELLS_PER_METRE)).join(','));
    const center=[],span=[];
    for(let axis=0;axis<3;axis++){
      const values=valid.map(v=>v[axis]).sort((a,b)=>a-b);
      const low=values[Math.floor((values.length-1)*.02)],high=values[Math.ceil((values.length-1)*.98)];
      center.push((high+low)/2);span.push(Math.max(.2,high-low));
    }
    const triangles=scene.triangles.slice(0,200000).filter(t=>Array.isArray(t)&&t.length===3&&
      t.every(index=>Number.isInteger(index)&&vertices[index]));
    return {vertices,triangles,center,span,colorCells:occupied.size};
  }

  function sourcePixels(scene, requireMapping) {
    const {image_width:w,image_height:h,texture_mapping:m,vertices}=scene;
    if(!Number.isInteger(w)||!Number.isInteger(h)||w<1||h<1||w*h>4194304)return null;
    if(!m){
      // Historical field viewers used the sampling lattice before explicit UVs existed.
      if(requireMapping||!Number.isInteger(scene.grid_width)||!Number.isInteger(scene.stride_px)||
         scene.stride_px<1||scene.grid_width!==Math.ceil(w/scene.stride_px)||
         vertices.length!==scene.grid_width*Math.ceil(h/scene.stride_px))return null;
      return vertices.map((p,i)=>p?[(i%scene.grid_width)*scene.stride_px,Math.floor(i/scene.grid_width)*scene.stride_px]:null);
    }
    if(m.schema_version!==1||m.origin!=='top_left'||m.coordinates!=='normalized_pixel_centers'||
       m.image_ref!==scene.source_evidence?.color?.artifact_ref||!Array.isArray(m.uv)||m.uv.length!==vertices.length)return null;
    const pixels=[];
    for(let i=0;i<m.uv.length;i++){
      const uv=m.uv[i];
      if(vertices[i]===null && uv===null){pixels.push(null);continue;}
      if(!vertices[i]||!Array.isArray(uv)||uv.length!==2||!uv.every(Number.isFinite))return null;
      const x=uv[0]*w-.5, y=uv[1]*h-.5;
      if(x<-.0001||y<-.0001||x>w-1+.0001||y>h-1+.0001)return null;
      pixels.push([x,y]);
    }
    return pixels;
  }

  function surfaceGray(a,b,c){
    // Use the measured optical XYZ to light the geometry independently of source RGB.
    const ab=[b[9]-a[9],b[10]-a[10],b[8]-a[8]],ac=[c[9]-a[9],c[10]-a[10],c[8]-a[8]];
    const nx=ab[1]*ac[2]-ab[2]*ac[1],ny=ab[2]*ac[0]-ab[0]*ac[2],nz=ab[0]*ac[1]-ab[1]*ac[0];
    const length=Math.hypot(nx,ny,nz);
    return length ? 76+138*Math.abs((.32*nx+.65*ny+.69*nz)/length) : 76;
  }

  // Distance in pixels to a 10 cm lattice plane at this visible surface.
  // Suppress tangential and sub-pixel crossings to avoid a solid or aliased wash.
  function boundaryInk(metres,gradient){
    if(gradient<.0005||gradient>=.5)return 0;
    const units=metres*CAMERA_CELLS_PER_METRE;
    const distance=Math.abs(units-Math.round(units));
    return Math.max(0,1-distance/(.95*gradient))*Math.min(1,(.5-gradient)*4);
  }

  // Orthographic barycentric RGB and depth interpolation. Each pixel belongs
  // to the closest measured triangle, even when rotating through folded surfaces.
  function rasterTriangle(image,depth,a,b,c,texture,showGrid,gray) {
    const left=Math.max(0,Math.floor(Math.min(a[0],b[0],c[0]))), right=Math.min(image.width-1,Math.ceil(Math.max(a[0],b[0],c[0])));
    const top=Math.max(0,Math.floor(Math.min(a[1],b[1],c[1]))), bottom=Math.min(image.height-1,Math.ceil(Math.max(a[1],b[1],c[1])));
    if(left>right||top>bottom)return;
    const x1=b[0]-a[0],y1=b[1]-a[1],x2=c[0]-a[0],y2=c[1]-a[1],area=x1*y2-x2*y1;
    if(Math.abs(area)<.0001)return;
    const uStep=y2/area,vStep=-y1/area;
    const gridGradient=axis=>{
      const ab=b[axis]-a[axis],ac=c[axis]-a[axis];
      return CAMERA_CELLS_PER_METRE*Math.hypot(ab*uStep+ac*vStep,(-ab*x2+ac*x1)/area);
    };
    const gz=showGrid?gridGradient(8):0, gx=showGrid?gridGradient(9):0, gy=showGrid?gridGradient(10):0;
    for(let y=top;y<=bottom;y++){
      let u=((left+.5-a[0])*y2-(y+.5-a[1])*x2)/area;
      let v=((y+.5-a[1])*x1-(left+.5-a[0])*y1)/area;
      for(let x=left;x<=right;x++,u+=uStep,v+=vStep){
        if(u<0||v<0||u+v>1)continue;
        const w=1-u-v, z=w*a[2]+u*b[2]+v*c[2], pos=y*image.width+x;
        if(z<=depth[pos])continue;
        depth[pos]=z;
        const i=4*pos;
        const eyeZ=w*a[8]+u*b[8]+v*c[8];
        if(texture){
          // The view has an orthographic camera, but the source PNG came from
          // a pinhole camera. Interpolate optical depth before sampling its UV.
          const sx=clamp((w*a[6]+u*b[6]+v*c[6])/eyeZ,0,texture.width-1);
          const sy=clamp((w*a[7]+u*b[7]+v*c[7])/eyeZ,0,texture.height-1);
          const ix=Math.floor(sx), iy=Math.floor(sy);
          const base=4*(ix+iy*texture.width), nextX=ix+1<texture.width?4:0;
          const nextY=iy+1<texture.height?texture.width*4:0, fx=sx-ix, fy=sy-iy;
          for(let ch=0;ch<3;ch++){
            const l=base+ch,top=texture.data[l]*(1-fx)+texture.data[l+nextX]*fx;
            const bottom=texture.data[l+nextY]*(1-fx)+texture.data[l+nextX+nextY]*fx;
            image.data[i+ch]=top*(1-fy)+bottom*fy;
          }
        }else if(gray!==null){
          image.data[i]=image.data[i+1]=image.data[i+2]=gray;
        }else{
          image.data[i]=w*a[3]+u*b[3]+v*c[3];image.data[i+1]=w*a[4]+u*b[4]+v*c[4];
          image.data[i+2]=w*a[5]+u*b[5]+v*c[5];
        }
        if(showGrid){
          const ink=Math.max(boundaryInk(eyeZ,gz),boundaryInk(w*a[9]+u*b[9]+v*c[9],gx),
            boundaryInk(w*a[10]+u*b[10]+v*c[10],gy));
          if(ink){
            const r=image.data[i],g=image.data[i+1],b=image.data[i+2];
            const alpha=ink*.53;
            const dark=.2126*r+.7152*g+.0722*b<108;
            image.data[i]=r+alpha*((dark?146:20)-r);
            image.data[i+1]=g+alpha*((dark?228:74)-g);
            image.data[i+2]=b+alpha*((dark?196:70)-b);
          }
        }
        image.data[i+3]=255;
      }
    }
  }

  function create(canvas, options={}) {
    const ctx=canvas.getContext('2d',{alpha:false});
    let scene=null, plan=null, frame=0, disposed=false, selected=null, pickable=[];
    let yaw=.39, pitch=.92, zoom=1, center=[.53,0], view='orbit', home=true;
    let width=0, height=0, scale=1, cos=1, sin=0, sinPitch=1, cosPitch=0, centerZ=0;
    let mesh=null, meshImage=null, meshDepth=null, texture=null, textureLoad=0, pixels=null;
    let loadedImage=null;
    const meshCanvas=document.createElement('canvas'),meshCtx=meshCanvas.getContext('2d');
    const layers={ground:true, occupied:true, route:true, rgbGrid:true, sourceRGB:true};
    const touches=new Map(), listeners=new AbortController();
    let born=null, dragged=false;
    canvas.style.touchAction='none';
    canvas.style.cursor='grab';
    if(!canvas.hasAttribute('tabindex'))canvas.tabIndex=0;
    if(!canvas.hasAttribute('aria-label'))canvas.setAttribute('aria-label','局部占用地图。方向键旋转，Shift 加方向键平移，加减键缩放，Home 复位。');

    const cameraOnly=()=>scene?.registration==='camera_only';
    const route=()=>!cameraOnly() && ['planned','partial'].includes(plan?.status) &&
      Array.isArray(plan.path_body_m) && plan.path_body_m.every(point) ? plan.path_body_m : [];
    const state=()=>({view,zoom,yaw,pitch,center:[...center],layers:{...layers}});
    function changed(){
      for(const [key,val] of Object.entries({view,zoom:zoom.toFixed(3),yaw:yaw.toFixed(3),pitch:pitch.toFixed(3),
        panX:center[0].toFixed(3),panY:center[1].toFixed(3),routePoints:layers.route?route().length:0,
        voxelCount:!cameraOnly()&&layers.occupied?(scene?.voxels?.length||0):0,decision:cameraOnly()?'camera':plan?.status||'unknown',
        triangleCount:cameraOnly()?(mesh?.triangles.length||0):0,frameKind:cameraOnly()?'camera':'body',
        colorCells:cameraOnly()?(mesh?.colorCells||0):0,rgbGrid:cameraOnly()&&layers.rgbGrid,
        ground:layers.ground,occupied:layers.occupied,route:layers.route}))canvas.dataset[key]=String(val);
      options.onChange?.(state());
      drawSoon();
    }
    function drawSoon(){if(!frame&&!disposed)frame=requestAnimationFrame(draw);}
    function fitTarget(){
      if(cameraOnly()){
        const target=mesh?.center||[0,0,0];center=[target[0],target[1]];centerZ=target[2];return;
      }
      centerZ=0;
      const path=route();
      if(path.length>1){
        const xx=[0,...path.map(p=>p[0])], yy=[0,...path.map(p=>p[1])];
        center=[.5*(Math.min(...xx)+Math.max(...xx)),.5*(Math.min(...yy)+Math.max(...yy))];
      }else center=[.53,0];
    }
    function reset(){
      view='orbit'; yaw=cameraOnly()?-.20:.39; pitch=cameraOnly()?.40:.92; zoom=1; home=true; selected=null; fitTarget(); changed();
    }
    function setView(mode){
      if(mode!=='top' && mode!=='orbit')return;
      view=mode; yaw=mode==='top'?0:(cameraOnly()?-.20:.39); pitch=mode==='top'?Math.PI/2:(cameraOnly()?.40:.92);changed();
    }
    function zoomBy(factor){
      if(Number.isFinite(factor)&&factor>0){zoom=clamp(zoom*factor,.35,5);changed();}
    }
    function cameraAxes(){
      cos=Math.cos(yaw);sin=Math.sin(yaw);sinPitch=Math.sin(pitch);cosPitch=Math.cos(pitch);
      if(cameraOnly()){
        const [dx,dy,dz]=mesh?.span||[2,2,2];
        scale=zoom*Math.min(width*.92/(Math.abs(sin)*dx+Math.abs(cos)*dy),
          height*.9/((Math.abs(cos)*dx+Math.abs(sin)*dy)*sinPitch+dz*cosPitch));return;
      }
      scale=Math.min(width*.32,height*.4);
      const proposed=route();
      if(proposed.length>1){
        const x=proposed.map(p=>p[0]), y=proposed.map(p=>p[1]);
        const dx=Math.max(0,...x)-Math.min(0,...x),dy=Math.max(0,...y)-Math.min(0,...y);
        scale=Math.min(scale,width*.76/(dx*Math.abs(sin)+dy*Math.abs(cos)||1),
          height*.70/((dx*Math.abs(cos)+dy*Math.abs(sin))*sinPitch||1));
      }
      scale=Math.max(1,scale*zoom);
    }
    function project(x,y,z=0){
      const dx=x-center[0], dy=y-center[1];
      return [width*.50+(dx*sin-dy*cos)*scale,
        height*(cameraOnly()?.5:.56)-((dx*cos+dy*sin)*sinPitch+(z-centerZ)*cosPitch)*scale];
    }
    function cameraDepth(x,y,z){return -(x*cos+y*sin)*cosPitch+z*sinPitch;}
    function onscreen(p,margin=0){return p[0]>=-margin&&p[0]<=width+margin&&p[1]>=-margin&&p[1]<=height+margin;}
    function square(cx,cy,half,z){
      return [project(cx-half,cy-half,z),project(cx+half,cy-half,z),
        project(cx+half,cy+half,z),project(cx-half,cy+half,z)];
    }
    function background(){
      const bg=ctx.createLinearGradient(0,0,0,height);
      bg.addColorStop(0,'#111f23');bg.addColorStop(1,'#142727');ctx.fillStyle=bg;ctx.fillRect(0,0,width,height);
      if(cameraOnly())return;
      if(layers.ground && scene?.observed_ground && Number.isFinite(scene.ground_cell_m)){
        const half=scene.ground_cell_m/2;
        ctx.beginPath();
        for(const row of scene.observed_ground){
          if(!point(row) || !onscreen(project(row[0],row[1]),scale*.2))continue;
          const xy=square(row[0],row[1],half,0);
          ctx.moveTo(...xy[0]);ctx.lineTo(...xy[1]);ctx.lineTo(...xy[2]);ctx.lineTo(...xy[3]);ctx.closePath();
        }
        ctx.fillStyle=colors.ground;ctx.fill();
      }
      if(layers.occupied && scene?.occupied_ground && scene.ground_cell_m>0){
        for(const [x,y] of scene.occupied_ground){
          if(onscreen(project(x,y),scale*.2))face(ctx,square(x,y,scene.ground_cell_m*.49,.002),'#634c3c77');
        }
      }
      if(!layers.ground)return;
      const radius=Math.min(100,Math.ceil(1.2*(width+height/sinPitch)/scale));
      const step=scale>100?.25:scale>55?.5:1;
      const x0=Math.floor(center[0]-radius),x1=Math.ceil(center[0]+radius);
      const y0=Math.floor(center[1]-radius),y1=Math.ceil(center[1]+radius);
      for(let k=x0;k<=x1;k+=step)line(ctx,[project(k,y0),project(k,y1)],Number.isInteger(k)?colors.major:colors.grid,Number.isInteger(k)?.85:.55);
      for(let k=y0;k<=y1;k+=step)line(ctx,[project(x0,k),project(x1,k)],Number.isInteger(k)?colors.major:colors.grid,Number.isInteger(k)?.85:.55);
    }
    function rover(){
      const f=plan?.footprint;
      if(!f || ![f.front_m,f.rear_m,f.left_m,f.right_m].every(v=>Number.isFinite(v)&&v>0))return;
      const xmin=-f.rear_m, xmax=f.front_m, ymin=-f.right_m, ymax=f.left_m;
      const footLength=xmax-xmin, footWidth=ymax-ymin;
      // Schematic marker inside the measured footprint. Only XY dimensions describe the vehicle.
      const h=Math.min(footWidth*.34,.12), dx=-cos*cosPitch, dy=-sin*cosPitch;
      const box=(x0,y0,z0,x1,y1,z1,rgb)=>cube(ctx,project,x0,y0,z0,x1,y1,z1,dx,dy,rgb,'#1c3739');
      const edge=footWidth*.12;
      for(const x of [xmin+footLength*.22,xmax-footLength*.22])
        for(const y of [ymin,ymax-edge])
          box(x-.09*footLength,y,.012,x+.09*footLength,y+edge,.75*h,[104,141,142]);
      box(xmin,ymin+edge,.016,xmax,ymax-edge,h,[202,227,216]);
      const bodyHalf=.27*footWidth, mid=(xmin+xmax)/2, side=(ymin+ymax)/2;
      const top=h+.004;
      face(ctx,[[mid+.30*footLength,side],[mid-.17*footLength,side+bodyHalf],[mid-.08*footLength,side],
        [mid-.17*footLength,side-bodyHalf]].map(([x,y])=>project(x,y,top)),'#197d73');
      line(ctx,[project(xmax,ymin+edge,h+.005),project(xmax,ymax-edge,h+.005)],'#ebfff4',3);
    }
    function occupation(){
      const drawables=[];
      if(layers.occupied&&scene?.voxels && scene.voxel_size_m>0){
        const size=scene.voxel_size_m,half=size*.475;
        for(let i=0;i<scene.voxels.length;i++){
          const v=scene.voxels[i];
          if(!point(v)||!Number.isFinite(v[2]))continue;
          const [x,y,z]=v;
          const screen=project(x,y,z);
          if(!onscreen(screen,scale*size*1.6))continue;
          drawables.push({i,x,y,z,screen,depth:cameraDepth(x,y,z),half});
        }
      }else if(layers.occupied && !scene && plan?.scan_hits_body_m){
        for(const h of plan.scan_hits_body_m){
          if(point(h))face(ctx,square(h[0],h[1],.016,0), '#9ad1c9');
        }
      }
      drawables.sort((a,b)=>a.depth-b.depth);
      pickable=[];
      const dx=-cos*cosPitch,dy=-sin*cosPitch;
      for(const row of drawables){
        const {x,y,z,i,half,screen}=row;
        const rgb=scene.voxel_colors?.[i];
        const useRGB=scene.texture?.mode==='registered_rgb'&&Array.isArray(rgb)&&rgb.length===3&&rgb.every(v=>Number.isFinite(v)&&v>=0&&v<=255);
        const palette=useRGB?rgb:heightTint(z);
        cube(ctx,project,x-half,y-half,z-half,x+half,y+half,z+half,dx,dy,palette,'#2c5050');
        pickable.push({i,p:screen,r:Math.max(5,scale*half*1.25)});
      }
      // Draw the robot as a location marker above the scan. Overhanging
      // returns can otherwise completely hide the robot and its front edge.
      rover();
    }
    function drawCamera(){
      if(!mesh)return;
      const sampled=layers.sourceRGB?texture:null;
      const geometryOnly=!sampled && (!layers.sourceRGB || options.requireTextureMapping);
      const ratio=Math.min(1.25,window.devicePixelRatio||1,Math.sqrt(950000/(width*height)));
      const w=Math.max(1,Math.round(width*ratio)),h=Math.max(1,Math.round(height*ratio));
      if(!meshImage||meshImage.width!==w||meshImage.height!==h){
        meshCanvas.width=w;meshCanvas.height=h;
        meshImage=meshCtx.createImageData(w,h);meshDepth=new Float32Array(w*h);
      }
      meshImage.data.fill(0);meshDepth.fill(-Infinity);
      const projected=mesh.vertices.map((v,index)=>{
        if(!v)return null;
        const [x,y,z,r,g,b]=v, [sx,sy]=project(x,y,z);
        const uv=pixels?.[index];
        return [sx*ratio,sy*ratio,cameraDepth(x,y,z),r,g,b,uv?x*uv[0]:0,uv?x*uv[1]:0,x,-y,-z];
      });
      for(const triangle of mesh.triangles){
        const a=projected[triangle[0]],b=projected[triangle[1]],c=projected[triangle[2]];
        rasterTriangle(meshImage,meshDepth,a,b,c,sampled,layers.rgbGrid,geometryOnly?surfaceGray(a,b,c):null);
      }
      // Retain real samples on fine edges and isolated surfaces where depth
      // discontinuities deliberately leave holes in the triangulation.
      for(const p of projected){
        if(!p)continue;
        const x=Math.floor(p[0]),y=Math.floor(p[1]),i=y*w+x;
        if(x<0||y<0||x>=w||y>=h||p[2]<meshDepth[i])continue;
        const rgba=geometryOnly?[153,153,153,255]:[p[3],p[4],p[5],255];
        if(sampled){
          const u=clamp(Math.round(p[6]/p[8]),0,sampled.width-1),v=clamp(Math.round(p[7]/p[8]),0,sampled.height-1);
          const source=4*(v*sampled.width+u);
          for(let ch=0;ch<3;ch++)rgba[ch]=sampled.data[source+ch];
        }
        meshImage.data.set(rgba,4*i);meshDepth[i]=p[2];
      }
      meshCtx.putImageData(meshImage,0,0);ctx.drawImage(meshCanvas,0,0,width,height);
      pickable=[];
    }
    function useTexture(input){
      const load=++textureLoad;
      pixels=input?sourcePixels(input,options.requireTextureMapping):null;
      const update=(state,image)=>{
        canvas.dataset.texture=state==='ready'?'source_png':state;
        options.onTextureState?.(state,image);
        drawSoon();
      };
      const hash=input?.source_evidence?.color?.artifact_ref;
      const url=typeof hash==='string'&&/^sha256:[0-9a-f]{64}$/.test(hash)?'/camera/'+hash.slice(7)+'.png':null;
      const source=url && pixels && input.texture_url===url ? (options.resolveTextureURL?options.resolveTextureURL(url):url) : null;
      if(source && loadedImage?.getAttribute('src')===source &&
         input.image_width===texture?.width && input.image_height===texture.height){update('ready',loadedImage);return;}
      loadedImage=null;texture=null;
      if(!input){update('none');return;}
      if(!source){update('unavailable');return;}
      update('loading');
      const img=new Image();
      const current=()=>!disposed && load===textureLoad;
      img.onerror=()=>{if(current())update('unavailable');};
      img.onload=()=>{
        if(!current())return;
        if(img.naturalWidth!==input.image_width||img.naturalHeight!==input.image_height){update('unavailable');return;}
        try{
          const tex=document.createElement('canvas');
          tex.width=img.naturalWidth;tex.height=img.naturalHeight;
          const tx=tex.getContext('2d',{willReadFrequently:true});
          tx.drawImage(img,0,0);
          texture=tx.getImageData(0,0,tex.width,tex.height);
          loadedImage=img;update('ready',img);
        }catch{texture=null;update('unavailable');}
      };
      img.src=source;
    }
    function routeArrows(points){
      // Space direction markers by display distance, independently of the planner's sampling density.
      let remaining=48;
      for(let i=1;i<points.length;i++){
        const a=points[i-1], b=points[i],dx=b[0]-a[0],dy=b[1]-a[1],length=Math.hypot(dx,dy);
        if(length<.01)continue;
        const ux=dx/length,uy=dy/length;
        while(remaining<=length){
          const x=a[0]+ux*remaining,y=a[1]+uy*remaining;
          const bx=x-ux*7,by=y-uy*7;
          face(ctx,[[x+ux*2,y+uy*2],[bx-uy*4,by+ux*4],[bx+uy*4,by-ux*4]],'#f1ffda');
          remaining+=96;
        }
        remaining-=length;
      }
    }
    function trajectory(){
      const points=route().map(p=>project(p[0],p[1],.015));
      if(points.length<2||!layers.route)return null;
      // Draw as a plan annotation even when occupied cells would hide a ground line.
      const candidate=plan.status==='partial', dash=candidate?[10,8]:[];
      line(ctx,points,'#101e19',10);
      line(ctx,points,colors.route,5,dash);
      routeArrows(points);
      const end=points.at(-1);
      dot(ctx,end,6.5,'#143124',colors.route);dot(ctx,end,2.5,colors.route);
      return {p:end,text:candidate?'候选终点':'规划终点',tint:colors.route,ox:16,oy:-29};
    }
    function annotations(){
      const f=plan?.footprint, annotations=[];
      const end=trajectory();
      if(end)annotations.push(end);
      if(!f){placeLabels(annotations);return;}
      const hit=plan.front_surface_body_m;
      const blocked=plan.status==='blocked';
      if(layers.route && blocked && point(hit) && Number.isFinite(plan.front_clearance_to_surface_m)){
        const a=project(f.front_m,hit[1],.08), b=project(hit[0],hit[1],.08), ground=project(hit[0],hit[1]);
        line(ctx,[ground,b],colors.block,1.3,[2,3]);line(ctx,[a,b],'#273331',6);
        line(ctx,[a,b],colors.block,2.7);dot(ctx,a,2.8,colors.block);dot(ctx,b,4.4,'#1a2828',colors.block);
        annotations.push({p:b,text:'前方净距  '+plan.front_clearance_to_surface_m.toFixed(3)+' m',
          tint:colors.block,ox:22,oy:-47});
      }
      if(layers.route&&blocked)annotations.push({p:project(0,0,.12), text:'车体 · 保持原位',stop:true,tint:'#ffbd91',ox:-23,oy:39});
      else annotations.push({p:project(0,0,.12),text:'车体',tint:colors.vehicle,ox:-18,oy:33});
      if(selected!==null && layers.occupied && scene?.voxels?.[selected]){
        const [x,y,z,n]=scene.voxels[selected], p=project(x,y,z);
        dot(ctx,p,6,'#bddddd25','#effdf3');
        annotations.unshift({p,text:'体素  z '+z.toFixed(2)+' m · '+n+' 点',tint:'#e5f7e9',ox:24,oy:-30});
      }
      placeLabels(annotations);
    }
    function placeLabels(items){
      const used=[];
      const compact=width<400;
      const padding=compact?6:10;
      ctx.font='600 '+(compact?11:12)+'px '+cssFont;
      ctx.textBaseline='middle';
      for(const item of items){
        const {p,text,tint,ox,oy}=item;
        if(!onscreen(p,4))continue;
        const labelWidth=Math.ceil(ctx.measureText(text).width)+2*padding+(item.stop?15:0), labelHeight=25;
        const left=clamp(ox<0?p[0]+ox-labelWidth:p[0]+ox,6,width-labelWidth-6);
        let preferred=clamp(p[1]+oy-labelHeight*.5,6,height-labelHeight-9);
        const candidates=[preferred,preferred-31,preferred+31,preferred-62,preferred+62];
        const top=candidates.find(y=>y>=3&&y+labelHeight<=height-3&&
          used.every(box=>left+labelWidth<box[0]-4||box[0]+box[2]<left-4||y+labelHeight<box[1]-4||y>box[1]+box[3]+4));
        if(top===undefined)continue;
        used.push([left,top,labelWidth,labelHeight]);
        const ex=clamp(p[0],left+4,left+labelWidth-4),ey=clamp(p[1],top+1,top+labelHeight-1);
        line(ctx,[p,[ex,ey]],tint,1.15);
        ctx.fillStyle='#112123f3';ctx.strokeStyle=tint;ctx.lineWidth=.85;
        ctx.beginPath();ctx.roundRect(left,top,labelWidth,labelHeight,5);ctx.fill();ctx.stroke();
        let textX=left+padding;
        if(item.stop){
          ctx.fillStyle=tint;ctx.fillRect(textX,top+8,8,8);textX+=15;
        }
        ctx.fillStyle=tint;ctx.fillText(text,textX,top+labelHeight/2+.5);
      }
    }
    function ruler(){
      const length=scale>118?.5:scale<39?2:1;
      const from=16,bottom=height-15,span=length*scale;
      if(span+28>width*.55)return;
      ctx.font='11px ui-monospace, Consolas, monospace';ctx.textBaseline='bottom';
      ctx.fillStyle='#d1e1d8';
      line(ctx,[[from,bottom-5],[from,bottom],[from+span,bottom],[from+span,bottom-5]],'#b9ccc3',1.5);
      ctx.fillText(length+' m',from,bottom-6);
    }
    function draw(){
      frame=0;if(disposed)return;
      const bounds=canvas.getBoundingClientRect();
      width=bounds.width;height=bounds.height;
      if(width<2||height<2)return;
      const dpr=Math.min(2,window.devicePixelRatio||1);
      const w=Math.round(width*dpr), h=Math.round(height*dpr);
      if(canvas.width!==w||canvas.height!==h){canvas.width=w;canvas.height=h;}
      ctx.setTransform(dpr,0,0,dpr,0,0);ctx.lineJoin='round';ctx.lineCap='round';
      cameraAxes(); background();
      if(cameraOnly())drawCamera();else {occupation();annotations();}
      ruler();
    }
    function pan(dx,dy){
      if(scale<=0)return;
      if(cameraOnly()){
        center=[center[0]-dx*sin/scale+dy*cos*sinPitch/scale,center[1]+dx*cos/scale+dy*sin*sinPitch/scale];
        centerZ+=dy*cosPitch/scale;home=false;return;
      }
      const horiz=-dx/scale, forward=dy/(scale*Math.max(.16,sinPitch));
      center=[clamp(center[0]+horiz*sin+forward*cos,-100,100),clamp(center[1]-horiz*cos+forward*sin,-100,100)];
      home=false;
    }
    function orbit(dx,dy){
      view='orbit'; yaw=(yaw-dx*.009)%TAU;pitch=clamp(pitch+dy*.007,.26,1.54);
    }
    function on(type,handler,opts={}){canvas.addEventListener(type,handler,{...opts,signal:listeners.signal});}
    const coord=e=>[e.clientX,e.clientY];
    const dist=(a,b)=>Math.hypot(a[0]-b[0],a[1]-b[1]);
    function pick(e){
      const b=canvas.getBoundingClientRect(), tap=[e.clientX-b.left,e.clientY-b.top];
      selected=null;
      for(let i=pickable.length-1;i>=0;i--){
        if(dist(pickable[i].p,tap)<=pickable[i].r){selected=pickable[i].i;break;}
      }
      drawSoon();
    }
    on('pointerdown',e=>{
      if(e.button!==0&&e.button!==2&&e.button!==1)return;
      e.preventDefault(); canvas.focus({preventScroll:true});
      canvas.setPointerCapture(e.pointerId);touches.set(e.pointerId,coord(e));
      if(touches.size===1){born=coord(e);dragged=false;}else dragged=true;
      canvas.style.cursor='grabbing';
    });
    on('pointermove',e=>{
      const old=touches.get(e.pointerId);if(!old)return;
      const next=coord(e),dx=next[0]-old[0],dy=next[1]-old[1];
      if(!dragged&&born&&dist(next,born)<3)return;
      dragged=true;touches.set(e.pointerId,next);
      if(touches.size===2){
        const other=[...touches].find(([id])=>id!==e.pointerId)[1];
        const prior=dist(old,other);
        if(prior>5)zoom=clamp(zoom*dist(next,other)/prior,.35,5);
        pan(dx*.5,dy*.5);
      }else if(e.shiftKey || (e.buttons&6) || view==='top')pan(dx,dy);
      else orbit(dx,dy);
      changed();
    });
    function finish(e){
      if(!touches.has(e.pointerId))return;
      if(!dragged&&e.type==='pointerup'&&e.button===0)pick(e);
      touches.delete(e.pointerId);
      if(touches.size===0){canvas.style.cursor='grab';born=null;}
    }
    on('pointerup',finish);on('pointercancel',finish);on('lostpointercapture',finish);
    on('wheel',e=>{e.preventDefault();zoomBy(Math.exp(-clamp(e.deltaY,-160,160)*.0022));},{passive:false});
    on('dblclick',()=>reset()); on('contextmenu',e=>e.preventDefault());
    on('keydown',e=>{
      const dx=({ArrowLeft:-1,ArrowRight:1})[e.key]||0;
      const dy=({ArrowUp:-1,ArrowDown:1})[e.key]||0;
      if(dx||dy){
        if(e.shiftKey||view==='top')pan(dx*22,dy*22);else orbit(dx*10,dy*10);
        changed();
      }else if(e.key==='+'||e.key==='=')zoomBy(1.25);
      else if(e.key==='-')zoomBy(.8);
      else if(e.key==='Home')reset();
      else if(e.key==='Escape'){selected=null;drawSoon();}
      else return;
      e.preventDefault();
    });
    const observer=new ResizeObserver(drawSoon);observer.observe(canvas);
    changed();
    return {getState:state,setView,zoomBy,reset,
      setLayer(name,on){if(name in layers){layers[name]=Boolean(on);changed();}},
      setData(nextScene,nextPlan){
        const previousCamera=cameraOnly(), nextCamera=nextScene?.registration==='camera_only';
        const sameFrame=nextCamera||!nextPlan?.base_frame||!nextScene?.frame_id||nextPlan.base_frame===nextScene.frame_id;
        if(nextScene!==scene){mesh=cameraMesh(nextScene);useTexture(nextCamera && mesh?nextScene:null);}
        scene=sameFrame?nextScene:null;plan=nextPlan;
        if(previousCamera!==nextCamera){reset();return;}
        selected=null;if(home)fitTarget();changed();
      },
      dispose(){disposed=true;observer.disconnect();listeners.abort();cancelAnimationFrame(frame);touches.clear();}
    };
  }
  return {create};
})();
