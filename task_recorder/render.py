"""Offline MP4 and chapter viewer for one exported task-recorder session. Requires Pillow and FFmpeg."""
import argparse
from bisect import bisect_right
from collections import defaultdict
from functools import lru_cache
import hashlib
import html
import json
import math
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageOps
from . import showcase, trace_view

W,H,FPS=1440,810,10
BG="#101E29"
PANEL="#192C39"
WHITE="#F4F5E9"
GRAY="#A8BECA"
GREEN="#79DED0"
AMBER="#FFC16C"
MODES={"survey":"探索","inspect":"复查","return":"返航"}


def read(path):
    return json.loads(path.read_bytes())


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream,"sha256").hexdigest()


def xy(value):
    return (isinstance(value,(list,tuple)) and len(value)==2 and
            all(type(n) in (int,float) and math.isfinite(n) for n in value))


class Recording:
    def __init__(self, root, axis):
        self.header=read(root/"recording.json")
        self.media=root/"media/mission"
        timeline=self.media/"timeline.jsonl"
        self.timeline_digest=sha(timeline)
        self.by=defaultdict(list)
        self.marks=[]
        self.all=[]
        for line in timeline.read_text(encoding="utf8").splitlines():
            r=json.loads(line)
            if r["task_id"]!=self.header["task_id"]:
                raise ValueError("unexpected task in recording")
            r["_at"]=r[axis]
            if "file" in r:
                path=(self.media/r["file"]).resolve()
                if not path.is_relative_to(self.media.resolve()) or sha(path)!=r["sha256"]:
                    raise ValueError("invalid media evidence")
                r["_path"]=path
            self.all.append(r)
            self.by[r["kind"]].append(r)
            if r.get("phase") or r["kind"] not in ("rgb","map","twist","fused_pose","world_pose","clock","bus","laser_scan","imu"):
                self.marks.append(r)
        if not self.all:
            raise ValueError("no recorded events to render")
        if not self.marks:
            self.marks=list(self.by["bus"])
        self.marks.sort(key=lambda r:(r["_at"],r["seq"]))
        self.by["_activity"]=self.marks
        self.by["_tools"]=trace_view.tool_history(self.marks)
        self.times={}
        for k,values in self.by.items():
            values.sort(key=lambda r:(r["_at"],r["seq"]))
            self.times[k]=[v["_at"] for v in values]
        self.first=min(r["_at"] for r in self.all)
        self.end=max(r["_at"] for r in self.all)

    def seen(self,k,t):
        return self.by[k][:bisect_right(self.times.get(k,[]),t)]

    def last(self,k,t):
        end=bisect_right(self.times.get(k,[]),t)
        return self.by[k][end-1] if end else {}


