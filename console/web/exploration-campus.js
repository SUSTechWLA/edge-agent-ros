/* Use the appearance pack from this Gazebo world and bind task overlays to its published edition. */
function createCampusAtlas(redraw) {
  let info, texture, renderer, scripts, scheduled=false, nextAttempt=0, registration=null;
  const editions = new Map();
  const invalidate = () => {
    if (!scheduled) requestAnimationFrame(() => {scheduled=false; redraw();});
    scheduled=true;
  };
  function script(name) {
    return new Promise((resolve,reject) => {
      const tag=document.createElement('script');
      tag.src='/campus-viewer/'+name+'.js'; tag.onload=resolve;
      tag.onerror=()=>{tag.remove();reject(Error('校园渲染模块未加载'));};
      document.head.append(tag);
    });
  }
  async function start() {
    if (info || nextAttempt > Date.now()) return;
    nextAttempt=Date.now()+30000;
    try {
      const response=await fetch('/api/campus-atlas',{signal:AbortSignal.timeout(4500)});
      if (!response.ok) return;
      const edition=await response.json();
      if (edition.status!=='ready') return;
      if (!window.createMapTexture) await script('texture');
      texture=window.createMapTexture(invalidate);
      info=edition;
      const credits=document.getElementById('campus-credits');
      for(const {label,url} of info.attribution){
        const link=document.createElement('a');link.textContent=label;link.href=url;
        link.target='_blank';link.rel='noopener noreferrer';
        if(credits.childNodes.length)credits.append(' · ');
        credits.append(link);
      }
      invalidate();
    } catch { /* The task grid remains usable. Retry transient startup or network failures. */ }
  }
  function registered(mission) {
    const ref=mission?.map_ref;
    if (!info || !ref?.revision || !ref.digest || !ref.id || ref.frame!==info.frame ||
        mission.grid?.frame!==info.frame) return false;
    const key=JSON.stringify([ref.id,ref.revision,ref.digest,ref.frame]);
    const entry=editions.get(key);
    if (!entry || (!entry.pending && !entry.ok && Date.now()>entry.retry)) {
      const state={pending:true,ok:false,retry:0};editions.set(key,state);
      const load=mission.kind==='map_import' && mission.id===ref.id ? Promise.resolve(mission) :
        api('/api/exploration-mission?task_id='+encodeURIComponent(ref.id)).then(r=>r.mission);
      load.then(publication=>{
        const r=publication?.map_ref, evidence=publication?.evidence;
        state.ok=publication?.kind==='map_import' && publication.status==='SUCCEEDED' &&
          r?.id===ref.id && r?.revision===ref.revision && r?.digest===ref.digest && r?.frame===ref.frame &&
          evidence?.map_id===ref.id && evidence.scene_revision===info.scene_revision &&
          evidence.layout_sha256===info.layout_sha256 && evidence.world_sha256===info.world_sha256;
      }).catch(()=>{}).finally(()=>{state.pending=false;state.retry=Date.now()+30000;invalidate();});
    }
    return editions.get(key).ok;
  }
  function prepare(mission) {
    if(!info) {void start();return null;}
    texture.update(info);
    registration=registered(mission);
    return texture.current;
  }
  function fullModel() {
    if (!renderer || !info) return;
    const [west,south,east,north]=info.bounds;
    const target=[(west+east)/2,(south+north)/2,0];
    const landscape=document.getElementById('campus-model').clientWidth>document.getElementById('campus-model').clientHeight;
    const eye=landscape && north-south>east-west ? [target[0]+1,target[1],1.4] : [target[0],target[1]-1,1.4];
    renderer.focusBounds(info.bounds,{target,eye,frameGround:true});
  }
  function model(canvas, mission, pack) {
    if (!scripts) {
      scripts=Promise.all(['grid','mesh','spatial'].map(script)).then(()=>{
        renderer=window.createSpatialRenderer(canvas,{invalidate,onPick:t=>{
          if(t)document.getElementById('local-hint').textContent=t;
        }});fullModel();invalidate();
      }).catch(()=>{scripts=null;});
    }
    if(!renderer)return;
    const pose=registration && mission.pose?.frame===info.frame ? mission.pose : null;
    const layers=registration && campusRouteLayers(mission);
    const reference=registration && referencePath(mission);
    const paths=registration ? layers.current || reference.points : [];
    const taskRoutes=paths?.length>1 ? [{kind:'global',points:paths}] : [];
    el('route-legend').hidden=!taskRoutes.length;
    if(taskRoutes.length)show('route-legend',layers.current ? layers.title : reference.archived ? '上次派发路线 · 历史参考' : '规划参考线');
    el('leg-legend').hidden=!(layers?.current && layers.leg);
    if(layers?.current && layers.leg)taskRoutes.push({kind:'local',points:layers.leg});
    if(registration && mission.trajectory?.length>1)taskRoutes.push({kind:'trail',points:mission.trajectory});
    const result=renderer.render({frame:info.frame,pose:pose?{status:'online',xyz:[pose.x,pose.y,.25]}:null},
      {connected:true,scene:true,texture:pack,appearancePack:pack,pose:true,grid:false,taskRoutes});
    canvas.dataset.triangles=String(result.triangles || 0);
  }
  return {prepare, model, get info(){return info;}, get registered(){return registration;},
    resetModel:fullModel, focusModel:b=>renderer?.focusBounds(b)};
}

