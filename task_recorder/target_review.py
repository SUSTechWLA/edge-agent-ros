"""Replay a grounded RGB-D target, including its blocked-to-resumed tool lifecycle."""
import argparse
from datetime import datetime
import html
import json
import math
from pathlib import Path
import re
import shutil

from PIL import Image, ImageDraw, ImageOps

from .map_view import map_output
from .render import Recording
from .review_media import write_video
from .sensor_review import (FPS, GOLD, GREEN, GRID, INK, MID, TaskReceipt, base, clock,
                            depth_colors, intro, ns, panel, pixels, read, require, sha, text)


def artifact(root, reference, suffix):
    digest = re.fullmatch(r"sha256:([a-f0-9]{64})", reference)
    require(digest is not None, "invalid archived artifact reference")
    path = root / (digest[1] + suffix)
    require(path.is_file() and sha(path) == digest[1], "missing or altered archived artifact")
    return path


def mask_rows(mask, size, point, bbox):
    w, h = size
    stride = mask["stride"]
    require(type(stride) is int and 1 <= stride <= max(size), "invalid sampling stride")
    gw, gh = ((n + stride - 1) // stride for n in size)
    require((gw, gh) == (mask["grid_width"], mask["grid_height"]), "mask raster does not match image")
    x0, y0, x1, y1 = bbox
    require(all(type(v) is int for v in (*point,*bbox)) and
            0 <= x0 <= point[0] < x1 <= w and 0 <= y0 <= point[1] < y1 <= h,
            "target pixel outside boxed image")
    row_ranges = {}
    for y, left, right in mask["runs"]:
        require(all(type(v) is int for v in (y,left,right)) and 0 <= y < gh and 0 <= left < right <= gw,
                "invalid target mask run")
        row_ranges.setdefault(y, []).append((left, right))
    require(any(left <= point[0] // stride < right for left, right in row_ranges.get(point[1] // stride, [])),
            "target pixel outside source mask")
    return row_ranges


def unproject(u, v, z, k):
    return ((u - k[2]) * z / k[0], (v - k[5]) * z / k[4], z)


class Target(TaskReceipt):
    def __init__(self, task, recording, artifacts):
        super().__init__(task, recording)
        self.g = self.batch["grounding"]
        self.obs = self.batch["observations"]
        self.actor = self.obs["actor_ref"]
        self.raw = {}
        require(self.obs["basis"] == "published" and ns(self.events[1]["stamp_ns"]) <= ns(self.obs["as_of_ns"])
                <= ns(self.events[2]["stamp_ns"]), "target not bound to its original observation call")
        refs = {key: self.g[key] for key in ("rgb_ref", "depth_ref", "calibration_ref")}
        for key, ref in refs.items():
            self.raw[key] = artifact(artifacts, ref, ".json" if key == "calibration_ref" else ".png")
        self.cal = read(self.raw["calibration_ref"])
        self.k = self.cal["k"]
        self.scale = self.g["depth_scale_m"]
        require(0 < self.scale <= .1 and self.g.get("depth_scale_ref") and len(self.k) == 9
                and self.k[0] > 0 and self.k[4] > 0, "depth scale/intrinsics evidence missing")
        require(self.g["actor_ref"] == self.actor and self.g["clock_id"] == self.obs["clock_id"] == "unix"
                and self.g["frame_id"] == self.cal["frame_id"], "incorrect result actor, frame or clock")
        self.selected = {}
        for key in refs:
            name = key.removesuffix("_ref")
            matches = [s for s in self.obs["samples"] if s.get("artifact_ref") == refs[key]
                       and s.get("kind") == "artifact" and s.get("calibration_ref") == refs["calibration_ref"]
                       and s["frame_id"] == self.cal["frame_id"] and s["actor_ref"] == self.actor
                       and s["clock_id"] == "unix" and s["source_id"] == self.g[name+"_source_id"]
                       and ns(s["stamp_ns"]) == ns(self.g[name+"_stamp_ns"])]
            require(len(matches) == 1, "grounding input missing from original observation batch")
            self.selected[key] = matches[0]
        with Image.open(self.raw["rgb_ref"]) as src:
            require(src.format == "PNG" and src.mode in ("RGB","RGBA"), "invalid color capture")
            self.rgb = src.convert("RGB")
        with Image.open(self.raw["depth_ref"]) as src:
            require(src.format == "PNG" and src.mode in ("I;16","I;16B","I;16L","I"), "invalid aligned depth")
            self.depth = src.copy()
        require(self.depth.size == self.rgb.size == (self.cal["width"], self.cal["height"])
                == (self.g["width"], self.g["height"]), "unaligned raster sizes")
        self.pt = self.g["point_pixel"]
        self.bbox = self.g["bbox_xyxy"]
        self.mask = self.g["mask"]
        self.rows = mask_rows(self.mask, self.rgb.size, self.pt, self.bbox)
        self.xyz = self.g["point_camera_optical_m"]
        raw = self.depth.getpixel(tuple(self.pt))
        require(0 < raw < 65535 and raw == self.g["point_depth_raw"] and len(self.xyz) == 3
                and math.dist(self.xyz, unproject(*self.pt, raw*self.scale, self.k)) <= .001,
                "archived depth and intrinsics disagree with 3D result")


def annotate(view, raster, *, use_mask=True):
    """Display original integer pixel coordinates and half-open source bounding box."""
    im = raster.convert("RGBA")
    marks = Image.new("RGBA", im.size)
    d = ImageDraw.Draw(marks)
    if use_mask:
        stride = view.mask["stride"]
        for y, runs in view.rows.items():
            for left, right in runs:
                d.rectangle((left*stride, y*stride, right*stride-1, (y+1)*stride-1),
                            fill=(50,210,164,85))
    x0,y0,x1,y1 = view.bbox
    d.rectangle((x0,y0,x1-1,y1-1), outline=GREEN, width=3)
    u,v = view.pt
    d.ellipse((u-7,v-7,u+7,v+7),outline=(10,20,25,255),width=4)
    d.ellipse((u-5,v-5,u+5,v+5),outline=GOLD,width=3)
    d.line((u-12,v,u-6,v),fill=GOLD,width=2)
    d.line((u+6,v,u+12,v),fill=GOLD,width=2)
    return Image.alpha_composite(im, marks).convert("RGB")


def paste(im, frame, box):
    x,y,r,b = box
    photo = ImageOps.contain(frame,(r-x,b-y),Image.Resampling.LANCZOS)
    im.paste(photo, (x+(r-x-photo.width)//2,y+(b-y-photo.height)//2))


def image_scene(view, face, *, depth=False):
    if depth:
        vals = list(pixels(view.depth))
        positive = sorted(v for v in vals if v > 0)
        require(positive, "depth image contains no returns")
        lo, hi = (positive[round((len(positive)-1)*p)] for p in (.02,.98))
        lut, colors = depth_colors(lo,hi)
        field = Image.frombytes("P",view.depth.size,bytes(lut[v] for v in vals))
        field.putpalette(colors)
        frame = annotate(view,field.convert("RGB"),use_mask=False)
        title = "从对齐深度读取目标像素"
        detail = "彩色框和十字来自同一次操作。深度颜色以整帧有效值的 2% 和 98% 分位缩放。"
    else:
        frame = annotate(view,view.rgb)
        title = "在 D435 实拍图像中标注目标"
        detail = "绿框是目标外接框，半透明区域是算法返回的掩膜。金色十字标出实测三维点所在的像素。"
    im = base(title,detail,"对齐深度" if depth else "二维标注",view.task["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,192,888,715))
    panel(d,(910,192,1392,715))
    paste(im,frame,(64,204,872,699))
    u,v = view.pt
    text(d,937,221,"目标像素",24,MID,face)
    text(d,937,268,f"u = {u}     v = {v}",33,INK,face,422)
    if depth:
        raw = view.depth.getpixel((u,v))
        text(d,937,355,f"原始深度数值   {raw}",23,INK,face,427)
        text(d,937,402,f"深度单位   {view.scale:g} m / 单位",22,INK,face,427)
        text(d,937,479,f"光轴深度 Z    {view.xyz[2]:.3f} m",27,GOLD,face,427)
        text(d,937,579,f"配色范围   {lo*view.scale:.2f} 至 {hi*view.scale:.2f} m",20,MID,face,427)
        text(d,937,621,"深色零值表示无有效深度。",18,MID,face)
    else:
        text(d,937,352,"原图坐标中的外接框",22,MID,face)
        x0,y0,x1,y1 = view.bbox
        text(d,937,395,f"({x0}, {y0})   至   ({x1}, {y1})",25,INK,face,427)
        text(d,937,440,"右、下边界为开区间。",18,MID,face)
        text(d,937,520,f"图像尺寸    {view.rgb.width} × {view.rgb.height}",22,INK,face)
        text(d,937,579,clock(ns(view.selected["rgb_ref"]["stamp_ns"]))+"   北京时间",22,GREEN,face,420)
        text(d,937,623,view.cal["frame_id"],18,MID,face,422)
    return im


def measured_cloud(view):
    w,h = view.depth.size
    step = max(1,math.ceil(math.sqrt(w*h/5000)))
    stride = view.mask["stride"]
    points = []
    depth = view.depth.load()
    for v in range(step//2,h,step):
        runs = view.rows.get(v//stride,())
        for u in range(step//2,w,step):
            if 0 < depth[u,v] < 65535:
                target = any(a <= u//stride < b for a,b in runs)
                points.append((unproject(u,v,depth[u,v]*view.scale,view.k),target))
    require(points, "no depth points to plot")
    return step, points


def scene_3d(view, face):
    im = base("用原始深度在三维中复核标注", "图中每个点来自一处有效深度像素，坐标原点在 D435 彩色光学中心。",
              "三维标注",view.task["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,193,870,715))
    panel(d,(889,193,1392,715))
    step, points = measured_cloud(view)
    distances = sorted(math.dist(p,view.xyz) for p,target in points if target)
    require(distances, "target mask contains no sampled depth points")
    radius = max(.05,1.25*distances[round((len(distances)-1)*.95)])
    kept = [(p,target) for p,target in points if math.dist(p,view.xyz) <= radius]
    # Oblique parallel projection. X is right, Y down and Z recedes upward/right.
    def project(point):
        x,y,z = point
        return x + .45*z, y - .43*z
    axis = min(.1,max(.01,radius/5))
    xy = [project(p) for p,_ in kept] + [project(view.xyz)]
    xs,ys = zip(*xy)
    l,r,t,b = min(xs),max(xs),min(ys),max(ys)
    scale = min(710/max(.01,r-l),373/max(.01,b-t))
    def screen(p):
        x,y = project(p)
        return round(456+(x-(l+r)/2)*scale),round(421+(y-(t+b)/2)*scale)
    for p,target in sorted(kept,key=lambda item:-item[0][2]):
        x,y = screen(p)
        color = "#70dfb4" if target else "#607785"
        dot = 2 if target else 1
        d.ellipse((x-dot,y-dot,x+dot,y+dot), fill=color)
    origin = (116,517)
    for label,p,col in zip(("X","Y","Z"),((axis,0,0),(0,axis,0),(0,0,axis)),("#e99d9b","#76bbc9","#bda2f2")):
        dx,dy = project(p)
        end = (round(origin[0]+dx*scale),round(origin[1]+dy*scale))
        d.line([origin,end],fill=col,width=2)
        text(d,end[0]+3,end[1],label,16,col,face)
    x,y = screen(view.xyz)
    d.ellipse((x-9,y-9,x+9,y+9),outline="#162027",width=5)
    d.ellipse((x-8,y-8,x+8,y+8),outline=GOLD,width=3)
    d.line((x-14,y,x+14,y),fill=GOLD,width=1)
    d.line((x,y-14,x,y+14),fill=GOLD,width=1)
    text(d,72,630,f"绿色为掩膜内的有效表面点    ·    灰色为周边场景    ·    方向标每轴 {axis:.2f} m",19,MID,face,774)
    text(d,72,674,f"原图每隔 {step} 像素取样，显示定位点周围 {radius:.2f} m 的 {len(kept):,} 个点。",18,MID,face,774)
    text(d,915,224,"相机光学坐标",26,GREEN,face)
    for i,(label,value) in enumerate(zip(("X 向右","Y 向下","Z 向前"),view.xyz)):
        y=300+i*71
        text(d,916,y,label,23,MID,face)
        text(d,1118,y,f"{value:+.3f} m",28,INK,face,245)
    q = view.g.get("quality",{})
    if "p95_residual_m" in q:
        text(d,915,548,f"平面拟合残差 P95    {q['p95_residual_m']*1000:.1f} mm",21,GOLD,face,443)
        text(d,915,586,"该数值描述平面拟合误差。",18,MID,face)
    text(d,915,651,view.cal["frame_id"],19,MID,face,443)
    return im


def cloud_viewer(view, path):
    step,points = measured_cloud(view)
    data = {"points":[[*(round(value,5) for value in p),int(target)] for p,target in points],
            "target":view.xyz,"pixel":view.pt}
    content = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>D435 实测三维目标</title><style>*{box-sizing:border-box}body{margin:0;background:#101a21;color:#f4f2e8;font:17px/1.5 "Microsoft YaHei",sans-serif}
main{max-width:1280px;margin:auto;padding:20px}h1{margin:0;font-weight:600}p{margin:9px 0;color:#b6c8cf}canvas{display:block;width:100%;height:65vh;min-height:340px;background:#192833;border-radius:12px;touch-action:none;cursor:grab}
button{background:#263b48;padding:7px 14px;border:1px solid #47616d;border-radius:6px;color:#f4f2e8;font:inherit;cursor:pointer}a{color:#84e2ba}</style>
<main><h1>目标周围的实测三维表面</h1><p>拖动旋转，滚轮缩放。绿色为二维掩膜内的实测点，金色十字为定位点，灰色为周边场景。</p>
<canvas aria-label="可旋转的 D435 光学坐标点云"></canvas><p id="values"></p><p><button id="reset">恢复视角</button> <button id="toggle">查看全部场景</button>　<a href="index.html">返回任务录像</a></p></main>
<script>const capture=DATA;const c=document.querySelector('canvas'),ctx=c.getContext('2d'),t=capture.target;
let yaw,pitch,zoom,only=true,drag;
function reset(){yaw=-.42;pitch=-.25;zoom=1;paint()}
function paint(){const w=c.clientWidth,h=c.clientHeight,ratio=Math.min(window.devicePixelRatio||1,2);
c.width=w*ratio;c.height=h*ratio;ctx.scale(ratio,ratio);ctx.clearRect(0,0,w,h);
const sy=Math.sin(yaw),cy=Math.cos(yaw),sp=Math.sin(pitch),cp=Math.cos(pitch);
function rotate(p){const x=p[0]-t[0],y=p[1]-t[1],z=p[2]-t[2],a=cy*x-sy*z,b=sy*x+cy*z;return[a,cp*y-sp*b,sp*y+cp*b,p[3]]}
const pts=capture.points.filter(p=>!only||p[3]).map(rotate);if(!pts.length)return;
const radii=pts.map(p=>Math.hypot(p[0],p[1],p[2])).sort((a,b)=>a-b),extent=Math.max(.04,radii[Math.round((radii.length-1)*.94)]);
const scale=.44*Math.min(w,h)/extent*zoom;function xy(p){return[w/2+p[0]*scale,h/2+p[1]*scale]}
for(const p of pts.sort((a,b)=>b[2]-a[2])){const [x,y]=xy(p);ctx.fillStyle=p[3]?'#69d6ad':'#647d89';ctx.fillRect(x,y,p[3]?3:2,p[3]?3:2)}
const [ox,oy]=xy(rotate([0,0,0]));const axis=Math.min(.1,extent/3);
for(const [label,p,color] of [['X',[axis,0,0],'#ee9c8d'],['Y',[0,axis,0],'#78c7d1'],['Z',[0,0,axis],'#c0a7ed']]){
const [x,y]=xy(rotate(p));ctx.strokeStyle=ctx.fillStyle=color;ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(ox,oy);ctx.lineTo(x,y);ctx.stroke();ctx.font='15px sans-serif';ctx.fillText(label,x+3,y)}
const [x,y]=xy(rotate(t));ctx.strokeStyle='#ffc467';ctx.lineWidth=3;ctx.beginPath();ctx.arc(x,y,8,0,2*Math.PI);ctx.stroke();ctx.beginPath();ctx.moveTo(x-14,y);ctx.lineTo(x+14,y);ctx.moveTo(x,y-14);ctx.lineTo(x,y+14);ctx.stroke()}
c.onpointerdown=e=>{drag=[e.clientX,e.clientY];c.setPointerCapture(e.pointerId)};
c.onpointermove=e=>{if(!drag)return;yaw+=(e.clientX-drag[0])*.009;pitch=Math.max(-1.5,Math.min(1.5,pitch+(e.clientY-drag[1])*.009));drag=[e.clientX,e.clientY];paint()};
c.onpointerup=c.onpointercancel=()=>{drag=null};c.addEventListener('wheel',e=>{e.preventDefault();zoom=Math.max(.15,Math.min(12,zoom*Math.exp(-e.deltaY*.001)));paint()},{passive:false});
document.getElementById('values').textContent='原始像素 ('+capture.pixel.join(', ')+')　·　光学坐标 ('+t.map(v=>v.toFixed(4)).join(', ')+') m　·　每隔 STEP 个像素取样';
document.querySelector('#reset').onclick=reset;document.querySelector('#toggle').onclick=e=>{only=!only;e.target.textContent=only?'查看全部场景':'仅看目标区域';paint()};window.addEventListener('resize',paint);reset();</script></html>'''
    path.write_text(content.replace("DATA",json.dumps(data,separators=(",",":"))).replace("STEP",str(step)),encoding="utf8")


def includes(value, word):
    """Inspect structured catalog entries without substring-matching unrelated names."""
    if isinstance(value,dict):
        return any(key == word or includes(v,word) for key,v in value.items())
    if isinstance(value,list):
        return any(includes(v,word) for v in value)
    return value == word


class Evolution:
    def __init__(self, root, view):
        self.inputs = {p: root/p for p in ("gap-task.json","catalog-before.json","catalog-after.json","events.jsonl",
                                         "gap-recording/recording.json","gap-recording/media/mission/timeline.jsonl")}
        self.gap = read(self.inputs["gap-task.json"])
        self.record = Recording(root/"gap-recording","received_ns")
        original, = self.record.by["agent_request"]
        require(self.gap["state"] == "BLOCKED" and not self.gap["steps"] and self.gap["text"] == original["text"]
                and self.gap["id"] == self.record.header["task_id"] and self.record.header["task_status"] == "BLOCKED"
                and self.gap["text"] == view.task["text"], "gap recording and resumed task disagree")
        self.source_request = original
        trigger = view.task["trigger"]
        require(trigger["parent_task_id"] == self.gap["id"] and trigger["trace_id"] == self.gap["trace_id"]
                and self.gap["trace_id"] == view.task["trace_id"], "follow-up task lost native trigger or trace")
        self.before = read(self.inputs["catalog-before.json"])
        self.after = read(self.inputs["catalog-after.json"])
        require(not includes(self.before,view.tool) and includes(self.after,view.tool),
                "tool discovery snapshots do not confirm the candidate transition")
        self.events = [json.loads(line) for line in self.inputs["events.jsonl"].read_text(encoding="utf8").splitlines() if line]
        require(self.events and all(e.get("phase") and e.get("utc") for e in self.events), "incomplete evolution event export")
        phases = {e["phase"]: e for e in self.events}
        require(all(e["task_id"] == self.gap["id"] and e["trace_id"] == view.task["trace_id"] for e in self.events)
                and phases["release_applied"]["event_id"] == trigger["parent_event_id"]
                and phases["candidate_generated"]["candidate_digest"] == phases["release_applied"]["candidate_digest"]
                == view.task["steps"][0]["soft_pin"]["content_digest"]
                and phases["continuation_submitted"]["binding"] == trigger["read_only_tool"]["binding"]
                and phases["continuation_terminal"]["followup_task_id"] == view.task["id"]
                and phases["continuation_terminal"]["state"] == view.task["state"], "unrelated evolution receipts")
        times = [datetime.fromisoformat(e["utc"].replace("Z","+00:00")) for e in self.events]
        require(all(a<=b for a,b in zip(times,times[1:])),"evolution clock regressed")
        qualification = root/"qualification.json"
        self.qualification = None
        if qualification.is_file():
            self.inputs["qualification.json"] = qualification
            q = read(qualification)
            require(q["passed"] is True and q["actor_ref"] == view.actor and q["tool_id"] == view.tool
                    and q["backend_digest"] == view.batch["backend_digest"] and
                    q["profile_digest"] == view.batch["profile_digest"] and q["checks"],
                    "candidate qualification does not match deployed backend or profile")
            self.qualification = q


def gap_scene(view, evo, face):
    gap = evo.gap
    im = base("首次请求遇到能力缺口","录制器保留了首次请求和阻塞终态。之后的工具上架与续办沿用本次任务来源。",
              "首次请求",gap["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,192,1392,464))
    text(d,77,228,"用户原文",24,GREEN,face)
    text(d,77,299,gap["text"],43,INK,face,1280)
    text(d,77,390,clock(ns(evo.source_request["stamp_ns"]))+"   北京时间",22,MID,face)
    text(d,59,510,"BLOCKED",39,GOLD,face)
    text(d,330,516,"执行步骤    0",26,INK,face)
    text(d,56,606,"首次目录中未发现该工具。",25,MID,face)
    text(d,56,663,"请求链    "+gap["trace_id"],20,MID,face,1315)
    return im


def event_scene(view, events, face):
    im = base("从能力缺口到现场标注","开发者提供感知算法和只读工具模板。边缘监督器依据原任务选择候选，校验并注册工具。",
              "沙盒演化记录",view.task["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,192,1392,715))
    for i,event in enumerate(events):
        y = 228+i*85
        time = datetime.fromisoformat(event["utc"].replace("Z","+00:00"))
        text(d,77,y,time.strftime("%H:%M:%S")+" "+time.tzname(),19,MID,face,236)
        text(d,319,y,event.get("label_zh") or event["phase"],24,GREEN,face,623)
        notes = {"gap_observed":"首次请求受阻，未派发任何动作",
                 "candidate_generated":"按原句匹配服务，生成只读工具发布提案",
                 "candidate_verified":"独立验收器从原图复算了三维表面点",
                 "site_policy_matched":"核对部署者预先批准的版本与只读权限",
                 "release_applied":"注册表以单个事务接纳候选工具",
                 "continuation_submitted":"监督器使用原任务文本和批准绑定续办",
                 "continuation_terminal":"Agent 核验工具回执，续办任务成功"}
        info = notes.get(event["phase"]) or event.get("detail_zh") or event.get("code")
        if info:
            text(d,319,y+32,str(info),18,MID,face,1014)
        d.line((77,y+65,1364,y+65),fill=GRID)
    return im


def resume_scene(view, evo, face):
    im = base("工具上架后，原请求触发续办","前后目录快照记录工具是否可见。任务账本将两次请求关联到同一个 trace。",
              "能力发现与续办",view.task["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,192,1392,715))
    for i,(name,value) in enumerate((("首次工具目录","未发现 "+view.tool),("续办前工具目录","已发现 "+view.tool),
            ("触发来源",view.task["trigger"]["source"]),("父任务",evo.gap["id"]),("请求链",view.task["trace_id"]))):
        y = 232 + i*85
        text(d,78,y,name,24,MID,face,260)
        text(d,376,y,value,25,GREEN if i==1 else INK,face,967)
        d.line((78,y+49,1357,y+49),fill=GRID)
    return im


def qualification_scene(view, q, face):
    im = base("候选工具通过独立实测验收","验收器在现场重新读取了彩色帧、深度与内参，并使用独立代码核对算法结果。",
              "候选资格验收",view.task["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,192,830,715))
    panel(d,(850,192,1392,715))
    checks = {"live_observation":"读取现场传感器观测","release_sha256":"核对后端与配置摘要",
              "source_sha256":"核对原始数据摘要","target_and_frame":"核对目标描述与坐标系",
              "mask_and_bbox":"核对掩膜与边界框","measured_depth_pixel":"从原图读取深度像素",
              "independent_backprojection":"独立反投影到三维","unsupported_target_rejected":"拒绝超出能力的目标描述"}
    for i,key in enumerate(q["checks"]):
        y=228+i*min(53,415//len(q["checks"]))
        text(d,75,y,"通过",21,GREEN,face,70)
        text(d,172,y,checks.get(key,key),22,INK,face,622)
    text(d,876,220,"验收帧测量",26,GREEN,face)
    values=(("采样时刻",clock(ns(q["observation_as_of_ns"]))),
            ("像素坐标",str(q["selected_pixel"])),("原始深度",str(q["raw_depth"])),
            ("彩色 / 深度偏差",f"{q['rgb_depth_skew_ns']/1e6:.2f} ms"))
    for i,(name,value) in enumerate(values):
        y=294+i*77
        text(d,876,y,name,20,MID,face,250)
        text(d,1098,y,value,22,INK,face,265)
    text(d,875,623,"后续章节显示续办操作采集的另一批数据。",18,MID,face,485)
    return im


def finish(view, face):
    im = base("Agent 核对操作并结束任务",view.task["text"],"核对任务终态",view.task["id"],face)
    d = ImageDraw.Draw(im)
    panel(d,(48,192,1392,715))
    labels = ("收到自然语言任务","发出工具调用","工具返回定位证据","Agent 核对步骤成功")
    for i,(event,label) in enumerate(zip(view.events,labels)):
        y = 227+i*80
        text(d,77,y,clock(ns(event["stamp_ns"])),23,GREEN,face)
        text(d,379,y,label,25,INK,face)
        d.line((77,y+46,1363,y+46),fill=GRID)
    text(d,77,580,"SUCCEEDED",38,GREEN,face)
    text(d,381,589,f"续办任务用时 {view.elapsed:.1f} ms",25,INK,face)
    text(d,77,661,view.op,20,MID,face,1287)
    return im


def render(view, evolution, destination, face, ffmpeg, ffprobe):
    with map_output(destination) as out:
        scenes = []
        if evolution:
            scenes.append(("首次请求受阻",4,gap_scene(view,evolution,face)))
            for off in range(0,len(evolution.events),5):
                events = evolution.events[off:off+5]
                scenes.append((f"演化事件 {off+1}–{off+len(events)}",max(4,len(events)+1),event_scene(view,events,face)))
            if evolution.qualification:
                scenes.append(("独立验收的原始样本",6,qualification_scene(view,evolution.qualification,face)))
            scenes.append(("目录变更与续办",5,resume_scene(view,evolution,face)))
        scenes.append(("任务与工具",4,intro(view,face)))
        annotate(view,view.rgb).save(out/"annotated-rgb.png")
        cloud_viewer(view,out/"cloud.html")
        samples = out/"samples"
        samples.mkdir()
        for name, path in view.inputs.items():
            shutil.copyfile(path,out/name)
        for path in view.raw.values():
            shutil.copyfile(path,samples/path.name)
        if evolution:
            for name,path in evolution.inputs.items():
                dest = out/"evolution"/name
                dest.parent.mkdir(parents=True,exist_ok=True)
                shutil.copyfile(path,dest)
        for title,filename,scene,seconds in (("二维标注","annotation-2d.png",image_scene(view,face),6),
                          ("原始深度","depth.png",image_scene(view,face,depth=True),5),
                          ("三维表面点","annotation-3d.png",scene_3d(view,face),7)):
            scene.save(out/filename)
            scenes.append((title,seconds,scene))
        scenes.append(("原始操作与终态",4,finish(view,face)))
        chapters,count = write_video(out/"task.mp4",scenes,ffmpeg,ffprobe)
        report = dict(task_id=view.task["id"], tool=view.tool, operation_id=view.op, task_elapsed_ms=view.elapsed,
                      duration_s=count/FPS, frames=count, chapters=chapters, task_sha256=view.task_sha,
                      timeline_sha256=view.record.timeline_digest, video_sha256=sha(out/"task.mp4"),
                      point_pixel=view.pt, point_camera_optical_m=view.xyz,
                      parent_gap_id=evolution.gap["id"] if evolution else None)
        (out/"video.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
        (out/"grounding.json").write_text(json.dumps(view.g,ensure_ascii=False,indent=2)+"\n",encoding="utf8")
        buttons = "".join(f'<button data-sec="{c["sec"]}">{c["sec"]:02.0f} s　{html.escape(c["title"])}</button>' for c in chapters)
        (out/"index.html").write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>目标定位与能力演化实测</title><style>*{{box-sizing:border-box}}body{{background:#101a21;color:#f4f2e8;font:17px/1.6 "Microsoft YaHei",sans-serif;margin:0}}
main{{max-width:1460px;margin:auto;padding:20px clamp(10px,3vw,46px)}}p{{color:#b7c4ca}}section{{display:grid;grid-template-columns:minmax(0,3fr) minmax(200px,1fr);gap:16px}}
aside{{background:#192833;padding:12px;border-radius:10px}}video,img{{width:100%;border-radius:10px}}button{{background:#223541;color:#fff;font:inherit;border:1px solid #516572;cursor:pointer;padding:7px;text-align:left}}
aside button{{display:block;width:100%;margin:4px 0}}a{{color:#84e2ba}}@media(max-width:780px){{section{{display:block}}}}</style>
<main><h1>目标定位与能力演化实测</h1><p>{html.escape(view.task["text"])}　·　{html.escape(view.task["id"])}</p><section>
<video controls playsinline preload="metadata" poster="annotation-2d.png" src="task.mp4"></video><aside><strong>任务录像</strong>{buttons}</aside></section>
<h2>逐帧核对</h2><p><button data-map="annotation-2d.png">实拍标注</button> <button data-map="depth.png">对齐深度</button> <button data-map="annotation-3d.png">三维表面点</button></p>
<img id="map" src="annotation-2d.png" alt="目标定位的原始证据视图"><p><a href="cloud.html">拖动查看三维点云</a>　<a href="annotated-rgb.png">原尺寸标注图</a>　<a href="task.mp4">打开录像</a>　<a href="grounding.json">定位证据</a>　<a href="task.json">任务回执</a>　<a href="video.json">完整性校验</a></p></main>
<script>const v=document.querySelector('video');for(const b of document.querySelectorAll('[data-sec]'))b.onclick=()=>{{v.currentTime=+b.dataset.sec;v.play()}};for(const b of document.querySelectorAll('[data-map]'))b.onclick=()=>document.querySelector('#map').src=b.dataset.map;</script></html>''',encoding="utf8")
    return report


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("task","recording","artifacts","out"):
        cli.add_argument("--"+name,type=Path,required=True)
    cli.add_argument("--evolution",type=Path)
    cli.add_argument("--font",required=True)
    cli.add_argument("--ffmpeg",default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe",default=shutil.which("ffprobe"))
    args = cli.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        cli.error("FFmpeg and FFprobe are required")
    view = Target(args.task,args.recording,args.artifacts)
    evolution = Evolution(args.evolution,view) if args.evolution else None
    report = render(view,evolution,args.out,args.font,args.ffmpeg,args.ffprobe)
    print(json.dumps(report,ensure_ascii=False))


if __name__ == "__main__":
    main()
