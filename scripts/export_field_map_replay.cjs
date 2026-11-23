/* Export a verified field scene and its task as a self-contained, read-only HTML replay.
   node scripts/export_field_map_replay.cjs SENSOR_EVIDENCE TASK_JSON OUTPUT_HTML */
'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {createHash} = require('node:crypto');
const assert = require('node:assert/strict');

const [evidence, taskFile, output, initialView='field'] = process.argv.slice(2);
if (!output) throw Error('Expected sensor-evidence directory, task JSON and output HTML');
assert(['field','example','rgbd'].includes(initialView),'unknown initial map view');
const web=path.resolve(__dirname,'../console/web');
function text(file){return fs.readFileSync(file,'utf8');}
function digest(bytes){return 'sha256:'+createHash('sha256').update(bytes).digest('hex');}
const manifest=JSON.parse(text(path.join(evidence,'manifest.json')));
function verified(name){
  const bytes=fs.readFileSync(path.join(evidence,name));
  assert.equal(digest(bytes),manifest.files[name],name+' digest mismatch');
  return bytes;
}
const scene=JSON.parse(verified('occupancy-scene.json'));
const rgbd=JSON.parse(verified('rgbd-scene.json'));
const task=JSON.parse(text(taskFile));
const plan=task.steps?.find(s=>s.evidence?.source_evidence?.snapshot_ref===manifest.lidar_snapshot_ref)?.evidence;
assert(plan && plan.motion_authorized===false,'a matching read-only planner receipt is required');
assert.equal(scene.source_evidence.snapshot_ref,plan.source_evidence.snapshot_ref);
assert.equal(scene.stamp_ns,plan.scan_stamp_ns);
assert.equal(scene.frame_id,plan.base_frame);
assert.equal(manifest.rgbd_scene_ref,manifest.files['rgbd-scene.json']);
// Bind each visible image to its own sensor capture, including the one used as UV texture.
const images={};
function bindPNG(name,ref,url){
  const bytes=verified(name);
  assert.equal(digest(bytes),ref);assert.equal(bytes.subarray(0,8).toString('hex'),'89504e470d0a1a0a');
  assert.equal(url,'/camera/'+ref.slice(7)+'.png');
  images[url]='data:image/png;base64,'+bytes.toString('base64');
}
bindPNG('lidar-task-color.png',scene.texture.ref,scene.texture.url);
bindPNG('rgbd-color.png',rgbd.source_evidence.color.artifact_ref,rgbd.texture_url);
const example=JSON.parse(text(path.join(web,'plan-example.json')));
const data={scene,rgbd,plan,example,images,request:task.text,taskId:task.id,initialView};
const safeJSON=JSON.stringify(data).replace(/</g,'\\u003c').replace(/\u2028/g,'\\u2028').replace(/\u2029/g,'\\u2029');

const hosted=text(path.join(web,'plan-observe.html'));
function fragment(start,end){
  const a=hosted.indexOf(start),b=hosted.indexOf(end,a);
  if(a<0||b<=a)throw Error('Expected the current product map panel markup');
  return hosted.slice(a,b);
}
const panel=fragment('<div class="layout">','<section id="task-panel"');
const css=text(path.join(web,'plan-observe.css'));
const original=text(path.join(web,'plan-scene.js'));
const textureAssignment='img.src=next;';
assert.equal(original.split(textureAssignment).length,2,'texture source adapter changed');
const renderer=original.replace(textureAssignment,'img.src=REPLAY.images[next];');

