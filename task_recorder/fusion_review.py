"""Offline evidence and source-layer replay for one stationary spatial fusion task."""
import argparse
import html
import json
import math
from pathlib import Path
import re
import shutil

from PIL import Image, ImageDraw, ImageOps

from .map_view import map_output
from .review_media import write_video
from .sensor_review import (FPS, GOLD, GREEN, GRID, INK, MID, TaskReceipt,
                            base, clock, intro, ns, panel, read, require, sha, text)


SOURCE_COLORS = ("#69d6bd", "#b4a3f4", "#77b8f4", "#f798bb", "#bcdb81", "#fa9774")
OVERLAP = "#ffc467"


def scalar(n):
    return type(n) in (int, float) and math.isfinite(n)


def source_name(source):
    return {"mid360.front": "前置 MID-360", "mid360.rear": "后置 MID-360",
            "d435.aligned.depth": "D435 对齐深度"}.get(source, source)


def validate_result(result):
    require(result.get("type") == "perception.stationary_fusion" and result.get("clock_id") == "unix",
            "invalid stationary fusion receipt")
    ids, src, grid = result["source_ids"], result["sources"], result["grid"]
    require(2 <= len(ids) <= len(SOURCE_COLORS) and len(set(ids)) == len(ids) and set(ids) == set(src),
            "invalid fusion layer identities")
    w,h = grid["width"],grid["height"]
    require(type(w) is int and type(h) is int and 0 < w*h <= 262144 and w > 0 and h > 0 and
            scalar(grid["resolution_m"]) and grid["resolution_m"] > 0 and grid["unknown"] == "unobserved" and
            len(grid["origin_xy_m"]) == 2 and all(scalar(n) for n in grid["origin_xy_m"]), "invalid grid geometry")
    found = set()
    counts = [0] * len(ids)
    overlap = 0
    for row in grid["cells"]:
        require(isinstance(row, list) and len(row) == 4 and all(type(v) is int for v in row), "invalid fusion cell")
        x,y,hits,mask = row
        require(0 <= x < w and 0 <= y < h and (x,y) not in found and 0 < mask < (1 << len(ids))
                and hits >= mask.bit_count(), "cell coordinate, source count or mask mismatch")
        found.add((x,y))
        overlap += mask.bit_count() > 1
        for i in range(len(ids)):
            counts[i] += bool(mask & (1 << i))
    require(len(found) == result["occupied_cells"] and overlap == result["overlap_cells"],
            "published cell counts disagree with raster")
    witness = result["witness"]
    require(witness["actor_ref"] == result["actor_ref"] and witness["body_frame"] == result["body_frame"]
            and witness["clock_id"] == result["clock_id"] and ns(witness["first_ns"]) < ns(witness["last_ns"])
            and type(witness["sample_count"]) is int and witness["sample_count"] >= 2, "unbound stationarity witness")
    for k in ("max_linear_mps", "max_angular_radps", "max_displacement_m"):
        require(scalar(witness[k]) and witness[k] >= 0, "invalid stationary witness")
    for name in ids:
        source = src[name]
        require(ns(source["first_ns"]) <= ns(source["last_ns"]) and len(source["translation_m"]) == 3
                and all(scalar(n) for n in source["translation_m"]) and len(source["quaternion_xyzw"]) == 4
                and all(scalar(n) for n in source["quaternion_xyzw"]) and
                math.isclose(sum(n*n for n in source["quaternion_xyzw"]), 1., abs_tol=.01),
                "invalid capture identity, time or rigid transform")
        require(ns(witness["first_ns"]) <= ns(source["first_ns"]) <= ns(source["last_ns"]) <= ns(witness["last_ns"]),
                "stationarity witness does not cover input")
    return counts