@lru_cache(maxsize=6)
def camera(path):
    with Image.open(path) as image:
        return ImageOps.contain(image.convert("RGB"),(706,438),Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def map_pixels(path):
    g=read(path)
    width,height=g["width"],g["height"]
    if not (0<width*height<=2_000_000 and width*height==len(g["cells"]) and g["resolution"]>0):
        raise ValueError("invalid occupancy grid")
    im=Image.frombytes("P",(width,height),bytes(0 if v<0 else 1 if v==0 else 2 if v>=50 else 3 for v in g["cells"]))
    im.putpalette([51,73,89,201,216,218,237,145,111,143,164,174]+[0]*756)
    return g,im.convert("RGB").transpose(Image.Transpose.FLIP_TOP_BOTTOM)


@lru_cache(maxsize=16)
def font(path,size):
    return ImageFont.truetype(path,size)


def paragraph(draw,pos,text,size,width,style,color=WHITE,max_lines=2):
    face=font(style,size)
    x,y=pos
    line=""
    count=1
    for char in str(text):
        if char=="\n" or face.getlength(line+char)>width:
            if count==max_lines:
                draw.text((x,y),line[:-1]+"…",font=face,fill=color,anchor="lt")
                return
            draw.text((x,y),line,font=face,fill=color,anchor="lt")
            y+=size+9
            line=""
            count+=1
        if char!="\n": line+=char
    draw.text((x,y),line,font=face,fill=color,anchor="lt")


def caption(row, guide):
    if not row:
        return "开始采集","等待本任务的第一条阶段记录。"
    if row["kind"] in {"agent_trace","agent_request","agent_state"}:
        return trace_view.title(row)
    kind=row.get("phase") or row["kind"]
    section=guide.get("phases",{}).get(kind,[])
    title=row.get("title") or (section[0] if section else kind)
    detail=row.get("text") or (section[1] if len(section)>1 else row.get("detail") or row.get("reason") or "")
    if guide.get("frontier_table") and kind in ("goal","accepted","waypoint"):
        if row.get("mode") in MODES: title=MODES[row["mode"]]+" · "+title
        if xy(row.get("goal")): detail+=f'　目标 ({row["goal"][0]:+.2f}, {row["goal"][1]:+.2f}) m。'
        if xy(row.get("frontier")): detail+=f'　前沿 ({row["frontier"][0]:+.2f}, {row["frontier"][1]:+.2f}) m。'
        if type(row.get("potential_unknown_cells")) is int: detail+=f'　邻近未知格 {row["potential_unknown_cells"]}。'
    if guide.get("frontier_table") and kind=="mapped" and type(row.get("gain")) is int: detail+=f'　作业范围新增 {row["gain"]} 个已知格。'
    if guide.get("frontier_table") and kind=="inspection_selected" and xy(row.get("target")):
        detail+=f'　复用第 {row.get("source_step","—")} 段实测位置 ({row["target"][0]:+.2f}, {row["target"][1]:+.2f}) m。'
    if guide.get("frontier_table") and kind=="inspected" and isinstance(row.get("counts"),dict):
        detail+=f'　复查范围内 {row["counts"].get("free","—")} 个空闲格；实测车速 {row.get("speed_mps","—")} m/s。'
    if guide.get("frontier_table") and kind=="home" and isinstance(row.get("home_error_m"),(int,float)): detail+=f'　位姿距起点 {row["home_error_m"]:.2f} m。'
    if row.get("state"): title+=" · "+str(row["state"])
    return str(title),str(detail)


def showcase_band(img, stamp, current, view, guide, opt):
    """Four seconds of actual samples and the provenance of the visible event."""
    d=ImageDraw.Draw(img)
    def label(x,y,text,size,color,width,lines=1):
        paragraph(d,(x,y),text,size,width,opt.font,color,lines)
    d.rectangle((0,610,W,H),fill=BG)
    d.rounded_rectangle((28,617,775,798),12,fill=PANEL)
    d.rounded_rectangle((790,617,1412,798),12,fill=PANEL)
    axis="距取样" if view.axis=="stamp_ns" else "距收到"
    label(47,628,"现场信号",20,WHITE,175)
    label(244,632,"最近 4 秒   ·   "+axis+"的时间标在各通道下方",15,GRAY,510)
    if not view.signals:
        label(47,695,"此录制没有选取数值信号",21,GRAY,680)
    for i,signal in enumerate(view.signals):
        x=48+i*242
        if i: d.line((x-10,665,x-10,777),fill="#394D58")
        reading,points=view.samples(stamp,signal)
        state=reading["status"]
        color=GREEN if state=="sample" else (AMBER if state in {"stale","invalid","no_return"} else GRAY)
        label(x,664,signal["label"],18,WHITE,224)
        value=reading["value"]
        text="—" if value is None else f'{showcase.formatted(value)} {signal["unit"]}'
        label(x,690,text,24,color,228)
        values=[v for _,v in points if v is not None]
        if values:
            low,high=min(values),max(values)
            prev=None
            for when,v in points:
                if v is None:
                    prev=None
                    continue
                px=x+int(210*(1-(stamp-when)/4_000_000_000))
                py=735 if low==high else 747-round(24*(v-low)/(high-low))
                if prev and (when-prev[0])/1e9<=signal["max_age_s"]:
                    d.line((prev[1],prev[2],px,py),fill="#78C8BB",width=2)
                else:
                    d.ellipse((px-1,py-1,px+1,py+1),fill="#78C8BB")
                prev=(when,px,py)
        age=f' · {reading["age_s"]:.1f} s' if "age_s" in reading else ""
        state_label=showcase.STATUS[state]
        if reading.get("stale") and state!="stale": state_label+=" · 超时"
        label(x,760,state_label+age,16,color,218)
    label(810,632,"任务事件",18,GREEN,270)
    title,detail=caption(current,guide)
    if current and current["kind"]=="bus":
        source=showcase.identity(current)
        title,detail=source.get("code","总线事件"),source.get("module","")
    label(810,659,title,23,WHITE,572)
    if detail: label(810,692,detail,16,GRAY,574,2)
    related=view.related(current,stamp)
    if related:
        s=related["source"]
        relation={"parent":"记录的父事件","operation":"同一操作","mission":"同一任务段","timeline":"最近另一事件 · 时间顺序"}[related["relation"]]
        code=s.get("code") or s.get("phase") or related["evidence"]["kind"]
        label(810,753,f'{relation}　#{related["evidence"]["seq"]}  {code}',15,AMBER,574,2)
    elif current:
        label(810,756,f'归档事件 #{current["seq"]}   ·   {current["kind"]}',15,GRAY,574)


def picture(record,stamp,opt,guide,view=None):
    img=Image.new("RGB",(W,H),BG)
    d=ImageDraw.Draw(img)
    def label(x,y,value,size,color=WHITE,width=1300,lines=1):
        paragraph(d,(x,y),value,size,width,opt.font,color,lines)
    label(34,18,"EDGE AGENT ROS    /    任务运行录像",19,GREEN)
    label(34,52,guide.get("title") or record.header["task_id"],37,WHITE,1110)
    label(1158,52,f"+ {(stamp-record.first)/1e9:6.1f} 秒",28,AMBER,255)
    d.rounded_rectangle((28,119,775,608),12,fill=PANEL)
    d.rounded_rectangle((790,119,1412,608),12,fill=PANEL)
    rows=view.activity(stamp) if view else record.seen("_activity",stamp)
    request=next((e.get("text") for e in rows if (e.get("phase")=="submitted" or e["kind"]=="agent_request") and e.get("text")),None)
    if request:
        label(36,97,"用户任务　"+str(request),16,GRAY,1320)
    title,detail=caption(rows[-1] if rows else None,guide)
    rgb=record.last("rgb",stamp)
    label(49,135,guide.get("camera_title","相机观测") if record.by["rgb"] else "任务事件",22)
    if rgb and rgb.get("_path"):
        frame=camera(rgb["_path"])
        img.paste(frame,(int(403-frame.width/2),int(384-frame.height/2)))
        image_age=(stamp-rgb["_at"])/1e9
        age_label="距收图" if view and opt.axis=="received_ns" else "帧龄"
        label(473,138,f'{rgb.get("width",frame.width)} × {rgb.get("height",frame.height)}  ·  {age_label} {image_age:.1f} s',16,AMBER if view and image_age>1.5 else GRAY,290)
    elif record.by["rgb"]:
        label(233,354,"等待第一帧相机观测",25,GRAY)
    else:
        for i,row in enumerate(rows[-9:]):
            name,_=caption(row,guide)
            label(52,194+43*i,f'+{(row["_at"]-record.first)/1e9:05.1f} s   {name}',21,WHITE,696)
    grid=record.last("map",stamp)
    has_tools=bool(record.by.get("agent_trace"))
    label(813,135,"车端占据地图" if record.by["map"] else ("工具执行记录" if has_tools else "采集来源"),22)
    if grid and grid.get("_path"):
        g,pixels=map_pixels(grid["_path"])
        n=min(g["width"],g["height"],max(15,round(guide.get("map_width_m",8)/g["resolution"])))
        ox,oy,a=g["origin"]
        def transform(pt):
            dx,dy=pt[0]-ox,pt[1]-oy
            return ((dx*math.cos(a)+dy*math.sin(a))/g["resolution"],g["height"]-(-dx*math.sin(a)+dy*math.cos(a))/g["resolution"])
        poses=[v for v in record.seen("fused_pose",stamp) if v.get("frame")==g["frame"] and xy(v.get("xy"))]
        center=transform(poses[-1]["xy"] if poses else (0,0))
        left=max(0,min(g["width"]-n,round(center[0]-n/2)))
        top=max(0,min(g["height"]-n,round(center[1]-n/2)))
        side=390
        img.paste(pixels.crop((left,top,left+n,top+n)).resize((side,side),Image.Resampling.NEAREST),(814,182))
        def dot(pt,r,color):
            x,y=transform(pt)
            x,y=814+(x-left)*side/n,182+(y-top)*side/n
            if 814+r<=x<=1204-r and 182+r<=y<=572-r:
                d.ellipse((x-r,y-r,x+r,y+r),fill=color,outline=BG if r>3 else None,width=2)
        for p in poses: dot(p["xy"],2,"#278075")
        goals=[r for r in rows if r.get("phase")=="goal" and xy(r.get("goal"))]
        if goals:
            if xy(goals[-1].get("frontier")):
                dot(goals[-1]["frontier"],8,"#BEA9F7")
            dot(goals[-1]["goal"],8,AMBER)
        if poses: dot(poses[-1]["xy"],6,GREEN)
        label(1221,192,"整图已知格",18,GRAY,176)
        label(1221,230,str(grid.get("known","—")),31,GREEN,177)
        label(1221,302,"距收到地图" if view and opt.axis=="received_ns" else "地图帧龄",18,GRAY,176)
        age=(stamp-grid["_at"])/1e9
        label(1221,339,f'{age:.1f} s',28,AMBER if age>1.5 else WHITE,177)
        if view and age>1.5:
            d.rounded_rectangle((790,119,1412,608),12,outline=AMBER,width=3)
            label(1130,139,"保留上一幅观测",16,AMBER,256)
        if goals:
            active=goals[-1]
            mode=MODES.get(active.get("mode"),"最近")
            label(1221,400,mode+"目标 (m)",16,GRAY,178)
            label(1221,431,f'{active["goal"][0]:+.2f}, {active["goal"][1]:+.2f}',20,AMBER,177)
            if xy(active.get("frontier")):
                label(1221,480,"前沿 (m)",16,GRAY,176)
                label(1221,512,f'{active["frontier"][0]:+.2f}, {active["frontier"][1]:+.2f}',20,"#CEB9FF",178)
        else:
            label(1221,411,"分辨率",18,GRAY,176)
            label(1221,448,f'{g["resolution"]:.2f} m',24,WHITE,177)
        label(815,579,"浅色 空闲　橙色 占据　深色 未知　紫点 前沿　黄点 目标　绿点 车",15,GRAY,580)
    elif record.by["map"]:
        label(920,354,"等待第一张地图",24,GRAY)
    elif has_tools:
        end=bisect_right(record.times["_tools"],stamp)
        recent=record.by["_tools"][max(0,end-6):end]
        if not recent:
            label(820,200,"等待实际工具调用",20,GRAY,550)
        for i,event in enumerate(recent):
            label(818,186+i*64,trace_view.title(event)[0],19,GREEN,576)
            label(818,213+i*64,f'日志 #{event["source_seq"]}　{event.get("source_utc","")}',15,GRAY,576)
    elif view and not record.by["rgb"] and view.signals:
        s=view.signals[0]
        reading,_=view.samples(stamp,s)
        label(820,198,s["label"],24,WHITE,548)
        if reading["value"] is not None:
            label(820,244,showcase.formatted(reading["value"])+" "+s["unit"],45,GREEN,548)
        else:
            label(820,245,showcase.STATUS[reading["status"]],30,GRAY,548)
        label(820,324,s["source"]+"."+s["field"],18,GRAY,548)
        d.line((820,377,1382,377),fill="#3B5361")
        for i,k in enumerate(sorted(view.sources)[:4]):
            label(820,399+i*43,k,19,WHITE,375)
            count=bisect_right(record.times.get(k,[]),stamp)
            label(1210,399+i*43,f'{count} 条',19,GREEN,150)
    else:
        kinds=[k for k in record.by if not k.startswith("_") and record.by[k]]
        for i,k in enumerate(kinds[:9]):
            label(820,194+i*42,k,20,GRAY,390)
            label(1245,194+i*42,str(bisect_right(record.times.get(k,[]),stamp)),21,GREEN,135)
    if view:
        showcase_band(img,stamp,rows[-1] if rows else None,view,guide,opt)
        return img
    highlight_phases=guide.get("highlights",[])
    highlight=next((row for row in reversed(rows) if row.get("phase") in highlight_phases),None)
    if highlight:
        h_title,h_detail=caption(highlight,guide)
        when=(highlight["_at"]-record.first)/1e9
        label(48,615,f"最近里程碑  +{when:.1f} s　{h_title}　{h_detail}",15,GREEN,1350)
    d.rectangle((34,637,40,738),fill=GREEN)
    label(59,637,title,30,WHITE,1320)
    label(59,686,detail,22,GRAY,1319,2)
    d.line((34,751,1408,751),fill="#3B5361")
    label(36,768,"任务  "+record.header["task_id"],17,GRAY,450)
    occurred={phase.get("phase") for phase in rows}
    for index,pair in enumerate(guide.get("progress",[])[:5]):
        phase,name=pair
        label(504+index*109,770,name,16,GREEN if phase in occurred else "#8A9BA5",105)
    label(1110,768,"原速 1.0×  ·  "+("来源时钟" if opt.axis=="stamp_ns" else "接收时钟"),17,GREEN,298)
    return img


def render(opt):
    if getattr(opt,"layout","classic")=="map":
        from .map_view import map_output
        with map_output(opt.out) as staged:
            return _render(argparse.Namespace(**{**vars(opt),"out":staged}))
    return _render(opt)


def _render(opt):
    record=Recording(opt.recording.resolve(),opt.axis)
    guide=read(opt.guide) if opt.guide else {}
    layout=getattr(opt,"layout","classic")
    if layout=="map":
        interval="来源时钟" if opt.axis=="stamp_ns" else "接收时钟"
        guide={**guide,"description":guide.get("map_description",
               f"画面按{interval}展示本次任务的观测与事件。地图随实际收到的栅格累计。点击交互地图可以逐次查看与重载各分段。"),
               "phases":{**guide.get("phases",{}),**guide.get("map_phases",{})}}
    view=showcase.Showcase(record,guide,opt.axis) if layout in ("showcase","map") else None
    opt.out.mkdir(parents=True,exist_ok=True)
    frames=max(FPS,math.ceil((record.end-record.first)/1e9*FPS)+1)
    if frames>opt.max_seconds*FPS: raise ValueError("time span exceeds --max-seconds; verify input clock domains")
    maps=None
    if layout=="map":
        from .map_view import MapPlayback
        from . import global_map
        base_dir=getattr(opt,"base_map",None)
        digest=getattr(opt,"base_manifest_sha256",None)
        if bool(base_dir)!=bool(digest):
            raise ValueError("map base directory and expected manifest digest must be supplied together")
        args={}
        if base_dir:
            info=global_map.read_map(base_dir,manifest_sha256=digest)
            meta=info["metadata"]
            if not meta.get("map_identity") or not meta.get("map_source",{}).get("publisher_id"):
                raise ValueError("use the saved map viewer for an artifact without explicit alignment identity")
            base=global_map.GlobalMap.load_base(base_dir,identity=meta["map_identity"],manifest_sha256=digest)
            args=dict(base=base,base_publisher_id=meta["map_source"]["publisher_id"],base_info=info)
        maps=MapPlayback(record,guide,opt.axis,view,captioner=caption,**args)
    target=opt.out/"task.mp4"
    pending=target.with_suffix(".partial.mp4")
    with subprocess.Popen([opt.ffmpeg,"-hide_banner","-loglevel","error","-y","-f","rawvideo","-pix_fmt","rgb24",
        "-s",f"{W}x{H}","-r",str(FPS),"-i","-","-an","-c:v","libx264","-preset","veryfast","-pix_fmt","yuv420p",
        "-crf","19","-movflags","+faststart",str(pending)],stdin=subprocess.PIPE) as ff:
        try:
            for i in range(frames):
                t=min(record.end,record.first+i*(10**9//FPS))
                frame=(maps.draw((t-record.first)/1e6,opt,guide) if maps and (maps.revision_ms or maps.model["diagnostics"])
                       else picture(record,t,opt,guide,view))
                if view and i==0: frame.save(opt.out/"opening.png")
                if i==frames//2: frame.save(opt.out/"cover.png")
                ff.stdin.write(frame.tobytes())
        finally:
            ff.stdin.close()
        if ff.wait(): raise RuntimeError("FFmpeg failed")
    probe=json.loads(subprocess.check_output([opt.ffprobe,"-v","error","-show_format","-show_streams","-of","json",str(pending)]))
    stream=next(e for e in probe["streams"] if e["codec_type"]=="video")
    if (stream["codec_name"],stream["width"],stream["height"],int(stream["nb_frames"]))!=("h264",W,H,frames):
        raise ValueError("encoded stream does not match requested timeline")
    pending.replace(target)
    activities=view.events if view else record.marks
    if maps:
        activities=[r for r in activities if r["kind"] not in ("overview","rgb","map","fused_pose","world_pose","laser_scan","imu")]
    markers=[{"sec":(e["_at"]-record.first)/1e9,"phase":e.get("phase",e["kind"]),"title":caption(e,guide)[0],
              "detail":caption(e,guide)[1],"event_id":e["event_id"]} for e in activities]
    for row,evidence in zip(markers,activities):
        if evidence["kind"]=="agent_trace":
            row.update({key:evidence[key] for key in ("source_seq","source_utc","source_event_id","operation_id","call_id","tool",
                       "module","layer","code","error_type","outcome","duration_ms") if key in evidence})
    summary=dict(task_id=record.header["task_id"],recording_id=record.header["recording_id"],status=record.header["status"],
                 task_status=record.header.get("task_status"),missing_sources=record.header["missing_sources"],
                 axis=opt.axis,first_ns=record.first,last_ns=record.end,fps=FPS,frames=frames,duration_s=float(probe["format"]["duration"]),
                 video_sha256=sha(target),timeline_sha256=record.timeline_digest,counts=record.header["counts"],markers=markers)
    if view:
        summary["showcase"]=dict(signals=view.signals,window_s=4,guide_sha256=sha(opt.guide) if opt.guide else None,
                                 chapters=[view.chapter(row) for row in activities if trace_view.milestone_key(row) is not None])
    if maps:
        summary["map"]=dict(segments=maps.export(opt.out),viewer="map.html",revisions=len(maps.revision_ms),
                            diagnostics=maps.model["diagnostics"])
    (opt.out/"video.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2)+"\n","utf8")
    selected=guide.get("chapters",[])
    shortlist=[(m,e) for m,e in zip(markers,activities) if not selected or m["phase"] in selected]
    buttons=[]
    chapters=[]
    seen=set()
    for row,evidence in shortlist:
        key=trace_view.milestone_key(evidence)
        if key is None or key in seen: continue
        seen.add(key)
        s=row["sec"]
        chapters.append(row)
        buttons.append(f'<button data-t="{s:.3f}" data-note="{html.escape(row["detail"],quote=True)}"><time>{int(s)//60:02}:{s%60:04.1f}</time> {html.escape(row["title"])}</button>')
    title=html.escape(guide.get("title",summary["task_id"]))
    desc=html.escape(guide.get("description","点击播放查看同一录制会话中的任务、来源时间和事件。"))
    table=[]
    def pair(v):
        return f'({v[0]:+.2f}, {v[1]:+.2f})' if xy(v) else "—"
    for row in record.marks:
        if row.get("phase")=="goal" and xy(row.get("goal")):
            mode=html.escape(MODES.get(row.get("mode"),"移动"))
            table.append(f'<tr><td>{len(table)+1}</td><td>{mode}</td><td>{pair(row.get("xy"))}</td><td>{pair(row.get("frontier"))}</td>'
                         f'<td>{pair(row["goal"])}</td><td>{int(row.get("map_stamp_ns",0))/1e9:.2f} s</td></tr>')
    coordinates=('<details><summary>查看每段移动坐标的来源</summary><p>坐标单位为米。在已知自由区与未知区的交界选择前沿，根据车体余量选出一段可执行的短目标。'
                 '复查段从已到过的位置选择与当前位置、起点均分离的可达停车点，以车体余量筛选。停车后统计一米内的栅格。返航段回到首次有效位姿。地图时间为选点时实际读取的源时间。</p><div class="scroll"><table><thead>'
                 '<tr><th>段</th><th>目的</th><th>计算时车位</th><th>候选前沿</th><th>提交目标</th><th>地图时钟</th></tr></thead>'
                 '<tbody>'+"".join(table)+'</tbody></table></div></details>') if table and guide.get("frontier_table") else ""
    history=record.by["_tools"]
    tool_table=""
    if history:
        names={e["operation_id"]:e.get("tool") or e.get("module") for e in history if e["phase"]=="tool.request"}
        tool_rows=[]
        for e in history:
            fields=[e["operation_id"],e.get("tool") or names.get(e["operation_id"],"—"),caption(e,guide)[0],
                    str(e.get("source_seq","—")),e.get("source_utc","—"),e.get("error_type") or e.get("code","—")]
            tool_rows.append("<tr>"+"".join("<td>"+html.escape(str(v))+"</td>" for v in fields)+"</tr>")
        tool_table=('<details open><summary>工具执行记录</summary><p>每行对应 Agent 持久日志中的一条证据。操作 ID 串起意图、调用、回执与验证。'
                    '表中保留各阶段的首次证据，重复轮询可按序号在原时间线核对。UTC 是 Agent 写入日志的时间。</p>'
                    '<div class="scroll"><table><thead><tr><th>操作 ID</th><th>工具</th><th>阶段 / 结果</th><th>日志序号</th><th>源 UTC</th><th>类型 / 代码</th>'
                    '</tr></thead><tbody>'+"".join(tool_rows)+'</tbody></table></div></details>')
    map_link='<a href="map.html">查看地图回放与观测记录</a>　·　' if maps else ""
    (opt.out/"index.html").write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title><style>
*{{box-sizing:border-box}}body{{margin:0;background:#f0f4f1;color:#19332f;font:16px/1.6 "Microsoft YaHei",sans-serif}}
main{{max-width:1440px;margin:auto;padding:25px clamp(14px,4vw,46px)}}h1{{font-size:clamp(25px,4vw,45px);margin:9px 0}}
section{{display:grid;grid-template-columns:minmax(0,2.65fr) minmax(215px,1fr);gap:18px;align-items:start}}
video{{width:100%;border-radius:10px;background:#101e29}}aside{{padding:14px;background:white;border-radius:10px}}
h2{{font-size:19px;margin:0 0 8px}}button{{width:100%;border:0;border-top:1px solid #d2ded9;background:white;padding:8px;color:inherit;font:inherit;text-align:left;cursor:pointer}}button:hover,button:focus-visible{{background:#ddede5}}time{{color:#136758}}
a{{color:#105b45}}footer{{margin-top:12px;overflow-wrap:anywhere}}details{{margin-top:20px;background:white;padding:14px;border-radius:10px}}summary{{cursor:pointer;font-weight:600}}.scroll{{overflow:auto}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #d2ddd7;text-align:left;white-space:nowrap}}
section>*{{min-width:0}}#note,h1,button{{overflow-wrap:anywhere}}@media(max-width:780px){{section{{grid-template-columns:minmax(0,1fr)}}}}</style>
<main><h1>{title}</h1><p>{desc}</p><section><div><video controls playsinline preload="metadata" src="task.mp4" poster="cover.png"></video><p id="note" aria-live="polite"></p></div>
<aside><h2>跳到现场</h2>{"".join(buttons)}</aside></section>{tool_table}{coordinates}<footer>任务 <code>{html.escape(summary["task_id"])}</code>　·　
{map_link}<a href="task.mp4">播放 MP4</a>　<a href="video.json">查看时间索引与证据哈希</a></footer></main>
<script>const v=document.querySelector("video");for(const b of document.querySelectorAll("button"))b.onclick=()=>{{document.querySelector("#note").textContent=b.textContent+"　"+b.dataset.note;v.currentTime=+b.dataset.t;v.play().catch(()=>{{}});}};</script></html>''',"utf8")
    notes=["# "+guide.get("title",summary["task_id"]),"","[打开分章回放](index.html) · [播放完整 MP4](task.mp4)","",
           guide.get("description","本回放按时间列出录制会话中的任务和事件。"),"",
           f'任务 `{summary["task_id"]}`。全长 {summary["duration_s"]:.1f} 秒，{W} × {H}，{FPS} 帧/秒。'
           +"速率为所选时间轴的 1.0×，相邻观测之间保留最近一次实际收到的样本。","",
           "| 片中时间 | 任务阶段 |","| --- | --- |"]
    for row in chapters:
        notes.append(f'| {row["sec"]:.2f} 秒 | {row["title"].replace("|","/")} |')
    notes+=["","来源计数与 SHA-256 指纹见[时间索引](video.json)。"]
    if maps:
        notes+=["","[交互式探索地图](map.html)"]
        for s in summary["map"]["segments"]:
            notes.append(f'- [查看已保存的整图 · 分段 {s["segment"]}]({s["folder"]}/index.html) · 清单 SHA-256 `{s["manifest_sha256"]}`')
    (opt.out/"README.md").write_text("\n".join(notes)+"\n","utf8")
    return summary


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--recording",required=True,type=Path)
    cli.add_argument("--out",required=True,type=Path)
    cli.add_argument("--guide",type=Path,help="optional presentation labels and selectable chapter phases")
    cli.add_argument("--axis",choices=("stamp_ns","received_ns"),default="received_ns")
    cli.add_argument("--layout",choices=("classic","showcase","map"),default="classic",help="map prioritizes measured occupancy and its on-disk revisions")
    cli.add_argument("--base-map",type=Path,help="trusted, explicitly aligned prior map for this recording")
    cli.add_argument("--base-manifest-sha256",help="pinned SHA-256 of the prior manifest")
    cli.add_argument("--font",required=True,help="path to a CJK font")
    cli.add_argument("--max-seconds",type=int,default=3600)
    cli.add_argument("--ffmpeg",default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe",default=shutil.which("ffprobe"))
    opts=cli.parse_args()
    if not (opts.ffmpeg and opts.ffprobe): cli.error("FFmpeg and FFprobe are required")
    result=render(opts)
    print(json.dumps({k:result[k] for k in ("task_id","frames","duration_s","video_sha256")}))
