/* Offline WebGL2 viewer for static GLBs emitted by the Gazebo scene plugin.
   The depth attachment is shared with the existing ROS overlay/picking raster. */
(() => {
  "use strict";
  const vertex = `#version 300 es
  in vec3 position, normal;
  in vec2 uv;
  in vec4 color;
  uniform mat4 model, lightSpace;
  uniform vec3 eye, right, up, away;
  uniform vec2 focal;
  uniform vec4 sheetBounds;
  uniform float nearZ;
  uniform bool shadowPass;
  out vec2 texcoord;
  out vec3 worldNormal, shadowCoord;
  out vec4 tint;
  out float depth, altitude;
  void main() {
    vec4 world = model * vec4(position, 1.);
    vec4 light = lightSpace * world;
    vec3 delta = world.xyz - eye;
    depth = -dot(delta, away);
    altitude = world.z;
    gl_Position = shadowPass ? light : vec4(dot(delta,right)*focal.x,
      dot(delta,up)*focal.y, depth-2.*nearZ, depth);
    worldNormal = transpose(inverse(mat3(model))) * normal;
    shadowCoord = .5 + .5*light.xyz;
    texcoord = sheetBounds.z>0. ? (position.xy-sheetBounds.xy)/sheetBounds.zw : uv;
    tint = color;
  }`;
  const fragment = `#version 300 es
  precision highp float;
  precision highp int;
  in vec2 texcoord;
  in vec3 worldNormal, shadowCoord;
  in vec4 tint;
  in float depth, altitude;
  uniform vec4 diffuse;
  uniform highp sampler2D albedo, shadows;
  uniform bool shadowPass;
  uniform float cutoff;
  uniform int objectTag;
  uniform vec3 eye, up, away;
  uniform vec2 framebufferSize, focal;
  layout(location=0) out vec4 paint;
  layout(location=1) out vec4 recordedDepth;
  void main() {
    vec4 color = texture(albedo,texcoord)*diffuse*tint;
    if (color.a < cutoff) discard;
    if (shadowPass) {
      gl_FragDepth=gl_FragCoord.z; paint=vec4(0.); recordedDepth=vec4(0.); return;
    }
    vec3 n=normalize(worldNormal)*(gl_FrontFacing?1.:-1.);
    float lambert=max(0.,dot(n,normalize(vec3(.43,-.50,.85))));
    float shadow=0.;
    if (all(greaterThan(shadowCoord,vec3(.001))) && all(lessThan(shadowCoord,vec3(.999)))) {
      vec2 pixel=1./vec2(textureSize(shadows,0));
      float bias=.00022+.00075*(1.-lambert);
      for(int j=-1;j<=1;++j) for(int i=-1;i<=1;++i) {
        float closest=texture(shadows,shadowCoord.xy+pixel*vec2(i,j)).r;
        shadow+=float(shadowCoord.z-bias>closest)/9.;
      }
    }
    vec3 light=vec3(.65,.69,.73)+vec3(.39,.36,.29)*lambert*(1.-shadow*.90);
    // IDs share one byte in the offscreen attachment; the display copy restores
    // fully opaque pixels and registers the corresponding robot for picking.
    paint=vec4(color.rgb*light,1.-float(objectTag)/255.);
    // Log depth keeps millimetre-scale field lines visible at campus-wide distances.
    // Large coplanar quads clipped by the near plane may lose centimetres in
    // interpolated depth. Intersect level surfaces with the camera ray exactly.
    float rayZ=-away.z+up.z*(2.*gl_FragCoord.y/framebufferSize.y-1.)/focal.y;
    bool level=abs(dFdx(altitude))+abs(dFdy(altitude))<.00001 && abs(n.z)>.9999;
    float exactDepth=level && abs(rayZ)>.000001 ? (altitude-eye.z)/rayZ : depth;
    gl_FragDepth=log2(1.+exactDepth)/20.;
    uint bits=floatBitsToUint(1./exactDepth);
    recordedDepth=vec4(uvec4(bits,bits>>8,bits>>16,bits>>24)&255u)/255.;
  }`;
  const identity = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
  const matrixProduct=(a,b)=>Array.from({length:16},(_,i)=>{
    const row=i%4,col=i-row;
    return a[row]*b[col]+a[4+row]*b[col+1]+a[8+row]*b[col+2]+a[12+row]*b[col+3];
  });
  const apply=(m,p)=>[0,1,2].map(i=>m[i]*p[0]+m[4+i]*p[1]+m[8+i]*p[2]+m[12+i]);
  function inverseRigid(m){
    const out=[m[0],m[4],m[8],0,m[1],m[5],m[9],0,m[2],m[6],m[10],0,0,0,0,1];
    const shift=apply(out,[-m[12],-m[13],-m[14]]);out.splice(12,3,...shift);return out;
  }
  function rigid(xyz,q){
    const n=Math.hypot(...q);
    if(!Number.isFinite(n)||n<1e-8)return null;
    const [x,y,z,w]=q.map(v=>v/n);
    return [1-2*(y*y+z*z),2*(x*y+z*w),2*(x*z-y*w),0,
      2*(x*y-z*w),1-2*(x*x+z*z),2*(y*z+x*w),0,
      2*(x*z+y*w),2*(y*z-x*w),1-2*(x*x+y*y),0,...xyz,1];
  }
  function loadRobotTemplates(doc,meshes){
    const templates=new Map();
    for(const scene of doc.scenes){
      const model=scene.extras?.robot_model;
      if(!model)continue;
      const links=new Map(), joints=new Map((scene.extras.joints||[]).map(j=>[j.child,j]));
      const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];
      for(const i of doc.nodes[scene.nodes[0]].children){
        const link=doc.nodes[i],draws=[];
        for(const id of link.children||[]){
          const node=doc.nodes[id];
          for(const prim of meshes[node.mesh]){
            const matrix=node.matrix||identity,rest=matrixProduct(link.matrix,matrix);
            draws.push({prim,matrix});
            for(const x of [prim.box.min[0],prim.box.max[0]])for(const y of [prim.box.min[1],prim.box.max[1]])for(const z of [prim.box.min[2],prim.box.max[2]]){
              const pos=apply(rest,[x,y,z]);pos.forEach((v,k)=>{low[k]=Math.min(low[k],v);high[k]=Math.max(high[k],v);});
            }
          }
        }
        links.set(link.name,{rest:link.matrix,draws,joint:joints.get(link.name)});
      }
      if(low.every(Number.isFinite)){
        const center=low.map((v,i)=>(v+high[i])/2);
        templates.set(model,{links,center,radius:Math.hypot(...low.map((v,i)=>v-center[i]))});
      }
    }
    return templates;
  }
  function robotDraws(template,robot,tag){
    const world=rigid(robot.xyz,robot.quaternion),found=new Map(),records=[];
    if(!world)return records;
    function linkPose(name){
      if(found.has(name))return found.get(name);
      const link=template.links.get(name),j=link.joint,measured=robot.joints?.[j?.name];
      let frame=link.rest;
      if(j && template.links.has(j.parent)){
        const parent=template.links.get(j.parent);
        frame=matrixProduct(matrixProduct(linkPose(j.parent),inverseRigid(parent.rest)),frame);
      }
      if(j && Number.isFinite(measured) && j.type!=="fixed"){
        const axis=unit(j.axis);
        const delta=j.type==="prismatic"?rigid(axis.map(a=>a*measured),[0,0,0,1]):
          rigid([0,0,0],[...axis.map(a=>a*Math.sin(measured/2)),Math.cos(measured/2)]);
        frame=matrixProduct(matrixProduct(matrixProduct(frame,j.origin),delta),inverseRigid(j.origin));
      }
      found.set(name,frame);return frame;
    }
    for(const [name,link] of template.links){
      const body=matrixProduct(world,linkPose(name));
      for(const part of link.draws)records.push({prim:part.prim,matrix:matrixProduct(body,part.matrix),
        middle:robot.xyz,radius:2*template.radius,tag,castsShadow:false});
    }
    return records;
  }
  const dot=(a,b)=>a.reduce((s,n,i)=>s+n*b[i],0);
  const unit=a=>a.map(v=>v/Math.hypot(...a));
  function program(gl,vert=vertex,frag=fragment,location=null) {
    const p=gl.createProgram();
    for(const [type,src] of [[gl.VERTEX_SHADER,vert],[gl.FRAGMENT_SHADER,frag]]){
      const s=gl.createShader(type); gl.shaderSource(s,src); gl.compileShader(s);
      if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));
      gl.attachShader(p,s); gl.deleteShader(s);
    }
    if(location!==null)gl.bindAttribLocation(p,location,"position");
    gl.linkProgram(p);
    if(!gl.getProgramParameter(p,gl.LINK_STATUS))throw Error(gl.getProgramInfoLog(p));
    gl.useProgram(p);
    return p;
  }

  function readGLB(data) {
    const v=new DataView(data);
    if(v.getUint32(0,true)!==0x46546c67 || v.getUint32(4,true)!==2 ||
       v.getUint32(8,true)!==data.byteLength || v.getUint32(16,true)!==0x4e4f534a)throw Error("Invalid GLB");
    const n=v.getUint32(12,true), start=28+n;
    const doc=JSON.parse(new TextDecoder().decode(new Uint8Array(data,20,n)));
    if(v.getUint32(start-4,true)!==0x004e4942 || start+v.getUint32(start-8,true)!==data.byteLength)throw Error("Invalid GLB buffer");
    return {doc, bytes(index) {
      const view=doc.bufferViews[index];
      if(view.buffer!==0)throw Error("External GLB buffer");
      return new Uint8Array(data,start+(view.byteOffset||0),view.byteLength);
    }};
  }

  class CampusMesh {
    constructor(invalidate){
      this.invalidate=invalidate; this.url=null; this.state="idle"; this.released=false;
    }
    configure(info) {
      // Disable rendering immediately on edition/frame/toggle changes; keep one edition in memory.
      if(!info?.mesh_url)return;
      const url=new URL(info.mesh_url,location.href);
      if(url.origin!==location.origin){this.state="failed";return;}
      if(this.url===url.href) {
        if(this.state!=="failed" || Date.now()<this.retryAt)return;
      }
      this.dispose();
      this.url=url.href; this.state="loading"; this.reason="";
      const ticket=this.ticket=new AbortController();
      this.load(ticket).catch(error=>{
        if(this.ticket!==ticket)return;
        this.state="failed";this.reason=String(error.message);
        this.retryAt=Date.now()+30000;this.invalidate();
      });
    }
    dispose(){
      this.ticket?.abort();
      if(this.gl && !this.gl.isContextLost()){
        for(const [method,resource] of this.resources||[])this.gl[method](resource);
        this.deleteTarget();
      }
      this.resources=[];this.cacheKey="";this.w=0;this.draws=[];this.ticket=null;
    }
    resource(type) {
      const value=this.gl["create"+type]();
      this.resources.push(["delete"+type,value]);
      return value;
    }
    async load(ticket){
      if(!this.canvas){
        this.canvas=document.createElement("canvas");
        this.gl=this.canvas.getContext("webgl2",{alpha:true,antialias:false,preserveDrawingBuffer:false});
        this.canvas.addEventListener("webglcontextlost",e=>{
          e.preventDefault();this.ticket?.abort();this.ticket=null;
          this.state="unavailable";this.invalidate();
        });
        this.canvas.addEventListener("webglcontextrestored",()=>{
          this.gl=this.canvas.getContext("webgl2");this.resources=[];this.fbo=null;
          this.url=null;this.state="idle";this.invalidate();
        });
      }
      const gl=this.gl;
      if(!gl){this.state="unavailable";this.invalidate();return;}
      const response=await fetch(this.url,{signal:ticket.signal,cache:"default"});
      if(!response.ok)throw Error("GLB HTTP "+response.status);
      const file=readGLB(await response.arrayBuffer());
      if(this.ticket!==ticket)return;
      const doc=file.doc;
      const p=this.p=program(gl);this.resources.push(["deleteProgram",p]);
      this.u={};this.a={};
      for(const name of ["model","lightSpace","eye","right","up","away","focal","sheetBounds","framebufferSize","nearZ","shadowPass","diffuse","albedo","shadows","cutoff","objectTag"])
        this.u[name]=gl.getUniformLocation(p,name);
      for(const name of ["position","normal","uv","color"])this.a[name]=gl.getAttribLocation(p,name);
      this.white=this.resource("Texture");
      gl.bindTexture(gl.TEXTURE_2D,this.white);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,1,1,0,gl.RGBA,gl.UNSIGNED_BYTE,new Uint8Array([255,255,255,255]));
      gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR);
      this.images=[];
      // Decode sequentially so rendering never retains an entire second set of bitmaps.
      for(const image of doc.images||[]){
        const bitmap=await createImageBitmap(new Blob([file.bytes(image.bufferView)],{type:image.mimeType}),
          {colorSpaceConversion:"none",premultiplyAlpha:"none"});
        try{
          if(this.ticket!==ticket)return;
          const texture=this.resource("Texture");
          gl.bindTexture(gl.TEXTURE_2D,texture);
          gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA,gl.RGBA,gl.UNSIGNED_BYTE,bitmap);
          gl.generateMipmap(gl.TEXTURE_2D);
          gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR_MIPMAP_LINEAR);
          const af=gl.getExtension("EXT_texture_filter_anisotropic");
          if(af)gl.texParameterf(gl.TEXTURE_2D,af.TEXTURE_MAX_ANISOTROPY_EXT,Math.min(8,gl.getParameter(af.MAX_TEXTURE_MAX_ANISOTROPY_EXT)));
          this.images.push(texture);
        }finally{bitmap.close();}
      }
      const buffers=new Map();
      function count(type){return {SCALAR:1,VEC2:2,VEC3:3,VEC4:4}[type];}
      const bind=(accessor,target)=>{
        const a=doc.accessors[accessor];
        if(!buffers.has(a.bufferView)){
          const b=this.resource("Buffer");
          gl.bindBuffer(target,b);gl.bufferData(target,file.bytes(a.bufferView),gl.STATIC_DRAW);
          buffers.set(a.bufferView,b);
        }else gl.bindBuffer(target,buffers.get(a.bufferView));
        return a;
      };
      const meshes=doc.meshes.map(mesh=>mesh.primitives.map(prim=>{
        const vao=this.resource("VertexArray");gl.bindVertexArray(vao);
        for(const [key,name] of [["POSITION","position"],["NORMAL","normal"],["TEXCOORD_0","uv"],["COLOR_0","color"]]){
          if(prim.attributes[key]===undefined)continue;
          const accessor=bind(prim.attributes[key],gl.ARRAY_BUFFER),loc=this.a[name];
          gl.enableVertexAttribArray(loc);
          gl.vertexAttribPointer(loc,count(accessor.type),accessor.componentType,!!accessor.normalized,
            doc.bufferViews[accessor.bufferView].byteStride||0,accessor.byteOffset||0);
        }
        const indices=bind(prim.indices,gl.ELEMENT_ARRAY_BUFFER);
        const mat=doc.materials[prim.material],material=mat.pbrMetallicRoughness;
        const tex=material.baseColorTexture;
        return {vao, mode:indices.componentType, count:indices.count, offset:indices.byteOffset||0,
          tex:tex?this.images[doc.textures[tex.index].source]:this.white,color:material.baseColorFactor,
          cutoff:mat.alphaMode==="MASK"?(mat.alphaCutoff??.5):0,box:doc.accessors[prim.attributes.POSITION]};
      }));
      for(const primitives of meshes)this.bakePlanarPaint(primitives);
      this.templates=loadRobotTemplates(doc,meshes);
      // The exporter normalizes the scene under one glTF Y-up root. Its children
      // are already complete ROS-world matrices, used by overlays and the camera.
      const root=doc.nodes[doc.scenes[doc.scene].nodes[0]];
      if(root.name!=="ROS world" || !root.children)throw Error("Missing ROS world transform");
      this.draws=[];const bounds=[[Infinity,Infinity,Infinity],[-Infinity,-Infinity,-Infinity]];
      for(const id of root.children){
        const node=doc.nodes[id], matrix=node.matrix||identity;
        for(const prim of meshes[node.mesh]){
          const low=[Infinity,Infinity,Infinity],high=[-Infinity,-Infinity,-Infinity];
          for(const x of [prim.box.min[0],prim.box.max[0]])for(const y of [prim.box.min[1],prim.box.max[1]])for(const z of [prim.box.min[2],prim.box.max[2]]){
            for(let i=0;i<3;i++){
              const v=matrix[i]*x+matrix[4+i]*y+matrix[8+i]*z+matrix[12+i];
              low[i]=Math.min(low[i],v);high[i]=Math.max(high[i],v);
            }
          }
          const middle=low.map((v,i)=>(v+high[i])/2);
          this.draws.push({prim,matrix,middle,radius:Math.hypot(...low.map((v,i)=>v-middle[i])),
            castsShadow:high[2]-low[2]>.12});
          if(node.name!=="floor")for(let i=0;i<3;i++){
            bounds[0][i]=Math.min(bounds[0][i],low[i]);bounds[1][i]=Math.max(bounds[1][i],high[i]);
          }
        }
      }
      this.triangles=this.draws.reduce((n,d)=>n+d.prim.count/3,0);
      if(!this.draws.length)throw Error("Empty campus mesh");
      this.shadowMap(bounds);
      this.state="ready";this.invalidate();
    }
    // Thin field markings and other layered, horizontal paint can be narrower
    // than one display pixel. Bake the source triangles in local XY once so GPU
    // mipmaps preserve their average coverage instead of producing dotted lines.
    bakePlanarPaint(prims){
      if(prims.length<2 || prims.some(p=>p.tex!==this.white))return;
      const lo=[0,1,2].map(i=>Math.min(...prims.map(p=>p.box.min[i])));
      const hi=[0,1,2].map(i=>Math.max(...prims.map(p=>p.box.max[i])));
      const size=hi.map((v,i)=>v-lo[i]);
      if(Math.min(size[0],size[1])<2 || size[2]>.15 || size[2]>.002*Math.min(size[0],size[1]))return;
      const gl=this.gl,limit=Math.min(gl.getParameter(gl.MAX_TEXTURE_SIZE),4096);
      const scale=limit/Math.max(size[0],size[1]),width=Math.ceil(size[0]*scale),height=Math.ceil(size[1]*scale);
      const bounds=[lo[0],lo[1],size[0],size[1]],texture=this.resource("Texture");
      gl.bindTexture(gl.TEXTURE_2D,texture);
      gl.texImage2D(gl.TEXTURE_2D,0,gl.RGBA8,width,height,0,gl.RGBA,gl.UNSIGNED_BYTE,null);
      const fb=gl.createFramebuffer(),zbuf=gl.createRenderbuffer();
      gl.bindFramebuffer(gl.FRAMEBUFFER,fb);gl.bindRenderbuffer(gl.RENDERBUFFER,zbuf);
      gl.renderbufferStorage(gl.RENDERBUFFER,gl.DEPTH_COMPONENT16,width,height);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER,gl.DEPTH_ATTACHMENT,gl.RENDERBUFFER,zbuf);
      gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0,gl.TEXTURE_2D,texture,0);
      const vert=`#version 300 es
        in vec3 position;
        uniform vec4 bounds;
        uniform vec2 heights;
        void main(){gl_Position=vec4(2.*(position.xy-bounds.xy)/bounds.zw-1.,
          .9-1.8*(position.z-heights.x)/heights.y,1.);}`;
      const frag=`#version 300 es
        precision highp float;
        uniform vec4 diffuse;
        out vec4 color;
        void main(){color=diffuse;}`;
      let p;
      try{
        if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)return;
        p=program(gl,vert,frag,this.a.position);
        gl.uniform4fv(gl.getUniformLocation(p,"bounds"),bounds);
        gl.uniform2f(gl.getUniformLocation(p,"heights"),lo[2],Math.max(.001,size[2]));
        const diffuse=gl.getUniformLocation(p,"diffuse");
        gl.viewport(0,0,width,height);gl.enable(gl.DEPTH_TEST);
        gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
        for(const prim of prims){
          gl.uniform4fv(diffuse,prim.color);gl.bindVertexArray(prim.vao);
          gl.drawElements(gl.TRIANGLES,prim.count,prim.mode,prim.offset);
        }
        gl.bindFramebuffer(gl.FRAMEBUFFER,null);
        gl.bindTexture(gl.TEXTURE_2D,texture);gl.generateMipmap(gl.TEXTURE_2D);
        gl.texParameteri(gl.TEXTURE_2D,gl.TEXTURE_MIN_FILTER,gl.LINEAR_MIPMAP_LINEAR);
        for(const k of [gl.TEXTURE_WRAP_S,gl.TEXTURE_WRAP_T])gl.texParameteri(gl.TEXTURE_2D,k,gl.CLAMP_TO_EDGE);
        const af=gl.getExtension("EXT_texture_filter_anisotropic");
        if(af)gl.texParameterf(gl.TEXTURE_2D,af.TEXTURE_MAX_ANISOTROPY_EXT,Math.min(8,gl.getParameter(af.MAX_TEXTURE_MAX_ANISOTROPY_EXT)));
        for(const prim of prims){prim.sheet=bounds;prim.color=[1,1,1,1];prim.tex=texture;}
      }finally{
        gl.bindVertexArray(null);gl.bindFramebuffer(gl.FRAMEBUFFER,null);gl.deleteFramebuffer(fb);gl.deleteRenderbuffer(zbuf);
        if(p)gl.deleteProgram(p);gl.useProgram(this.p);
      }
    }
    drawModels(shadow,cam=null,draws=this.draws){
      const gl=this.gl,u=this.u;
      gl.uniform1i(u.shadowPass,+shadow);gl.activeTexture(gl.TEXTURE0);
      // Generic values apply to disabled arrays in the bound VAOs.
      gl.vertexAttrib2f(this.a.uv,0,0);gl.vertexAttrib4f(this.a.color,1,1,1,1);
      for(const {prim,matrix,middle,radius,castsShadow,tag} of draws){
        if(shadow && !castsShadow)continue;
        if(cam){
          // Reject whole materials/instances outside the eye frustum before submitting triangles.
          const delta=middle.map((v,i)=>v-cam.target[i]),z=cam.distance-dot(delta,cam.out);
          if(z+radius<=0 || Math.abs(dot(delta,cam.right))>z*cam.tanX+radius*Math.hypot(1,cam.tanX) ||
             Math.abs(dot(delta,cam.up))>z*cam.tanY+radius*Math.hypot(1,cam.tanY))continue;
        }
        gl.uniformMatrix4fv(u.model,false,matrix); gl.uniform4fv(u.diffuse,prim.color);
        gl.uniform1i(u.objectTag,tag||0);
        gl.uniform4fv(u.sheetBounds,prim.sheet||[0,0,0,0]);
        gl.uniform1f(u.cutoff,prim.cutoff);gl.bindTexture(gl.TEXTURE_2D,prim.tex);
        gl.bindVertexArray(prim.vao);gl.drawElements(gl.TRIANGLES,prim.count,prim.mode,prim.offset);
      }
      gl.bindVertexArray(null);
    }
    shadowMap([lo,hi]){
      const gl=this.gl,u=this.u,size=Math.min(2048,gl.getParameter(gl.MAX_TEXTURE_SIZE));
      const l=unit([.43,-.50,.85]), r=unit([-l[1],l[0],0]), up=unit([-r[1]*l[2],r[0]*l[2],r[1]*l[0]-r[0]*l[1]]);
      const rows=[r,up,l.map(n=>-n)],m=new Array(16).fill(0);m[15]=1;
      for(let i=0;i<3;i++){
        const corners=[];
        for(const x of [lo[0],hi[0]])for(const y of [lo[1],hi[1]])for(const z of [lo[2],hi[2]])corners.push(dot(rows[i],[x,y,z]));
        const a=Math.min(...corners)-3,b=Math.max(...corners)+3;
        for(let j=0;j<3;j++)m[4*j+i]=2*rows[i][j]/(b-a);
        m[12+i]=-(a+b)/(b-a);
      }
      this.lightSpace=m;
      this.shadow=this.resource("Texture");gl.bindTexture(gl.TEXTURE_2D,this.shadow);
      gl.texStorage2D(gl.TEXTURE_2D,1,gl.DEPTH_COMPONENT24,size,size);
      for(const p of [gl.TEXTURE_MAG_FILTER,gl.TEXTURE_MIN_FILTER])gl.texParameteri(gl.TEXTURE_2D,p,gl.NEAREST);
      for(const p of [gl.TEXTURE_WRAP_S,gl.TEXTURE_WRAP_T])gl.texParameteri(gl.TEXTURE_2D,p,gl.CLAMP_TO_EDGE);
      const fb=this.resource("Framebuffer");gl.bindFramebuffer(gl.FRAMEBUFFER,fb);
      gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.DEPTH_ATTACHMENT,gl.TEXTURE_2D,this.shadow,0);
      gl.drawBuffers([gl.NONE]);gl.readBuffer(gl.NONE);
      if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)throw Error("Shadow framebuffer unavailable");
      gl.useProgram(this.p);gl.uniformMatrix4fv(u.lightSpace,false,m);
      gl.uniform1i(u.albedo,0);gl.uniform1i(u.shadows,1);
      gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D,this.white);
      gl.viewport(0,0,size,size);gl.enable(gl.DEPTH_TEST);gl.disable(gl.DITHER);
      gl.clear(gl.DEPTH_BUFFER_BIT);this.drawModels(true);
    }
    deleteTarget(){
      const gl=this.gl;
      gl.deleteFramebuffer(this.fbo||null);gl.deleteRenderbuffer(this.zbuf||null);
      for(const t of this.attachments||[])gl.deleteTexture(t);
      this.fbo=null;this.attachments=[];
    }
    target(w,h){
      if(w===this.w&&h===this.h&&this.fbo)return;
      const gl=this.gl;this.deleteTarget();this.w=w;this.h=h;
      this.fbo=gl.createFramebuffer();gl.bindFramebuffer(gl.FRAMEBUFFER,this.fbo);
      this.attachments=[0,1].map(i=>{
        const t=gl.createTexture();gl.bindTexture(gl.TEXTURE_2D,t);
        gl.texStorage2D(gl.TEXTURE_2D,1,gl.RGBA8,w,h);
        gl.framebufferTexture2D(gl.FRAMEBUFFER,gl.COLOR_ATTACHMENT0+i,gl.TEXTURE_2D,t,0);
        return t;
      });
      this.zbuf=gl.createRenderbuffer();gl.bindRenderbuffer(gl.RENDERBUFFER,this.zbuf);
      gl.renderbufferStorage(gl.RENDERBUFFER,gl.DEPTH_COMPONENT24,w,h);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER,gl.DEPTH_ATTACHMENT,gl.RENDERBUFFER,this.zbuf);
      gl.drawBuffers([gl.COLOR_ATTACHMENT0,gl.COLOR_ATTACHMENT1]);
      if(gl.checkFramebufferStatus(gl.FRAMEBUFFER)!==gl.FRAMEBUFFER_COMPLETE)throw Error("Color framebuffer unavailable");
      this.rgba=new Uint8Array(w*h*4);this.packed=new Uint8Array(w*h*4);
      this.color=new Uint32Array(this.rgba.buffer);this.depth=new Float32Array(this.packed.buffer);
      this.cachedColor=new Uint32Array(w*h);this.cachedDepth=new Float32Array(w*h);
    }
    paint(surface,cam,near,id,robots=[],showWorld=true){
      try{
        this.target(surface.w,surface.h);
        const key=[surface.w,surface.h,surface.focal,...cam.target,cam.distance,...cam.up,...cam.right,showWorld,
          ...robots.map(r=>JSON.stringify([r.model_name,r.xyz,r.quaternion,r.joints]))].join(",");
        if(key!==this.cacheKey){
          const gl=this.gl,u=this.u,w=this.w,h=this.h;
          gl.bindFramebuffer(gl.FRAMEBUFFER,this.fbo);gl.viewport(0,0,w,h);
          gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
          gl.uniform3fv(u.eye,cam.target.map((n,i)=>n+cam.distance*cam.out[i]));
          gl.uniform3fv(u.right,cam.right);gl.uniform3fv(u.up,cam.up);gl.uniform3fv(u.away,cam.out);
          gl.uniform2f(u.focal,2*surface.focal/w,2*surface.focal/h);gl.uniform1f(u.nearZ,near);
          gl.uniform2f(u.framebufferSize,w,h);
          gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D,this.shadow);
          const view={...cam,tanX:w/(2*surface.focal),tanY:h/(2*surface.focal)};
          if(showWorld)this.drawModels(false,view);
          for(let i=0;i<robots.length;i++){
            const template=this.templates.get(robots[i].model_name);
            if(template)this.drawModels(false,view,robotDraws(template,robots[i],i+1));
          }
          gl.readBuffer(gl.COLOR_ATTACHMENT0);gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,this.rgba);
          gl.readBuffer(gl.COLOR_ATTACHMENT1);gl.readPixels(0,0,w,h,gl.RGBA,gl.UNSIGNED_BYTE,this.packed);
          for(let j=0;j<h;j++){
            const row=(h-j-1)*w;
            this.cachedColor.set(this.color.subarray(row,row+w),j*w);
            this.cachedDepth.set(this.depth.subarray(row,row+w),j*w);
          }
          this.cacheKey=key;
        }
        surface.pixels.set(this.cachedColor);surface.depth.set(this.cachedDepth);
        const robotPixels=new Uint32Array(robots.length);
        for(let i=0;i<surface.depth.length;i++)if(surface.depth[i]>0){
          const tag=255-(surface.pixels[i]>>>24);
          surface.ids[i]=tag?(robots[tag-1]?.pickId||id):id;
          if(tag)robotPixels[tag-1]++;
          surface.pixels[i]|=0xff000000;
        }
        this.robotPixels=Object.fromEntries(robots.map((r,i)=>[r.id,robotPixels[i]]));
        return true;
      }catch(error){
        this.state="failed";this.reason=String(error.message);this.retryAt=Date.now()+30000;
        this.invalidate();return false;
      }
    }
  }
  window.createCampusMesh=invalidate=>new CampusMesh(invalidate);
})();