class Fusion(TaskReceipt):
    def __init__(self, task, recording, artifacts, stream=None, *, step_index=None):
        super().__init__(task, recording, step_index=step_index)
        self.data = self.batch if self.batch.get("type") == "perception.stationary_fusion" else self.batch["fusion"]
        self.counts = validate_result(self.data)
        self.actor = self.data["actor_ref"]
        self.body = self.data["body_frame"]
        self.sources = self.data["source_ids"]
        self.raw = {}
        observed = self.batch.get("observations")
        if observed:
            require(observed["actor_ref"] == self.actor and observed["clock_id"] == "unix"
                    and observed["basis"] == "published"
                    and ns(self.events[1]["stamp_ns"]) <= ns(observed["as_of_ns"]) <= ns(self.events[2]["stamp_ns"]),
                    "fusion not bound to its original observation call")
        for name, source in self.data["sources"].items():
            reference = source["artifact_ref"]
            match = re.fullmatch(r"sha256:([0-9a-f]{64})", reference)
            require(match is not None, "missing immutable source artifact")
            paths = [artifacts / (match[1] + ext) for ext in (".json", ".png")]
            matches = [p for p in paths if p.is_file()]
            require(len(matches) == 1 and sha(matches[0]) == match[1], "missing or altered fusion input")
            if observed:
                bound = [s for s in observed["samples"] if s.get("artifact_ref") == reference
                         and s["source_id"] == name and s["frame_id"] == source["frame_id"]
                         and s["clock_id"] == source["clock_id"] and ns(s["stamp_ns"]) == ns(source["last_ns"])]
                require(len(bound) == 1, "fusion source absent from operation observation batch")
            path = matches[0]
            self.raw[name] = path
            if path.suffix == ".json":
                data = read(path)
                require(data.get("source_id") == name and data.get("frame_id") == source["frame_id"]
                        and ns(data["first_receive_ns"]) == ns(source["first_ns"])
                        and ns(data["last_receive_ns"]) == ns(source["last_ns"]),
                        "raw lidar envelope does not match the fused source")
        self.grid = self.data["grid"]
        self.witness = self.data["witness"]
        self.limits = self.data.get("limits", {})
        self.context_image = self.select_context(artifacts, stream)
        if stream:
            self.inputs["window.jsonl"] = stream

    def select_context(self, artifacts, stream):
        start = ns(self.witness["first_ns"])
        end = ns(self.witness["last_ns"])
        at = ns(self.data["stamp_ns"])
        candidates = []
        batch = self.batch.get("observations", {})
        rows = [(0, batch.get("as_of_ns", end), batch.get("samples", []))]
        if stream:
            for line in stream.read_text(encoding="utf8").splitlines():
                row = json.loads(line)
                rows.append((1, row["received_ns"], row["samples"]))
        for priority, received, samples in rows:
            for s in samples:
                if (s.get("clock_id") == "unix" and s.get("actor_ref") == self.actor
                        and s.get("media_type") == "image/png" and (priority == 0 or start <= ns(s["stamp_ns"]) <= end)
                        and ns(s["stamp_ns"]) <= ns(received)):
                    candidates.append((priority, s))
        for _, s in sorted(candidates, key=lambda pair: (pair[0], abs(ns(pair[1]["stamp_ns"]) - at))):
            match = re.fullmatch(r"sha256:([0-9a-f]{64})", s["artifact_ref"])
            require(match is not None, "invalid context frame reference")
            path = artifacts / (match[1] + ".png")
            require(path.is_file() and sha(path) == match[1], "missing or altered camera context")
            with Image.open(path) as im:
                if im.format == "PNG" and im.mode in ("RGB", "RGBA"):
                    self.raw["context/" + s["source_id"]] = path
                    return s, im.convert("RGB")
        return None


def palette(mask):
    if mask.bit_count() > 1:
        return OVERLAP
    return SOURCE_COLORS[mask.bit_length()-1]


