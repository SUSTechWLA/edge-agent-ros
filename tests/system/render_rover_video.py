"""Render measured Gazebo commissioning timelines with Pillow, FFmpeg and a Chinese font."""
import argparse
from bisect import bisect_right
from datetime import timedelta
from functools import lru_cache
import json
import math
from pathlib import Path
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1440, 810, 12
NAVY = "#101D29"
PANEL = "#192A39"
TEXT = "#F5F4EB"
MUTED = "#B3C6D2"
MINT = "#62E1C5"
AMBER = "#FFC772"
EMPTY = "#C6D6D9"
UNKNOWN = "#354858"
OCCUPIED = "#FF956B"


class Timeline:
    def __init__(self, folder):
        self.folder = folder
        self.rows = {}
        self.stamps = {}
        for line in (folder/"timeline.jsonl").read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            self.rows.setdefault(row["kind"], []).append(row)
        for kind, rows in self.rows.items():
            rows.sort(key=lambda r: r["stamp_ns"])
            self.stamps[kind] = [r["stamp_ns"] for r in rows]
        for kind in ("rgb","map","fused_pose","mission","bus","twist"):
            if not self.rows.get(kind):
                raise ValueError(f"missing {folder.name}/{kind} observations")
        self.end = self.stamps["rgb"][-1]
        self.start = self.stamps["rgb"][0]
        self.finish = next(r for r in self.rows["mission"] if r["phase"] == "finished")

    def seen(self, kind, stamp):
        end = bisect_right(self.stamps[kind], stamp)
        return self.rows[kind][:end]

    def last(self, kind, stamp):
        end = bisect_right(self.stamps[kind], stamp)
        return self.rows[kind][end-1] if end else {}


@lru_cache(maxsize=20)
def font(path, pixels):
    return ImageFont.truetype(path, pixels)


def mask_text(draw, xy, value, size, color=TEXT, bold=False):
    draw.text(xy, str(value), fill=color, font=font(FONTS[int(bold)],size), anchor="lt")


@lru_cache(maxsize=12)
def read_camera(path):
    with Image.open(path) as im:
        return im.convert("RGB").resize((672,448), Image.Resampling.LANCZOS)


@lru_cache(maxsize=8)
def read_grid(path):
    grid = json.loads(path.read_bytes())
    width, height = grid["width"], grid["height"]
    colors = {0:Image.new("RGB",(1,1),EMPTY).getpixel((0,0)), -1:Image.new("RGB",(1,1),UNKNOWN).getpixel((0,0)),
              100:Image.new("RGB",(1,1),OCCUPIED).getpixel((0,0))}
    image = Image.new("RGB",(width,height))
    image.putdata([colors.get(cell, colors[100] if cell>=50 else colors[0]) for cell in grid["cells"]])
    # ROS grid row zero is at the lower edge in a Cartesian map.
    return grid, image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)


def viewport(grid, half_metres=3.):
    ox, oy, theta = grid["origin"]
    spacing = grid["resolution"]
    def grid_pixel(x,y):
        dx, dy = x-ox, y-oy
        return ((dx*math.cos(theta)+dy*math.sin(theta))/spacing,
                grid["height"]-(-dx*math.sin(theta)+dy*math.cos(theta))/spacing)
    cx,cy = grid_pixel(0.,0.)
    half = round(half_metres/spacing)
    left,top = round(cx)-half,round(cy)-half
    size = 2*half
    def screen(xy):
        x,y = grid_pixel(*xy)
        return 806+(x-left)*400/size, 173+(y-top)*400/size
    return (left,top,left+size,top+size),screen