function drawCampusTexture(ctx, pack, project) {
  const [west,south,east,north]=pack.bounds;
  const topLeft=project([west,north]), topRight=project([east,north]), bottomLeft=project([west,south]);
  ctx.save();
  ctx.transform((topRight[0]-topLeft[0])/pack.width,(topRight[1]-topLeft[1])/pack.width,
    (bottomLeft[0]-topLeft[0])/pack.height,(bottomLeft[1]-topLeft[1])/pack.height,...topLeft);
  ctx.drawImage(pack.image,0,0);
  ctx.restore();
}

function missionFocusBounds(mission) {
  if (mission?.kind!=='navigate' || !mission.grid || !point(mission.goal)) return mission?.bounds;
  const frame=mission.grid.frame;
  const layers=campusRouteLayers(mission);
  const points=[mission.goal,...(layers.first || []),...(layers.current || []),...referencePath(mission).points,...(mission.trajectory || [])];
  for(const pose of [mission.initial_pose,mission.pose]) if(pose?.frame===frame) points.push(xy(pose));
  let [west,south,east,north]=[Infinity,Infinity,-Infinity,-Infinity];
  for(const p of points) if(point(p)){
    west=Math.min(west,p[0]); east=Math.max(east,p[0]);
    south=Math.min(south,p[1]); north=Math.max(north,p[1]);
  }
  const pad=Math.max(.5,.10*Math.max(east-west,north-south));
  return [west-pad,south-pad,east+pad,north+pad];
}

function drawRouteInset(pack,mission,transform,bounds,scale,mainWidth) {
  const panel=el('route-inset'), canvas=el('route-inset-map');
  if(mission?.kind!=='navigate' || !point(mission.goal) || !validBounds(bounds))return;
  const layers=campusRouteLayers(mission), reference=referencePath(mission), trail=mission.trajectory || [];
  const points=layers.first || layers.current || reference.points;
  if(points.length<2 && trail.length<2)return;
  const [w,s,e,n]=bounds, cx=(w+e)/2, cy=(s+n)/2;
  if(Math.max(e-w,n-s)*scale>mainWidth*.28)return;
  panel.hidden=false;
  let distance=0;
  for(let i=1;i<points.length;i++)distance+=Math.hypot(points[i][0]-points[i-1][0],points[i][1]-points[i-1][1]);
  const label=layers.first?'出发规划 ':reference.archived && !layers.current?'已派发 ':'规划 ';
  show('inset-distance',distance>0?label+number(distance,1)+' m':'里程计轨迹');
  const width=canvas.clientWidth, height=canvas.clientHeight, dpr=window.devicePixelRatio || 1;
  if(!width || !height)return;
  if(canvas.width!==Math.round(width*dpr) || canvas.height!==Math.round(height*dpr)){
    canvas.width=Math.round(width*dpr);canvas.height=Math.round(height*dpr);
  }
  const ctx=canvas.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  ctx.fillStyle='#10221a';ctx.fillRect(0,0,width,height);
  // North-up inset, independent of the main camera. Both use the same registered map.
  const m=Math.min((width-22)/(e-w),(height-18)/(n-s));
  const zoom=([x,y])=>[width/2+(x-cx)*m,height/2-(y-cy)*m];
  ctx.save();ctx.globalAlpha=.62;drawCampusTexture(ctx,pack,zoom);ctx.restore();
  drawTaskGrid(ctx,mission.grid,transform,zoom,.65);
  const frame=mission.grid.frame;
  paintReferenceLine(ctx,mission,zoom,true);
  pathLine(ctx,trail,zoom,'#69d4e3',2,[4,3]);
  if(mission.initial_pose?.frame===frame)marker(ctx,xy(mission.initial_pose),zoom,{color:'#f1dfba',radius:3.5,label:'起'});
  marker(ctx,mission.goal,zoom,{color:'#c7edac',radius:6,dashed:true,label:'终'});
  if(mission.pose?.frame===frame)marker(ctx,xy(mission.pose),zoom,{color:'#85d9ee',radius:3.5});
}