def draw_map(draw, bounds, grid, ids, face, *, selected=None):
    """Raster index ix increases forward (+body X); iy increases left (+body Y)."""
    left,top,right,bottom = bounds
    ox,oy = grid["origin_xy_m"]
    r,w,h = grid["resolution_m"],grid["width"],grid["height"]
    step = min((right-left)/(h*r), (bottom-top)/(w*r))
    width,height = h*r*step,w*r*step
    x0 = (left+right-width)/2
    y0 = (top+bottom-height)/2
    def point(x,y):
        return (x0 + (oy+h*r-y)*step, y0 + (ox+w*r-x)*step)
    draw.rectangle((x0,y0,x0+width,y0+height), fill="#1a2930", outline="#647780", width=1)
    tick = 1
    while tick * step < 42:
        tick *= 2
    for x in range(math.ceil(ox/tick)*tick, math.floor((ox+w*r)/tick)*tick+1, tick):
        _,y = point(x,oy)
        draw.line((x0,y,x0+width,y), fill=GRID)
    for y in range(math.ceil(oy/tick)*tick, math.floor((oy+h*r)/tick)*tick+1, tick):
        x,_ = point(ox,y)
        draw.line((x,y0,x,y0+height), fill=GRID)
    for ix,iy,_,mask in grid["cells"]:
        if selected == "overlap" and mask.bit_count() < 2:
            continue
        if isinstance(selected,int):
            if not mask & (1 << selected):
                continue
            color = SOURCE_COLORS[selected]
        else:
            color = palette(mask)
        a,b = point(ox+(ix+1)*r,oy+(iy+1)*r)
        c,d = point(ox+ix*r,oy+iy*r)
        draw.rectangle((math.floor(a),math.floor(b),max(math.floor(a),math.ceil(c)-1),
                        max(math.floor(b),math.ceil(d)-1)),fill=color)
    if ox <= 0 <= ox+w*r and oy <= 0 <= oy+h*r:
        x,y = point(0,0)
        draw.polygon([(x,y-9),(x-6,y+6),(x,y+3),(x+6,y+6)], fill=INK)
    return tick