def paint_map(canvas, timeline, stamp, missions, patrol=False):
    d=ImageDraw.Draw(canvas)
    row=timeline.last("map",stamp)
    if not row:
        d.rectangle((806,173,1205,572),fill=UNKNOWN)
        mask_text(d,(935,355),"等待第一张地图",20)
        return
    grid,base=read_grid(timeline.folder/row["file"])
    area,point=viewport(grid,5. if patrol else 3.)
    canvas.paste(base.crop(area).resize((400,400),Image.Resampling.NEAREST),(806,173))
    # Each dot is one measured fused position. Do not extrapolate between poses.
    poses=timeline.seen("fused_pose",stamp)
    for pose in poses[::3]:
        if pose["frame"]!=grid["frame"]:
            continue
        x,y=point(pose["xy"])
        if 809<x<1202 and 176<y<570:
            d.ellipse((x-2,y-2,x+2,y+2),fill="#317A68")
    goals=[r["xy"] for r in missions if r.get("phase")=="goal"] if patrol else [r["goal"] for r in missions if r.get("goal")]
    if patrol:
        for observed in missions:
            if observed["phase"]=="waypoint":
                x,y=point(observed["xy"])
                d.ellipse((x-3,y-3,x+3,y+3),fill="#015A51")
    if goals:
        x,y=point(goals[-1])
        if 819<x<1192 and 185<y<560:
            d.ellipse((x-9,y-9,x+9,y+9),outline="#6D400A",width=4)
            d.ellipse((x-7,y-7,x+7,y+7),outline=AMBER,width=3)
    if poses and poses[-1]["frame"]==grid["frame"]:
        pose=poses[-1]
        x,y=point(pose["xy"])
        if 820<x<1190 and 189<y<556:
            a=pose["yaw"]-grid["origin"][2]
            triangle=[(x+math.cos(a+b)*r,y-math.sin(a+b)*r) for b,r in ((0,13),(2.45,9),(-2.45,9))]
            d.polygon(triangle,fill="#00DFAD",outline="#123E38",width=2)
    mask_text(d,(1226,179),"全图已知格",18,MUTED)
    mask_text(d,(1226,211),row["known"],36,MINT,True)
    mask_text(d,(1226,270),"已验收到点",18,MUTED)
    arrivals=sum(r["phase"]=="waypoint" if patrol else r["phase"]=="receipt" and r["core_state"]=="SUCCEEDED" for r in missions)
    submitted=next((r for r in missions if r["phase"]=="submitted"),{})
    mask_text(d,(1226,302),f"{arrivals} / {len(submitted['goals'])}" if submitted else arrivals,36,TEXT,True)
    if patrol:
        mask_text(d,(1226,362),"故障与恢复记录",18,AMBER,True)
        labels={"fault_core":"暂停 Core","retry":"查询超时重试","core_restored":"解除暂停",
                "fault_agent":"终止 Agent","agent_restored":"进程已重启"}
        notes=[r for r in missions if r["phase"] in labels]
        for i,r in enumerate(notes[-5:]):
            mask_text(d,(1225,395+i*32),f"{r['stamp_ns']/1e9:04.1f}  {labels[r['phase']]}",16,MUTED)
    else:
        mask_text(d,(1226,367),"地图分辨率",18,MUTED)
        mask_text(d,(1226,398),f"{grid['resolution']:.2f} m",24,TEXT)
        mask_text(d,(1226,463),"最新图戳",18,MUTED)
        mask_text(d,(1226,494),f"{row['stamp_ns']/1e9:.2f} s",22,TEXT)