function replay(){
  const id=name=>document.getElementById(name);
  const set=(name,value)=>{id(name).textContent=value;};
  const renderer=window.FieldScene.create(id('map'),{onChange(s){
    id('view-top').setAttribute('aria-pressed',s.view==='top');
    id('view-orbit').setAttribute('aria-pressed',s.view==='orbit');
  }});
  const tabs={field:'scene-field',rgbd:'view-rgbd',example:'scene-example'};
  const time=ns=>new Date(Number(BigInt(ns)/1000000n)).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai',hour12:false});
  const meters=v=>Number.isFinite(v)?v.toFixed(3)+' m':'—';
  function metric(label,title,value){set(label+'-label',title);set(label,value);}
  function picture(url,title,source){
    const frame=id('rgb');
    document.querySelector('.camera').hidden=!url;
    if(!url){frame.removeAttribute('src');return;}
    frame.src=REPLAY.images[url];frame.hidden=false;id('rgb-empty').hidden=true;
    set('rgb-title',title);set('rgb-source',source);
  }
  function choose(mode){
    for(const [name,button] of Object.entries(tabs))id(button).setAttribute('aria-pressed',name===mode);
    document.body.dataset.scene=mode;
    document.querySelector('.layers').hidden=mode==='rgbd';
    id('rgb-grid').hidden=mode!=='rgbd';
    const {scene,rgbd,example,plan}=REPLAY;
    if(mode==='rgbd'){
      renderer.setData(rgbd,null);
      set('scene-title','相机彩色三维表面');set('scene-meta',time(rgbd.stamp_ns)+' · D435');
      set('decision','彩色纹理与 10 cm 占用网格');
      set('detail','细线显示已观测表面与相机坐标系 10 厘米栅格的交界。可关闭网格对照原始纹理。深度缺测处保留空隙，此次采样与车体障碍图各自使用独立坐标和时间。');
      set('source',rgbd.frame_id+' · '+rgbd.stamp_ns+' ns');
      picture(rgbd.texture_url,'同帧 RGB 纹理',time(rgbd.stamp_ns));
      metric('range','有效顶点',rgbd.vertices.filter(Boolean).length);
      metric('clearance','纹理三角面',rgbd.triangles.length);metric('coverage','深度命中单元',id('map').dataset.colorCells);
    }else{
      const isField=mode==='field',p=isField?plan:example.plan,s=isField?scene:example.scene;
      renderer.setData(s,p);
      const held=p.status==='blocked',partial=p.status==='partial';
      set('scene-title',isField?'车身周围的占用网格':'中央箱体绕行');
      set('scene-meta',isField?time(s.stamp_ns)+' · 双 MID-360':'Gazebo 仿真 · 中央箱体');
      set('route-label',held?'停驻标记':partial?'候选轨迹':'规划轨迹');
      set('decision',held?'建议保持原位':partial?'候选绕行轨迹':'规划轨迹');
      const shape=p.trajectory?.continuity==='C2'?'绿色箭头沿规划器验证过的平滑曲线标出行进方向。':'绿色箭头标出航点之间的行进方向。';
      set('detail',isField?REPLAY.request:shape+(partial?'虚线表示部分通道仍待观测。':''));
      set('source',isField?REPLAY.taskId:example.source);
      picture(isField?s.texture.url:null,'任务现场彩色帧',isField?time(s.texture.stamp_ns):'');
      metric('range','车头到前方表面',meters(p.front_clearance_to_surface_m));
      metric('clearance',partial?'候选路径观测覆盖':'车身到可见障碍',partial?(p.coverage_fraction_on_path*100).toFixed(1)+'%':meters(p.present_min_clearance_m));
      metric('coverage','要求的车身净距',meters(p.margin_m));
    }
    renderer.reset();
  }
  Object.entries(tabs).forEach(([mode,button])=>{id(button).onclick=()=>choose(mode);});
  for(const button of document.querySelectorAll('[data-layer]'))button.onclick=()=>{
    const enabled=button.getAttribute('aria-pressed')!=='true';
    button.setAttribute('aria-pressed',enabled);renderer.setLayer(button.dataset.layer,enabled);
  };
  id('view-top').onclick=()=>renderer.setView('top');id('view-orbit').onclick=()=>renderer.setView('orbit');
  id('zoom-in').onclick=()=>renderer.zoomBy(1.25);id('zoom-out').onclick=()=>renderer.zoomBy(.8);
  id('reset-camera').onclick=()=>renderer.reset();id('map-empty').hidden=true;
  choose(REPLAY.initialView);
}

const html='<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'+
  '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; connect-src \'none\'; form-action \'none\'; base-uri \'none\'">'+
  '<meta name="viewport" content="width=device-width,initial-scale=1"><title>实车障碍地图 · 离线回放</title><style>'+css+
  '\n.canvas-wrap{height:clamp(395px,62vh,820px)} .scene-heading{max-width:64%} .mission p+p{margin-top:10px}.sidebar .context{flex:1}'+
  '</style></head><body><header><div class="brand"><i>◇</i> Edge Agent <b>ROS</b></div><span class="divider"></span>机器人观测台</header>'+
  '<main><div class="heading"><h1>三维感知与路径规划</h1><span class="mode">离线回放</span></div>'+panel+
  '<section class="context panel"><div class="bar"><h2>观测结论</h2></div><div class="mission"><p id="decision"></p><p id="detail"></p></div><div class="provenance" id="source"></div></section></aside></div></main>'+
  '<script>const REPLAY='+safeJSON+';\n'+renderer+'\n('+replay.toString()+')();</script></body></html>';
fs.writeFileSync(output,html);
console.log(digest(Buffer.from(html))+' '+output);