function drawCampusAtlas(ctx, width, height, pack, mission, transform) {
  const bounds=pack.bounds, [west,south,east,north]=bounds;
  const horizontal=width>height && (north-south)>(east-west);
  const extentX=east-west, extentY=north-south;
  const baseScale=Math.min((width-48)/(horizontal?extentY:extentX),(height-48)/(horizontal?extentX:extentY));
  cameras.global.yaw=horizontal ? Math.PI/2 : 0;
  const {project,scale}=setCameraView(ctx.canvas,'global',[(west+east)/2,(south+north)/2],baseScale,
    ([x,y])=>[clampCamera(x,west,east),clampCamera(y,south,north)]);
  cameraView.maxZoom=256;
  Object.assign(ctx.canvas.dataset,{atlasKey:pack.key,atlasBounds:JSON.stringify(bounds),
    overlayRegistered:String(Boolean(mission)),siteOrientation:horizontal?'north-right':'north-up'});
  drawCampusTexture(ctx,pack,project);
  const occupied=[];
  function label(text,x,y) {
    ctx.font='600 12px "Microsoft YaHei",sans-serif';
    const w=ctx.measureText(text).width+14,h=22;
    if(x<-2 || y<0 || x+w>=width || y+h>=height-23 ||
       occupied.some(r=>x<r[0]+r[2]+4 && x+w>r[0]-4 && y<r[1]+h+4 && y+h>r[1]-4))return;
    occupied.push([x,y,w]);ctx.fillStyle='#11251de8';ctx.fillRect(x,y,w,h);
    ctx.fillStyle='#ebf3df';ctx.textBaseline='middle';ctx.fillText(text,x+7,y+h/2);
  }
  const taskSize=mission && validBounds(mission.bounds) ? Math.max(mission.bounds[2]-mission.bounds[0],mission.bounds[3]-mission.bounds[1])*scale : 0;
  if(mission && transform){
    drawRouteInset(pack,mission,transform,missionFocusBounds(mission),scale,width);
    if(taskSize>36) paintTaskOverlay(ctx,mission,transform,project,scale,width,.68);
    else{
      paintReferenceLine(ctx,mission,project,true);
      pathLine(ctx,mission.trajectory,project,'#69d4e3',2);
      const pose=mission.pose?.frame===pack.frame ? xy(mission.pose) : null;
      if(point(pose)){
        marker(ctx,pose,project,{color:'#85d9ee',radius:6,yaw:mission.pose.yaw-cameras.global.yaw});
        const [x,y]=project(pose), zone=poseZone(mission);
        label(zone?'机器人 · '+zone:'机器人',x+10,y-11);
      }
    }
  }
  if(taskSize<90)for(const area of pack.areas){
    const [x0,y0,x1,y1]=area.bounds;
    if((x1-x0)*(y1-y0)>extentX*extentY*.10)continue;
    const [x,y]=project([(x0+x1)/2,(y0+y1)/2]);
    label(area.label,x+4,y-11);
  }
  const rough=72/scale, power=10**Math.floor(Math.log10(rough));
  const meters=(rough/power>=5?5:rough/power>=2?2:1)*power;
  ctx.fillStyle='#11251de8';ctx.fillRect(10,height-28,Math.ceil(scale*meters)+70,22);
  pathLine(ctx,[[17,height-16],[17+scale*meters,height-16]],p=>p,'#d7e6da',2);
  ctx.fillStyle='#dfebdf';ctx.font='12px system-ui';ctx.textBaseline='middle';
  ctx.fillText(meters+' m',22+scale*meters,height-16);
  ctx.fillStyle='#11251de8';ctx.fillRect(10,9,54,23);
  ctx.fillStyle='#e1eddf';ctx.fillText(horizontal?'北 →':'北 ↑',20,21);
}

function validBounds(b) {
  return Array.isArray(b) && b.length===4 && b.every(Number.isFinite) && b[0]<b[2] && b[1]<b[3];
}