def message(timeline, stamp):
    missions=timeline.seen("mission",stamp)
    buses=timeline.seen("bus",stamp)
    row=missions[-1] if missions else {}
    event=buses[-1]["event"] if buses else {}
    twist=timeline.last("twist",stamp)
    phase=row.get("phase")
    n=row.get("mission_id","").rsplit("/",1)[-1]
    if phase=="finished":
        if row["core_state"]=="CANCELED":
            return "取消完成 · CANCELED","网关已观察到连续静止里程计，Core 回报取消，当前地图随任务保存。"
        if row.get("reason")=="goal_budget":
            return "达到目标数上限 · PARTIAL","本轮用完探索步数，系统保存已观测的地图。浅色未覆盖到的深灰区域仍待探索。"
        return "本轮探索结束 · "+row["core_state"],"最终状态和原因已写入本轮任务回执。"
    if event.get("code")=="navigation.cancel_requested":
        if event.get("kind")=="action.result":
            quiet=event.get("data",{}).get("stop_evidence",{}).get("quiet_samples")
            return "停车已验证","网关用"+(f"{quiet} 个" if quiet else "连续")+"静止里程计样本确认车辆停稳，等待任务最终状态。"
        return "收到撤销 · 正在制动","网关已进入制动，等待实测车速稳定后才返回停车结果。"
    if phase=="receipt":
        xy=row.get("measured_xy")
        at=f"融合位姿为 ({xy[0][0]:.2f}, {xy[0][1]:.2f}) m。" if xy else ""
        return f"目标 {n} 已到达 · {row['core_state']}", at+"Core 收到到点回执，规划器会用新地图选择下一步。"
    if phase in ("accepted","requested"):
        goal=row["goal"]
        if event.get("mission_id")==row.get("mission_id") and event.get("kind")=="action.started":
            if twist and abs(twist.get("linear",0.))>.015:
                return f"执行目标 {n} · 已观测到速度指令",f"目标坐标 ({goal[0]:.2f}, {goal[1]:.2f}) m。绿色箭头是实测融合位姿，黄圈是本步目标。"
            return f"执行目标 {n} · 网关已接收",f"目标坐标 ({goal[0]:.2f}, {goal[1]:.2f}) m。车辆可先调整朝向，再向目标行驶。"
        return f"第 {n} 个子目标 · "+("Core 已接单" if phase=="accepted" else "已提交"),"规划器从可通行区与未知区的交界选点。提交成功后，还要等网关接单才能驱动车辆。"
    return "先观察，后规划","二维激光扫描经位姿配准形成占据图。开发者看到左侧全景，规划器读取右侧地图与融合位姿。"


def frame(tl,stamp,chapter,hold=False):
    canvas=Image.new("RGB",(W,H),NAVY)
    d=ImageDraw.Draw(canvas)
    mask_text(d,(37,20),"EDGE AGENT ROS    /    ROS 2 · GAZEBO    /    无人车实验证据",19,MINT,True)
    title="01   无人车如何在未知场地中探索？" if chapter=="explore" else "02   撤销导航后，车真的停下了吗？"
    mask_text(d,(36,56),title,41,TEXT,True)
    mask_text(d,(1121,28),f"仿真时间  {stamp/1e9:05.2f} s",25,TEXT,True)
    mask_text(d,(1122,68),"观测定格" if hold else "实测回放  ·  0.75 倍速",19,AMBER if hold else MUTED)
    d.line((36,116,1405,116),fill="#3B5060",width=2)
    d.rounded_rectangle((33,133,751,634),radius=13,fill=PANEL)
    d.rounded_rectangle((776,133,1407,634),radius=13,fill=PANEL)
    mask_text(d,(53,145),"A  /  仿真相机",20,TEXT,True)
    rgb=tl.last("rgb",stamp)
    if rgb:
        canvas.paste(read_camera(tl.folder/rgb["file"]),(56,173))
        mask_text(d,(461,144),f"RGB   768 × 512   ·   {rgb['stamp_ns']/1e9:.2f} s",17,MUTED)
    mask_text(d,(803,145),"B  /  车端占据地图",20,TEXT,True)
    mask_text(d,(1123,145),"局部视窗  ·  6 × 6 m",17,MUTED)
    missions=tl.seen("mission",stamp)
    paint_map(canvas,tl,stamp,missions,patrol=chapter=="mission")
    for i,(color,label) in enumerate(((UNKNOWN,"未知"),(EMPTY,"空闲"),(OCCUPIED,"占据"),(MINT,"车体"),(AMBER,"目标"))):
        x=806+i*115
        d.rounded_rectangle((x,596,x+16,612),radius=3,fill=color,outline=MUTED if i==0 else color)
        mask_text(d,(x+24,593),label,17,MUTED)
    if chapter=="mission":
        return canvas
    action,sub=message(tl,stamp)
    d.rectangle((36,657,42,733),fill=AMBER if chapter=="cancel" else MINT)
    mask_text(d,(60,659),action,32,TEXT,True)
    mask_text(d,(60,710),sub,24,MUTED)
    d.line((36,754,1405,754),fill="#3B5060",width=2)
    mask_text(d,(39,774),"探索实验" if chapter=="explore" else "取消实验 · 新启动的独立仿真",17,MINT)
    twist=tl.last("twist",stamp)
    if twist:
        mask_text(d,(744,774),f"速度指令    线速 {twist['linear']:+.2f} m/s     角速 {twist['angular']:+.2f} rad/s",18,MUTED)
    return canvas