def fusion_map(view, face, selected=None):
    ids, data, grid = view.sources, view.data, view.grid
    title = (source_name(ids[selected]) + " 的回波" if isinstance(selected,int) else
             "多源共同命中" if selected=="overlap" else "静止时刻的局部融合图")
    stage = "单来源图层" if isinstance(selected,int) else "共同命中图层" if selected=="overlap" else "融合结果"
    img = base(title, "同一批数据投影到机体坐标系。深色区域未观测，栅格颜色表示命中它的传感器。", stage, view.task["id"], face)
    d = ImageDraw.Draw(img)
    panel(d,(48,190,884,717))
    panel(d,(905,190,1392,717))
    tick = draw_map(d, (65,223,868,655), grid, ids, face, selected=selected)
    text(d,78,675,f"上方为机体前方 +X     左侧为 +Y     网线间隔 {tick} m", 19, MID, face, 774)
    text(d,930,216,"机体坐标系    " + view.body, 25, INK, face, 435)
    text(d,930,263,f"{grid['width']} × {grid['height']} 格    每格 {grid['resolution_m']:g} m",20,MID,face)
    text(d,930,303,f"计入高度    {grid['z_min_m']:g} 至 {grid['z_max_m']:g} m",20,MID,face)
    d.line((931,344,1364,344),fill=GRID)
    row_height = min(42, 218 // len(ids))
    for i,name in enumerate(ids):
        y=370+i*row_height
        d.rounded_rectangle((931,y+1,946,y+17),3,fill=SOURCE_COLORS[i])
        text(d,960,y,source_name(name),21,INK,face,273)
        text(d,1248,y,f"{view.counts[i]} 格",20,INK,face,110)
    y = 370 + len(ids)*row_height
    d.rounded_rectangle((931,y+1,946,y+17),3,fill=OVERLAP)
    text(d,960,y,"多个来源共同命中",21,INK,face,278)
    text(d,1248,y,f"{data['overlap_cells']} 格",20,GOLD,face,110)
    d.line((931,y+41,1364,y+41),fill=GRID)
    text(d,930,y+64,f"合计 {data['occupied_cells']} 个命中栅格",25,GREEN,face,430)
    text(d,930,665,"同一格内的多次回波合并计数。",18,MID,face,430)
    return img


def witness_scene(view, face):
    w, data = view.witness, view.data
    img = base("轮式里程计见证了静止窗口", "用里程计的连续样本核对采集时段内的移动量和速度。", "静止判据与时间", view.task['id'],face)
    d=ImageDraw.Draw(img)
    panel(d,(48,193,787,714)); panel(d,(806,193,1392,714))
    text(d,74,219,"静止观测",27,INK,face)
    duration=(ns(w["last_ns"])-ns(w["first_ns"]))/1e6
    text(d,74,273,f"{w['sample_count']} 个样本   ·   覆盖 {duration:.1f} ms",24,GREEN,face)
    for i,(title,field,unit) in enumerate((("最大线速度","max_linear_mps","m/s"),
                                            ("最大角速度","max_angular_radps","rad/s"),
                                            ("最大平面位移","max_displacement_m","m"))):
        y = 345+i*95
        text(d,74,y,title,23,MID,face)
        text(d,366,y-2,f"{w[field]:.6f} {unit}",27,INK,face,381)
        bound=view.limits.get(field)
        if scalar(bound):
            text(d,366,y+31,f"允许上限 {bound:g} {unit}",17,MID,face)
    text(d,74,662,str(w['source_id']) + "    ·    " + str(w["frame_id"]),19,MID,face,690)
    text(d,831,218,"各来源时间窗口",26,INK,face)
    windows=[("里程计",ns(w["first_ns"]),ns(w["last_ns"]),GREEN)]
    for i,name in enumerate(view.sources):
        s=data['sources'][name]
        windows.append((source_name(name),ns(s["first_ns"]),ns(s["last_ns"]),SOURCE_COLORS[i]))
    first=min(win[1] for win in windows); end=max(win[2] for win in windows)
    for i,(name,start,end_ns,color) in enumerate(windows):
        y = 286+i*min(79,358//len(windows))
        text(d,832,y,name,19,INK,face,228)
        text(d,1110,y,f"{(end_ns-start)/1e6:.1f} ms",18,MID,face,240)
        d.line((832,y+36,1362,y+36),fill=GRID,width=4)
        a=832+530*(start-first)/max(1,end-first); b=832+530*(end_ns-first)/max(1,end-first)
        d.line((a,y+36,max(a+1,b),y+36), fill=color,width=7)
    text(d,831,664,f"{clock(first)}  至  {clock(end)}",19,MID,face,525)
    return img


def binding_scene(view,face):
    img=base("两套来源如何进入同一坐标系" if len(view.sources)==2 else "传感器如何进入同一坐标系",
             "平移和四元数来自本次任务绑定的站点外参。", "采样与外参",view.task['id'],face)
    d=ImageDraw.Draw(img)
    for i,name in enumerate(view.sources):
        s=view.data["sources"][name]
        left=48 if i%2==0 else 734
        rows = math.ceil(len(view.sources)/2)
        height = min(252, 520 // rows)
        top=198+(i//2)*height
        panel(d,(left,top,left+658,top+height-16))
        text(d,left+23,top+13,source_name(name),25,SOURCE_COLORS[i],face,280)
        text(d,left+23,top+height*.23,s["frame_id"]+"  →  "+view.body,22,INK,face,610)
        xyz=", ".join(f"{n:+.3f}" for n in s["translation_m"])
        text(d,left+23,top+height*.40,f"平移 XYZ    ({xyz}) m",19,MID,face,610)
        xyzw=", ".join(f"{n:+.3f}" for n in s["quaternion_xyzw"])
        text(d,left+23,top+height*.55,f"旋转 XYZW    ({xyzw})",19,MID,face,608)
        basis=s.get("extrinsic_basis") or s["extrinsic_ref"]
        basis_name = {"nominal_urdf":"车辆 URDF 标称安装位姿","site_configured":"站点配置"}.get(basis,basis)
        text(d,left+23,top+height*.75,"外参来源    "+str(basis_name),18,GOLD,face,610)
    if len(view.sources)<=2:
        text(d,66,507,"时间对齐",23,INK,face)
        text(d,66,555,"MID-360 使用 Jetson 收包时间。各雷达设备时钟仍独立。",23,MID,face,1298)
        text(d,66,609,"本次以静止窗口约束运动误差，外参精度须通过标定单独确认。",23,MID,face,1298)
    return img


def result_scene(view,face):
    img=base("融合结果已随原操作回执保存",view.task['text'],"核对任务终态",view.task['id'],face)
    d=ImageDraw.Draw(img)
    panel(d,(48,193,867,714));panel(d,(888,193,1392,714))
    labels=("用户提交原文","Agent 发起工具调用","工具返回融合结果","Agent 确认步骤完成")
    for i,(label,event) in enumerate(zip(labels,view.events)):
        y=237+i*90
        text(d,76,y,label,24,INK,face,460)
        text(d,571,y,clock(ns(event["stamp_ns"])),22,GREEN,face,237)
        d.line((76,y+52,836,y+52),fill=GRID)
    text(d,76,656,f"任务用时 {view.elapsed:.1f} ms",23,GOLD,face)
    text(d,915,237,"SUCCEEDED",37,GREEN,face)
    text(d,915,325,view.tool,23,INK,face,450)
    text(d,915,390,f"{len(view.sources)} 个来源",25,INK,face)
    text(d,915,441,f"{view.data['occupied_cells']} 个命中格",25,INK,face)
    text(d,915,525,"原始操作",18,MID,face)
    text(d,915,565,view.op,21,INK,face,447)
    return img


def context_scene(view, face):
    sample, photograph = view.context_image
    in_batch = any(s.get("artifact_ref") == sample["artifact_ref"] and s["source_id"] == sample["source_id"]
                   and ns(s["stamp_ns"]) == ns(sample["stamp_ns"]) for s in view.batch.get("observations",{}).get("samples",[]))
    intro_text = ("这张彩色帧来自融合操作读取的同一批原始观测，采样时间取自 ROS 图像消息。"
                  if in_batch else "选取里程计见证窗口内最接近点云结束时间的彩色帧，采样时间取自 ROS 图像消息。")
    img = base("同期 D435 现场图像", intro_text,
               "同期相机观测", view.task["id"], face)
    d = ImageDraw.Draw(img)
    panel(d, (48, 191, 864, 715))
    panel(d, (886, 191, 1392, 715))
    thumb = ImageOps.contain(photograph, (790, 495), Image.Resampling.LANCZOS)
    img.paste(thumb, (456 - thumb.width // 2, 453 - thumb.height // 2))
    stamp = ns(sample["stamp_ns"])
    text(d, 914, 225, sample["source_id"], 29, GREEN, face, 451)
    text(d, 914, 312, f"{photograph.width} × {photograph.height}   ·   RGB", 25, INK, face)
    text(d, 914, 370, clock(stamp) + "   北京时间", 24, INK, face)
    delta = (stamp - ns(view.data["stamp_ns"])) / 1e6
    text(d, 914, 429, f"相对融合采样终点   {delta:+.1f} ms", 22, MID, face, 451)
    text(d, 914, 510, "用途    观察当时现场", 23, GOLD, face)
    text(d, 914, 563, "融合栅格来源见后续图层。", 21, MID, face)
    return img


def render(view, destination, face, ffmpeg, ffprobe):
    with map_output(destination) as out:
        scenes = [("任务原文",4,intro(view,face)),("静止窗口",5,witness_scene(view,face)),("采样与外参",5,binding_scene(view,face))]
        if view.context_image:
            scene = context_scene(view, face)
            scene.save(out / "context.png")
            scenes.append(("同期现场相机", 4, scene))
        layers = []
        for i, name in enumerate(view.sources):
            img = fusion_map(view,face,i)
            filename=f"source-{i+1}.png"
            img.save(out/filename)
            layers.append({"name":source_name(name),"file":filename})
            scenes.append((source_name(name)+"图层",3,img))
        combined=fusion_map(view,face)
        combined.save(out/"fusion-map.png")
        fusion_map(view,face,"overlap").save(out/"overlap.png")
        scenes += [("机体坐标系融合图",7,combined),("原始事件与终态",4,result_scene(view,face))]
        target=out/"task.mp4"
        chapters, count = write_video(target, scenes, ffmpeg, ffprobe)
        for name,path in view.inputs.items():
            shutil.copyfile(path,out/name)
        samples=out/"samples"
        samples.mkdir()
        for path in view.raw.values():
            shutil.copyfile(path,samples/path.name)
        (out/"fusion.json").write_text(json.dumps(view.data,ensure_ascii=False,indent=2)+"\n","utf8")
        report={"task_id":view.task["id"],"operation_id":view.op,"tool":view.tool,"duration_s":count/FPS,"frames":count,
                "task_elapsed_ms":view.elapsed,"chapters":chapters,"task_sha256":view.task_sha,
                "timeline_sha256":view.record.timeline_digest,"video_sha256":sha(target),
                "occupied_cells":view.data["occupied_cells"],"overlap_cells":view.data["overlap_cells"],
                "layers":layers,"sources":view.sources,
                "context_image":view.context_image[0] if view.context_image else None}
        (out/"video.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n","utf8")
        viewer(out,view,report)
    return report


def viewer(out,view,report):
    esc=html.escape
    chapters="".join(f'<button data-sec="{c["sec"]}">{c["sec"]:02.0f} s　{esc(c["title"])}</button>' for c in report["chapters"])
    layers=[{"name":"全部融合来源","file":"fusion-map.png"},*report["layers"],{"name":"共同命中","file":"overlap.png"}]
    switches="".join(f'<button data-map="{l["file"]}">{esc(l["name"])}</button>' for l in layers)
    (out/"index.html").write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>静止传感器融合实测</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#101a21;color:#f4f2e8;font:17px/1.55 "Microsoft YaHei",sans-serif}}
main{{max-width:1460px;margin:auto;padding:22px clamp(12px,3vw,46px)}}h1{{margin:5px 0}}p{{color:#b3c2c9}}section{{display:grid;grid-template-columns:minmax(0,3fr) minmax(210px,1fr);gap:17px}}
video,img{{width:100%;border-radius:12px}}aside{{padding:15px;background:#192833;border-radius:12px}}button{{background:#223541;border:1px solid #415460;color:#fff;font:inherit;text-align:left;padding:8px;cursor:pointer}}
aside button{{display:block;width:100%;margin-top:5px}}nav{{display:flex;flex-wrap:wrap;gap:7px;margin-bottom:12px}}a{{color:#84e2ba}}@media(max-width:780px){{section{{display:block}}}}</style>
<main><h1>静止传感器融合实测</h1><p>{esc(view.task["text"])}　·　{esc(view.task["id"])}</p><section><video controls playsinline preload="metadata" poster="fusion-map.png" src="task.mp4"></video>
<aside><strong>任务与融合证据</strong>{chapters}</aside></section><p>按章节停留展示同一次融合回执和原始设备时间。</p>
<h2>检查同一栅格中的各个来源</h2><nav>{switches}</nav><img id="map" src="fusion-map.png" alt="机体坐标系下的静止命中栅格">
<p><a href="task.mp4">打开 MP4</a>　<a href="fusion.json">融合数据</a>　<a href="task.json">原任务</a>　<a href="timeline.jsonl">事件时间线</a>　<a href="video.json">完整性校验</a></p></main>
<script>const v=document.querySelector('video');for(const b of document.querySelectorAll('[data-sec]'))b.onclick=()=>{{v.currentTime=+b.dataset.sec;v.play()}};for(const b of document.querySelectorAll('[data-map]'))b.onclick=()=>document.querySelector('#map').src=b.dataset.map;</script></html>''',encoding="utf8")


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    for name in ("task","recording","artifacts","out"):
        cli.add_argument("--"+name,type=Path,required=True)
    cli.add_argument("--stream", type=Path, help="Optional archive of same-session sensor batches")
    cli.add_argument("--font",required=True)
    cli.add_argument("--ffmpeg",default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe",default=shutil.which("ffprobe"))
    args=cli.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        cli.error("FFmpeg and FFprobe are required")
    view=Fusion(args.task,args.recording,args.artifacts,args.stream)
    report=render(view,args.out,args.font,args.ffmpeg,args.ffprobe)
    print(json.dumps(report,ensure_ascii=False))


if __name__=="__main__":
    main()
