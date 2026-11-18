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
  uniform float nearZ;
  uniform bool shadowPass;
  out vec2 texcoord;
  out vec3 worldNormal, shadowCoord;
  out vec4 tint;
  out float depth;
  void main() {
    vec4 world = model * vec4(position, 1.);
    vec4 light = lightSpace * world;
    vec3 delta = world.xyz - eye;
    depth = -dot(delta, away);
    gl_Position = shadowPass ? light : vec4(dot(delta,right)*focal.x,
      dot(delta,up)*focal.y, depth-2.*nearZ, depth);
    worldNormal = transpose(inverse(mat3(model))) * normal;
    shadowCoord = .5 + .5*light.xyz; texcoord = uv; tint = color;
  }`;
  const fragment = `#version 300 es
  precision highp float;
  precision highp int;
  in vec2 texcoord;
  in vec3 worldNormal, shadowCoord;
  in vec4 tint;
  in float depth;
  uniform vec4 diffuse;
  uniform sampler2D albedo, shadows;
  uniform bool shadowPass;
  uniform float cutoff;
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
    paint=vec4(color.rgb*light,1.);
    // Log depth keeps millimetre-scale field lines visible at campus-wide distances.
    gl_FragDepth=log2(1.+depth)/20.;
    uint bits=floatBitsToUint(1./depth);
    recordedDepth=vec4(uvec4(bits,bits>>8,bits>>16,bits>>24)&255u)/255.;
  }`;
  const identity = [1,0,0,0,0,1,0,0,0,0,1,0,0,0,0,1];
  const dot=(a,b)=>a.reduce((s,n,i)=>s+n*b[i],0);
  const unit=a=>a.map(v=>v/Math.hypot(...a));
  function program(gl) {
    const p=gl.createProgram();
    for(const [type,src] of [[gl.VERTEX_SHADER,vertex],[gl.FRAGMENT_SHADER,fragment]]){
      const s=gl.createShader(type); gl.shaderSource(s,src); gl.compileShader(s);
      if(!gl.getShaderParameter(s,gl.COMPILE_STATUS))throw Error(gl.getShaderInfoLog(s));
      gl.attachShader(p,s); gl.deleteShader(s);
    }
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
      for(const name of ["model","lightSpace","eye","right","up","away","focal","nearZ","shadowPass","diffuse","albedo","shadows","cutoff"])
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
      // The exporter normalizes the scene under one glTF Y-up root. Its children
      // are already complete ROS-world matrices, used by overlays and the camera.
      const root=doc.nodes[doc.scenes[doc.scene].nodes[0]];
      if(root.name!=="ROS world" || !root.children)throw Error("Missing ROS world transform");
      this.draws=[];const bounds=[[Infinity,Infinity,Infinity],[-Infinity,-Infinity,-Infinity]];
      for(const id of root.children){
        const node=doc.nodes[id], matrix=node.matrix||identity;
        for(const prim of meshes[node.mesh]){
          this.draws.push({prim,matrix});
          if(node.name==="floor")continue;
          for(const x of [prim.box.min[0],prim.box.max[0]])for(const y of [prim.box.min[1],prim.box.max[1]])for(const z of [prim.box.min[2],prim.box.max[2]]){
            for(let i=0;i<3;i++){
              const v=matrix[i]*x+matrix[4+i]*y+matrix[8+i]*z+matrix[12+i];
              bounds[0][i]=Math.min(bounds[0][i],v);bounds[1][i]=Math.max(bounds[1][i],v);
            }
          }
        }
      }
      this.triangles=this.draws.reduce((n,d)=>n+d.prim.count/3,0);
      if(!this.draws.length)throw Error("Empty campus mesh");
      this.shadowMap(bounds);
      this.state="ready";this.invalidate();
    }
    drawModels(shadow){
      const gl=this.gl,u=this.u;
      gl.uniform1i(u.shadowPass,+shadow);gl.activeTexture(gl.TEXTURE0);
      // Generic values apply to disabled arrays in the bound VAOs.
      gl.vertexAttrib2f(this.a.uv,0,0);gl.vertexAttrib4f(this.a.color,1,1,1,1);
      for(const {prim,matrix} of this.draws){
        gl.uniformMatrix4fv(u.model,false,matrix); gl.uniform4fv(u.diffuse,prim.color);
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
    paint(surface,cam,near,id){
      try{
        this.target(surface.w,surface.h);
        const key=[surface.w,surface.h,surface.focal,...cam.target,cam.distance,...cam.up,...cam.right].join(",");
        if(key!==this.cacheKey){
          const gl=this.gl,u=this.u,w=this.w,h=this.h;
          gl.bindFramebuffer(gl.FRAMEBUFFER,this.fbo);gl.viewport(0,0,w,h);
          gl.clearColor(0,0,0,0);gl.clear(gl.COLOR_BUFFER_BIT|gl.DEPTH_BUFFER_BIT);
          gl.uniform3fv(u.eye,cam.target.map((n,i)=>n+cam.distance*cam.out[i]));
          gl.uniform3fv(u.right,cam.right);gl.uniform3fv(u.up,cam.up);gl.uniform3fv(u.away,cam.out);
          gl.uniform2f(u.focal,2*surface.focal/w,2*surface.focal/h);gl.uniform1f(u.nearZ,near);
          gl.activeTexture(gl.TEXTURE1);gl.bindTexture(gl.TEXTURE_2D,this.shadow);
          this.drawModels(false);
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
        for(let i=0;i<surface.depth.length;i++)if(surface.depth[i]>0)surface.ids[i]=id;
        return true;
      }catch(error){
        this.state="failed";this.reason=String(error.message);this.retryAt=Date.now()+30000;
        this.invalidate();return false;
      }
    }
  }
  window.createCampusMesh=invalidate=>new CampusMesh(invalidate);
})();