def clock_text(seconds):
    return str(timedelta(seconds=math.floor(seconds)))[2:]


def patrol_caption(rows):
    if not rows:
        return "等待任务进入系统","摄像机开始记录，右侧地图只显示车端已经观测到的区域。"
    row=rows[-1]
    phase=row["phase"]
    if phase=="goal":
        x,y=row["xy"]
        return f"正在执行第 {row['index']} 站 / {row['total']}",f"网关导航目标为 ({x:.2f}, {y:.2f}) m。地图黄圈显示本步目标，绿色箭头为融合位姿。"
    if phase=="waypoint":
        x,y=row["xy"]
        return f"第 {row['index']} 站已到达，里程计回执已存档",f"实测到点位置 ({x:.2f}, {y:.2f}) m。只有带里程计证据的站点才计入右侧进度。"
    if phase=="verified":
        return "状态工具读回停车结果",f"实测位置 ({row['x']:.2f}, {row['y']:.2f}) m，速度 {row['speed']:.3f} m/s。"
    if phase=="finished":
        return f"同一任务成功完成 · {row['state']}",f"{row['verified_count']} 个站点都有里程计回执。末端 status 工具已取得停车位置和速度。"
    descriptions={
        "submitted":("收到一条自然语言指令","Agent 将用户的一条请求保存为一个任务。上方保留用户原文，任务 ID 在片中始终一致。"),
        "planned":("本地规划器选出 inspect → status","inspect 按顺序巡检坐标，status 随后读取车体状态。本次运行的规划提供方是 local。"),
        "fault_core":("注入故障 1 · 暂停 Core","验收脚本暂停任务执行服务，模拟查询回执超时。故障注入和后续重试都进入任务记录。"),
        "retry":("执行服务暂时无响应 · 已记录重试","Agent 查询 /edge/get_mission 超时，保留同一操作 ID，按退避策略再次查询。"),
        "core_restored":("执行服务恢复 · 继续原操作","验收脚本解除 Core 暂停。Agent 用原操作 ID 获取状态；用户没有再次提交任务。"),
        "moving":("机器人已驶离起点","独立仿真位姿检测到车辆运动，Core 与网关正在执行已经受理的巡检。"),
        "fault_agent":("注入故障 2 · 车在运动时终止 Agent 进程","现有导航由 Core 和网关继续执行。监看容器是否重启，以及 Agent 能否恢复同一任务。"),
        "agent_restored":("Agent 已重启 · 从持久任务记录续跑","重启计数增加，任务 ID、trace 和巡检操作 ID 保持一致，后续回执继续归入原任务。")}
    return descriptions.get(phase,(phase,row.get("detail","已登记任务状态。")))


def patrol_frame(tl,stamp,task_id):
    # The same measured RGB, map and controls are used for both experiment presentations.
    picture=frame(tl,stamp,"mission",False)
    d=ImageDraw.Draw(picture)
    rows=tl.seen("mission",stamp)
    phases={r["phase"] for r in rows}
    task=next((r for r in rows if r["phase"]=="submitted"),None)
    d.rectangle((29,11,1418,113),fill=NAVY)
    mask_text(d,(37,15),"EDGE AGENT ROS   /   单任务连续实录   /   ROS 2 · GAZEBO",17,MINT,True)
    mask_text(d,(34,42),"七站巡检：从工坊穿过门廊，再返回起点",34,TEXT,True)
    if task:
        mask_text(d,(37,90),"原始指令   "+task["text"],17,MUTED)
    else:
        mask_text(d,(37,90),"现场监测已就绪，等待用户提交任务。",17,MUTED)
    mask_text(d,(1099,24),f"仿真时钟  {stamp/1e9:06.2f} s",25,TEXT,True)
    mask_text(d,(1100,63),"连续播放  ·  仿真原速 1.0×",19,MINT)
    d.rectangle((1106,143,1399,169),fill=PANEL)
    mask_text(d,(1117,145),"局部视窗  ·  10 × 10 m",17,MUTED)
    d.rectangle((33,646,1410,809),fill=NAVY)
    action,sub=patrol_caption(rows)
    d.rectangle((36,648,42,719),fill=AMBER if rows and rows[-1]["phase"] in ("fault_core","retry","fault_agent") else MINT)
    mask_text(d,(60,649),action,29,TEXT,True)
    mask_text(d,(60,692),sub,22,MUTED)
    d.line((35,736,1403,736),fill="#3B5060",width=2)
    mask_text(d,(38,754),"任务  "+(task_id if task else "等待提交"),17,MUTED)
    checkpoints=[("submitted","已提交"),("planned","已规划"),("retry","超时重试"),
                 ("agent_restored","进程已恢复"),("finished","已完成")]
    for i,(phase,label) in enumerate(checkpoints):
        mask_text(d,(479+i*111,754),label,17,MINT if phase in phases else "#87959D")
    speed=tl.last("twist",stamp)
    if speed:
        mask_text(d,(1060,751),f"指令线速  {speed['linear']:+.2f} m/s",19,TEXT)
    return picture


def encode(args,target,frames,count):
    pending=target.with_suffix(".partial.mp4")
    cmd=[args.ffmpeg,"-hide_banner","-loglevel","error","-y","-f","rawvideo","-pix_fmt","rgb24",
         "-s",f"{W}x{H}","-r",str(FPS),"-i","-","-an","-c:v","libx264","-preset","veryfast",
         "-crf","20","-pix_fmt","yuv420p","-movflags","+faststart",str(pending)]
    with subprocess.Popen(cmd,stdin=subprocess.PIPE) as enc:
        try:
            for data in frames:
                enc.stdin.write(data)
        finally:
            enc.stdin.close()
        if enc.wait():
            raise RuntimeError("H.264 encoding failed")
    probe=json.loads(subprocess.check_output([args.ffprobe,"-v","error","-show_streams","-show_format",
                                              "-of","json",str(pending)]))
    video=next(s for s in probe["streams"] if s["codec_type"]=="video")
    if (video["codec_name"],video["width"],video["height"],int(video["nb_frames"]))!=("h264",W,H,count):
        raise ValueError("encoded stream does not match the evidence timeline")
    pending.replace(target)
    probe["format"]["filename"]=target.name
    (args.out/"ffprobe.json").write_text(json.dumps(probe,indent=2)+"\n",encoding="utf-8")


def patrol(args):
    import hashlib
    tl=Timeline(args.run/"media"/"mission")
    ids={r["task_id"] for r in tl.rows["mission"]}
    if ids!={args.task_id} or tl.finish.get("state")!="SUCCEEDED":
        raise ValueError(f"need one successful task matching --task-id, observed {ids}")
    stamp0=tl.start
    stop=tl.finish["stamp_ns"]+1_200_000_000
    if tl.end<stop:
        raise ValueError("missing measured post-terminal frames")
    delta=round(1e9/FPS)
    ticks=list(range(stamp0,stop,delta))+[stop]
    target=args.out/"rover-long-mission.mp4"
    encode(args,target,(patrol_frame(tl,t,args.task_id).tobytes() for t in ticks),len(ticks))
    middle=next(r["stamp_ns"] for r in tl.rows["mission"] if r["phase"]=="waypoint" and r["index"]==3)
    cover=patrol_frame(tl,middle,args.task_id)
    d=ImageDraw.Draw(cover)
    d.rectangle((29,11,1418,113),fill=NAVY)
    mask_text(d,(36,18),"EDGE AGENT ROS    /    完整单任务仿真实录",19,MINT,True)
    mask_text(d,(35,54),"无人车七站巡检：故障重试、进程恢复与返航",40,TEXT,True)
    mask_text(d,(1063,74),f"第 3 站现场   ·   {middle/1e9:.2f} s",19,AMBER)
    cover.save(args.out/"rover-long-mission-cover.png")
    submitted=next(r for r in tl.rows["mission"] if r["phase"]=="submitted")
    lines=["# 无人车七站巡检：故障重试、进程恢复与返航","",
           "[播放 H.264 录像](rover-long-mission.mp4) · [封面预览](rover-long-mission-cover.png)","",
           "用户原话为“"+submitted["text"]+"”。","",
           f"全片来自一次 Gazebo 运行和一次请求，任务 ID 为 `{args.task_id}`。"
           f"自相机首帧至任务成功后 1.2 仿真秒，连续播放 {len(ticks)/FPS:.1f} 秒，采用仿真原速 1.0×。","",
           "| 视频位置 | 仿真时钟 | 观测 |","|---|---:|---|"]
    markers=[]
    for r in tl.rows["mission"]:
        sec=max(0,r["stamp_ns"]-stamp0)/1e9
        title=patrol_caption([r])[0]
        lines.append(f"| {clock_text(sec)} | {r['stamp_ns']/1e9:.2f} s | {title} |")
        markers.append(dict(phase=r["phase"],video_seconds=sec,stamp_ns=r["stamp_ns"],title=title))
    lines.extend(["","左侧为同一次运行中的真实 Gazebo 相机图像，为便于看到车体，录制相机对屋顶与近侧两面墙采用剖视。雷达与物理碰撞仍能感知场景。右侧占据图、融合位姿、点位与速度指令均来自 ROS 或实际任务回执。浅色空闲、深灰未知、橙色占据。目标来自本次请求与网关执行记录，车体箭头来自融合位姿，历史点来自先前测量。","",
                  "传感器按消息头时间更新，任务事件使用记录器首次观测到该记录时最近的仿真时钟；相同仿真时间内的多条事件依接收顺序呈现。固定帧率下相邻事件可能在同一帧出现，所有阶段的采集时间见上表。未收到新数据时沿用最近观测，不在帧间合成车体位姿。","",
                  "本次规划使用 local 提供方；自然语言指令包含明确的七个坐标。暂停 Core 和终止 Agent 进程均由验收脚本注入，第一次恢复由脚本解除暂停，第二次进程恢复由容器重启策略与 Agent 持久任务记录完成。"])
    (args.out/"README.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    with target.open("rb") as movie:
        movie_sha=hashlib.file_digest(movie,"sha256").hexdigest()
    summary={"task_id":args.task_id,"fps":FPS,"simulation_speed":1.0,"first_camera_ns":stamp0,"last_display_ns":stop,
             "frame_count":len(ticks),"sha256":movie_sha,
             "timeline_sha256":hashlib.sha256((tl.folder/"timeline.jsonl").read_bytes()).hexdigest(),
             "input_events":len(tl.rows["mission"]),"input_rgb":len(tl.rows["rgb"]),"markers":markers}
    (args.out/"video.json").write_text(json.dumps(summary,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(summary))


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run",type=Path,required=True)
    ap.add_argument("--out",type=Path,required=True)
    ap.add_argument("--mode",choices=("exploration","mission"),default="exploration")
    ap.add_argument("--task-id",help="required for mission mode")
    ap.add_argument("--font",required=True,help="Chinese regular TTF/TTC")
    ap.add_argument("--bold-font",help="Chinese bold TTF/TTC")
    ap.add_argument("--ffmpeg",default=shutil.which("ffmpeg"))
    ap.add_argument("--ffprobe",default=shutil.which("ffprobe"))
    args=ap.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        ap.error("both ffmpeg and ffprobe are required")
    global FONTS
    FONTS=(args.font,args.bold_font or args.font)
    proof=json.loads((args.run/"acceptance.json").read_bytes())
    if not proof["passed"]:
        raise ValueError("the recording requires a passing acceptance file")
    args.out.mkdir(parents=True,exist_ok=True)
    if args.mode=="mission":
        if not args.task_id:
            ap.error("--task-id is required for the single-task film")
        patrol(args)
        return
    timelines={kind:Timeline(args.run/"media"/kind) for kind in ("explore","cancel")}
    target=args.out/"rover-exploration.mp4"
    cues=[]
    plan=[]
    for key,tl in timelines.items():
        sampled=set(range(tl.start,tl.end+1,round(.75e9/FPS)))
        holds={r["stamp_ns"] for r in tl.rows["mission"] if r["phase"] in ("finished","receipt")}
        sampled |= holds | {tl.end}
        cuts=[]
        if key=="cancel":
            accept=next((r["stamp_ns"] for r in tl.rows["mission"] if r["phase"]=="accepted"),0)
            gateway=next((r["stamp_ns"] for r in tl.rows["bus"] if r["event"]["kind"]=="action.started"),0)
            if gateway-accept>8e9:
                cuts=[(accept+2_000_000_000,gateway-1_000_000_000)]
        for stamp in sorted(sampled):
            if any(a<stamp<b for a,b in cuts):
                continue
            if stamp in holds or stamp in (tl.start,tl.end):
                cues.append((len(plan)/FPS,key,stamp,message(tl,stamp)[0]))
            show_cut=any(b<=stamp<b+300_000_000 for _,b in cuts)
            if show_cut and (not plan or not plan[-1][3]):
                cues.append((len(plan)/FPS,key,stamp,"省略等待网关接单的时段，仿真时刻在画面右上角"))
            plan.append((key,stamp,False,show_cut))
            if stamp in holds:
                plan.extend([(key,stamp,True,False)]*round(FPS*(3.0 if stamp==tl.finish["stamp_ns"] else 1.15)))
    def frames():
        previous=None
        for key,stamp,hold,cut in plan:
            sample=(key,stamp,hold,cut)
            if sample!=previous:
                pic=frame(timelines[key],stamp,key,hold)
                if cut:
                    draw=ImageDraw.Draw(pic)
                    draw.rectangle((51,178,532,220),fill=NAVY)
                    mask_text(draw,(61,188),"时间跳切 · 省略等待网关接单的时段",21,AMBER,True)
                data=pic.tobytes()
                previous=sample
            yield data
    encode(args,target,frames(),len(plan))
    tl=timelines["explore"]
    sample=next(r["stamp_ns"] for r in tl.rows["mission"] if r["phase"]=="receipt")
    cover=frame(tl,sample,"explore",True)
    d=ImageDraw.Draw(cover)
    d.rectangle((33,16,1410,110),fill=NAVY)
    mask_text(d,(37,20),"EDGE AGENT ROS   /   仿真实验记录",19,MINT,True)
    mask_text(d,(35,55),"无人车自主探索、建图与撤销停车",43,TEXT,True)
    mask_text(d,(1064,69),f"首个到点回执  ·  {sample/1e9:.2f} s",19,AMBER)
    cover.save(args.out/"rover-exploration-cover.png")
    lines=["# 无人车自主探索、建图与撤销停车","","[播放录像](rover-exploration.mp4) · [封面预览](rover-exploration-cover.png)","",
           f"H.264 · {W} × {H} · {FPS} fps · {len(plan)/FPS:.1f} 秒。右上为各次独立实验的 Gazebo 仿真时间。","",
           "| 视频位置 | 实验 | 仿真时刻 | 画面含义 |","|---|---|---:|---|"]
    for sec,kind,stamp,title in cues:
        lines.append(f"| {clock_text(sec)} | {'探索' if kind=='explore' else '取消'} | {stamp/1e9:.2f} s | {title} |")
    lines.extend(["","左侧为固定仿真相机的实际图像。右侧用截至该时刻的最近 ROS 占据栅格着色，已知格数为整幅栅格统计。箭头与轨迹点来自同坐标系的融合位姿。地图局部视窗边长 6 m，色块保留原网格边界。","",
                  "播放速度为仿真时间的 0.75 倍。时间跳切在右上仿真时钟和画面提示中可见；到点回执和终态定格供阅读。两个实验分别对齐自己的仿真时钟。帧间显示最近的真实观测，传感器画面与位姿均不插值。","",
                  "此实验使用 Gazebo 平面雷达、IMU 和轮速里程计。橙色障碍物的场景坐标供独立验收使用；规划器依据激光构建的地图选点。"])
    (args.out/"README.md").write_text("\n".join(lines)+"\n",encoding="utf-8")
    print(json.dumps({"file":str(target),"seconds":len(plan)/FPS,"frames":len(plan),"codec":"h264"}))


if __name__=="__main__":
    main()
