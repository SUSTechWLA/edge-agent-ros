"""Render a verified sensor-read receipt together with the surrounding real acquisition window."""
import argparse
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from functools import cached_property, lru_cache
import html
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageOps
from edge_contracts.trace_content import encoded

from .map_view import map_output
from .render import Recording, font, sha


W, H, FPS = 1440, 810, 10
BG, PANEL, INK, MID = "#101a21", "#192833", "#f4f2e8", "#a8bac1"
GREEN, GOLD, GRID = "#84e2ba", "#f1bd72", "#344756"
CN = timezone(timedelta(hours=8))
NANO = 1_000_000_000


def require(value, message):
    if not value:
        raise ValueError(message)


def read(path):
    return json.loads(path.read_bytes())


def ns(value):
    require(type(value) in (int, str) and re.fullmatch(r"[0-9]{1,19}", str(value)), "invalid nanosecond time")
    return int(value)


def clock(stamp):
    return datetime.fromtimestamp(stamp // NANO, CN).strftime("%H:%M:%S") + f".{stamp % NANO // 1000000:03d}"


def age(sample, when):
    return (when - ns(sample["stamp_ns"])) / 1e6


class TaskReceipt:
    """Match a completed tool receipt with its original request, call and recorder terminal."""
    def __init__(self, task, recording, *, step_index=None):
        self.task = read(task)
        self.inputs = {"task.json": task, "recording.json": recording / "recording.json",
                       "timeline.jsonl": recording / "media/mission/timeline.jsonl"}
        self.task_sha = sha(task)
        self.record = Recording(recording.resolve(), "received_ns")
        header = self.record.header
        require(header["task_id"] == self.task["id"] and header["status"] == "FINISHED" and
                header["task_status"] == self.task["state"] == "SUCCEEDED",
                "task/recorder identity or terminal status mismatch")
        trigger = self.task.get("trigger", {})
        if trigger.get("source") == "plugin.supervisor" and trigger.get("kind") == "agent":
            # The recorder emits agent_request only for a user. The continuation
            # has its own native acceptance, causally linked to the publication.
            require(set(header["missing_sources"]) <= {"agent_request"}, "missing continuation trace or state")
            request, = (e for e in self.record.by["agent_trace"] if e["phase"] == "accepted")
            original, = (e for e in self.task["events"] if e["event_id"] == request["source_event_id"])
            payload = json.loads(original["payload"])
            require(original["kind"] == "accepted" and request["parent_event_id"] == trigger["parent_event_id"]
                    and payload["trace"]["trace_id"] == trigger["trace_id"] == self.task["trace_id"]
                    and payload["request"]["sha256"] == hashlib.sha256(encoded(self.task["text"])).hexdigest(),
                    "continuation does not match its recorded release and original text")
        else:
            require(not header["missing_sources"], "missing user task source")
            request, = self.record.by["agent_request"]
            require(request["text"] == self.task["text"], "recorded request mismatch")
        if step_index is None:
            require(len(self.task["steps"]) == 1, "expected one completed task operation")
            step_index = 0
        require(type(step_index) is int and 0 <= step_index < len(self.task["steps"]), "step outside task")
        self.op = f"{self.task['id']}/step/{step_index + 1}"
        step = self.task["steps"][step_index]
        require(step["status"] == "SUCCEEDED" and step.get("operation_id") == self.op,
                "expected one completed task operation")
        self.tool = step["tool"]
        self.batch = step["evidence"]
        self.events = [request]
        for phase in ("tool.request", "tool.response", "verified"):
            rows = [r for r in self.record.by["agent_trace"] if r.get("phase") == phase and
                    r.get("operation_id") == self.op and (r.get("tool") or r["module"]) == self.tool]
            require(len(rows) == 1, "missing or ambiguous recorded operation " + phase)
            self.events.append(rows[0])
        require(self.events[1].get("call_id") and self.events[1]["call_id"] == self.events[2]["call_id"]
                and self.events[2]["outcome"] == "SUCCEEDED", "tool response did not match its recorded request")
        self.elapsed = (ns(self.events[-1]["stamp_ns"]) - ns(request["stamp_ns"])) / 1e6


class Review(TaskReceipt):
    def __init__(self, task, recording, artifacts, stream):
        super().__init__(task, recording)
        self.artifacts = artifacts
        if stream:
            self.inputs["window.jsonl"] = stream
        self.at = ns(self.batch["as_of_ns"])
        self.samples = self.batch["samples"]
        self.actor = self.batch["actor_ref"]
        require(self.batch.get("basis") == "published" and self.batch["clock_id"] == "unix",
                "requires real published samples on a common Unix receive clock")
        require(ns(self.events[1]["stamp_ns"]) <= self.at <= ns(self.events[2]["stamp_ns"]),
                "sensor batch was not read during the recorded tool call")
        self.rgb = self.find_image(self.samples, depth=False)
        require(self.rgb is not None, "the receipt contains no color PNG")
        self.depth = self.find_image(self.samples, depth=True)
        self.clouds = []
        for s in self.samples:
            self.check_sample(s, self.at)
            if s.get("media_type") == "application/json":
                data = self.payload(s["artifact_ref"])
                if data.get("type") == "livox.mid360.preview":
                    self.validate_cloud(s, data)
                    self.clouds.append((s, data))
        self.clouds.sort(key=lambda pair: pair[0]["source_id"])
        self.window = []
        self.stream_sha = None
        if stream:
            self.stream_sha = sha(stream)
            for line in stream.read_text(encoding="utf8").splitlines():
                row = json.loads(line)
                stamp = ns(row["received_ns"])
                require(not self.window or self.window[-1][0] <= stamp, "acquisition window clock regressed")
                for sample in row["samples"]:
                    self.check_sample(sample, stamp)
                self.window.append((stamp, row["samples"], row.get("source_gaps", [])))
            require(len(self.window) >= 2 and self.window[0][0] <= self.at <= self.window[-1][0] and
                    self.window[-1][0] - self.window[0][0] <= 90 * NANO,
                    "acquisition window must overlap this original call and cover at most 90 seconds")
        self.times = [row[0] for row in self.window]

    @lru_cache(maxsize=1024)
    def artifact(self, reference, media_type):
        match = re.fullmatch(r"sha256:([a-f0-9]{64})", reference)
        suffix = {"image/png": ".png", "application/json": ".json"}.get(media_type)
        require(match and suffix, "unsupported sensor artifact")
        path = self.artifacts / (match[1] + suffix)
        require(path.is_file() and path.stat().st_size <= 16 * 1024 * 1024 and sha(path) == match[1],
                "sensor artifact digest mismatch or missing: " + path.name)
        return path

    @lru_cache(maxsize=128)
    def payload(self, reference):
        return read(self.artifact(reference, "application/json"))

    @lru_cache(maxsize=48)
    def image(self, reference):
        with Image.open(self.artifact(reference, "image/png")) as src:
            require(src.format == "PNG" and 0 < src.width * src.height <= 4_194_304, "invalid color image")
            return src.copy()

    def find_image(self, samples, *, depth):
        modes = ("I", "I;16", "I;16B", "I;16L") if depth else ("RGB", "RGBA")
        return next((s for s in samples if s.get("media_type") == "image/png"
                     and self.image(s["artifact_ref"]).mode in modes), None)

    def check_sample(self, sample, when):
        require(sample.get("actor_ref") == self.actor and sample.get("clock_id") == "unix"
                and isinstance(sample.get("source_id"), str) and isinstance(sample.get("frame_id"), str)
                and ns(sample["stamp_ns"]) <= when, "invalid sensor sample source, scope or clock")
        if sample["kind"] == "artifact":
            self.artifact(sample["artifact_ref"], sample["media_type"])

    def validate_cloud(self, sample, cloud):
        require(cloud["source_id"] == sample["source_id"] and cloud["frame_id"] == sample["frame_id"]
                and cloud["stamp_basis"] == "host_packet_receive"
                and ns(cloud["last_receive_ns"]) == ns(sample["stamp_ns"]), "cloud source/receive stamp mismatch")
        points = cloud["points"]
        require(isinstance(points, list) and 0 < len(points) <= 65536 and cloud["sampled_point_count"] == len(points)
                and cloud["valid_point_count"] >= len(points)
                and all(len(p) == 4 and all(type(v) in (int, float) and math.isfinite(v) for v in p)
                        for p in points), "invalid cloud points or count")

    def position(self, samples):
        return next((s for s in samples if s["kind"] == "position"), None)

    @cached_property
    def depth_scale(self):
        raw = self.image(self.depth["artifact_ref"])
        positive = sorted(x for x in pixels(raw) if x > 0)
        return tuple(positive[int((len(positive)-1) * q)] for q in (.02, .98)) if positive else (0, 0)

    @lru_cache(maxsize=32)
    def painted_depth(self, ref, thumbnail=False):
        raw = self.image(ref)
        if thumbnail:
            raw = ImageOps.contain(raw, (259, 153), Image.Resampling.NEAREST)
        low, high = self.depth_scale
        lookup, palette = depth_colors(low, high)
        points = list(pixels(raw))
        field = Image.frombytes("P", raw.size, bytes(lookup[v] for v in points))
        field.putpalette(palette)
        return field.convert("RGB"), (len(points) - points.count(0)) / len(points)


def pixels(image):
    # Pillow 12+ returns a tuple; earlier deployed render environments expose getdata.
    return getattr(image, "get_flattened_data", image.getdata)()


def text(draw, x, y, content, size, color, face, width=None):
    content = str(content)
    f = font(face, size)
    if width:
        while size > 13 and draw.textlength(content, font=f) > width:
            size -= 1
            f = font(face, size)
    draw.text((x, y), content, font=f, fill=color, anchor="lt")


def panel(draw, box):
    draw.rounded_rectangle(box, radius=15, fill=PANEL)


def base(title, detail, stage, task_id, face):
    image = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(image)
    text(d, 48, 28, "Edge Agent  /  传感器实测", 22, GREEN, face)
    text(d, 48, 66, title, 43, INK, face, 1070)
    text(d, 49, 130, detail, 21, MID, face, 1335)
    d.line((48, 171, 1392, 171), fill=GRID, width=2)
    d.line((48, 738, 1392, 738), fill=GRID, width=2)
    text(d, 48, 758, task_id, 20, MID, face, 840)
    text(d, 995, 759, stage, 20, GREEN, face, 390)
    return image


def sensor_name(sample):
    source = sample["source_id"]
    if sample["kind"] == "position":
        return "轮式里程计" if "wheel" in source else "位置观测"
    if source == "d435.color.image":
        return "D435 彩色图像"
    if source == "d435.color.info":
        return "D435 彩色相机内参"
    if source == "d435.aligned.depth":
        return "D435 对齐深度"
    if source == "mid360.front":
        return "前置 MID-360"
    if source == "mid360.rear":
        return "后置 MID-360"
    return source


def intro(view, face):
    img = base("边缘端收到自然语言任务", "来自任务录制器的原始请求和 Agent 调用记录", "任务请求", view.task["id"], face)
    d = ImageDraw.Draw(img)
    panel(d, (48, 199, 1392, 449))
    text(d, 79, 229, "用户原文", 25, GREEN, face)
    text(d, 79, 298, view.task["text"], 49, INK, face, 1270)
    text(d, 79, 384, clock(ns(view.events[0]["stamp_ns"])) + "   北京时间", 23, MID, face)
    for x, heading, value in ((49, "执行主体", view.actor), (334, "Agent 选择的工具", view.tool),
                              (864, "工具参数", json.dumps(view.task["steps"][0]["args"], ensure_ascii=False))):
        text(d, x, 511, heading, 22, MID, face)
        text(d, x, 555, value, 29, INK, face, 475)
    text(d, 49, 673, "录像按阶段停留以便阅读，时间线保留每个事件在设备上的真实时间。", 22, MID, face)
    return img


def photo(img, view, sample, box):
    x, y, right, bottom = box
    picture = ImageOps.contain(view.image(sample["artifact_ref"]), (right - x, bottom - y),
                               Image.Resampling.LANCZOS)
    img.paste(picture, (x + (right - x - picture.width) // 2, y + (bottom - y - picture.height) // 2))


def sample_panel(img, view, samples, gaps, when, face):
    d = ImageDraw.Draw(img)
    panel(d, (835, 197, 1392, 711))
    text(d, 865, 224, "本批传感器", 27, INK, face)
    text(d, 1267, 229, "样本帧龄", 18, MID, face)
    height = min(73, 355 // max(1, len(samples) + len(gaps)))
    for i, s in enumerate(samples):
        y = 278 + height * i
        text(d, 865, y, sensor_name(s), 22, INK, face, 300)
        detail = s["frame_id"]
        if s.get("media_type") == "application/json":
            info = view.payload(s["artifact_ref"])
            if info.get("type") == "livox.mid360.preview":
                detail += f"   ·   {info['valid_point_count']:,} 有效点"
            elif info.get("width") and info.get("height"):
                detail += f"   ·   {info['width']} × {info['height']}"
        text(d, 865, y + 28, detail, 16, MID, face, 355)
        text(d, 1234, y + 7, f"{age(s, when):.1f} ms", 22, GREEN, face, 137)
        d.line((865, y + height - 10, 1363, y + height - 10), fill=GRID)
    for i, gap in enumerate(gaps, len(samples)):
        y = 278 + height * i
        text(d, 865, y, gap.get("source_id", "事实网关"), 20, GOLD, face, 280)
        text(d, 1150, y + 2, "该批次缺样", 18, GOLD, face, 210)
    pos = view.position(samples)
    if pos:
        xy = pos["position_m"]
        text(d, 865, 658, f"里程计位置    X {xy[0]:+.4f} m     Y {xy[1]:+.4f} m", 22, GOLD, face, 496)


def color_view(view, samples, when, face, *, live, gaps=()):
    stage = "采集窗口  ·  原速播放" if live else "任务回执  ·  单帧停留"
    detail = ("同一主体连续发布的实测采样，图像按 ROS 采样时间标注。" if live else
              "画面和读数取自该操作回执，通过 SHA-256 核对原始 PNG。")
    img = base("现场采集窗口" if live else "这次任务实际读到了什么", detail, stage, view.task["id"], face)
    d = ImageDraw.Draw(img)
    panel(d, (48, 197, 814, 711))
    rgb = view.find_image(samples, depth=False)
    if rgb:
        photo(img, view, rgb, (61, 207, 801, 645))
        text(d, 69, 666, sensor_name(rgb) + "    " + clock(ns(rgb["stamp_ns"])), 21, INK, face, 710)
    else:
        text(d, 76, 407, "该批次未收到彩色帧", 28, MID, face)
    sample_panel(img, view, samples, gaps, when, face)
    return img


@lru_cache(maxsize=4)
def depth_colors(low, high):
    lookup = bytes(0 if k == 0 else 1 + round(254 * max(0, min(1, (k - low) / max(1, high - low))))
                   for k in range(65536))
    anchors = [(43, 34, 98), (38, 114, 157), (79, 193, 167), (246, 221, 118)]
    palette = [13, 19, 26]
    for k in range(255):
        x = k * (len(anchors) - 1) / 254
        i = min(int(x), len(anchors) - 2)
        palette.extend(round(a + (b - a) * (x - i)) for a, b in zip(anchors[i], anchors[i+1]))
    return lookup, palette


def depth_view(view, face):
    rgb, depth = view.rgb, view.depth
    low, high = view.depth_scale
    field, valid = view.painted_depth(depth["artifact_ref"])
    img = base("彩色图像与对齐深度", "两幅图来自同一任务回执。深度图使用 16 位原始数值着色，零值显示为深黑。",
               "深度回执  ·  单帧停留", view.task["id"], face)
    d = ImageDraw.Draw(img)
    for left, sample, picture, title in ((48, rgb, view.image(rgb["artifact_ref"]), "彩色图像"),
                                         (728, depth, field, "深度原始采样")):
        panel(d, (left, 197, left + 663, 711))
        text(d, left + 22, 217, title, 24, INK, face)
        text(d, left + 425, 221, clock(ns(sample["stamp_ns"])), 18, GREEN, face)
        image = ImageOps.contain(picture, (629, 367), Image.Resampling.NEAREST if sample == depth else Image.Resampling.LANCZOS)
        img.paste(image, (left + (663 - image.width)//2, 271 + (367 - image.height)//2))
        text(d, left + 23, 658, (f"{picture.width} × {picture.height}    {sample['frame_id']}" if sample == rgb else
                                 f"伪彩范围 P2–P98    {low:,} 至 {high:,}   ·   非零 {valid:.1%}"),
             19, MID, face, 620)
    return img


def extent_of(points):
    furthest = max(1.0, max(max(abs(p[0]), abs(p[1])) for p in points))
    scale = 10 ** math.floor(math.log10(furthest))
    return math.ceil(furthest / scale) * scale


def plot_cloud(d, points, center, radius, extent, dot, face):
    cx, cy = center
    for frac in (.5, 1.0):
        r = radius * frac
        d.ellipse((cx-r, cy-r, cx+r, cy+r), outline=GRID)
    d.line((cx-radius, cy, cx+radius, cy), fill=GRID)
    d.line((cx, cy-radius, cx, cy+radius), fill=GRID)
    for x, y, _z, intensity in points:
        if abs(x) > extent or abs(y) > extent:
            continue
        px, py = cx + x*radius/extent, cy - y*radius/extent
        k = min(1, max(0, intensity/255)) ** .45
        color = tuple(int(lo + k*(hi-lo)) for lo,hi in ((74,235), (151,249), (128,194)))
        d.ellipse((px-dot, py-dot, px+dot, py+dot), fill=color)
    d.ellipse((cx-3, cy-3, cx+3, cy+3), fill=GOLD)
    text(d, cx+radius+5, cy-8, "+X", 13, MID, face)
    text(d, cx+6, cy-radius-14, "+Y", 13, MID, face)


def live_scene(view, samples, stamp, gaps, face):
    if not view.depth or len(view.clouds) != 2:
        return color_view(view, samples, stamp, face, live=True, gaps=gaps)
    img = base("Jetson 上的真实传感器数据", "采集窗口按原速播放，每个面板保留各自的采样时刻。", "现场采集  ·  1×", view.task["id"], face)
    d = ImageDraw.Draw(img)
    sources = {s["source_id"]: s for s in samples}
    panel(d, (48, 194, 753, 713))
    text(d, 70, 213, "D435 彩色图像", 23, INK, face)
    rgb = view.find_image(samples, depth=False)
    if rgb:
        photo(img, view, rgb, (58, 253, 742, 668))
        text(d, 70, 674, clock(ns(rgb["stamp_ns"])) + f"    帧龄 {age(rgb,stamp):.1f} ms", 17, MID, face)
    else:
        text(d, 125, 427, "本次采样缺少画面", 23, GOLD, face)
    panel(d, (770, 194, 1088, 440))
    text(d, 786, 212, "D435 对齐深度", 21, INK, face)
    depth = view.find_image(samples, depth=True)
    if depth:
        thumb, _ = view.painted_depth(depth["artifact_ref"], True)
        img.paste(thumb, (929-thumb.width//2, 249+(153-thumb.height)//2))
        text(d, 787, 408, clock(ns(depth["stamp_ns"])) + "  ·  伪彩", 16, MID, face)
    else:
        text(d, 800, 308, "本批次缺样", 24, GOLD, face)
    panel(d, (1103, 194, 1392, 440))
    text(d, 1120, 212, "轮式里程计", 21, INK, face)
    pos = view.position(samples)
    if pos:
        for axis, value, y in zip("XY", pos["position_m"], (266, 322)):
            text(d, 1120, y, axis, 23, MID, face)
            text(d, 1160, y-1, f"{value:+.4f} m", 27, GOLD, face, 210)
        text(d, 1120, 408, clock(ns(pos["stamp_ns"])) + "  ·  " + pos["frame_id"], 16, MID, face, 255)
    else:
        text(d, 1120, 308, "本批次缺样", 24, GOLD, face)
    for i, (sensor, original) in enumerate(view.clouds):
        left, right = ((770, 1088), (1103, 1392))[i]
        panel(d, (left, 453, right, 713))
        text(d, left+16, 472, sensor_name(sensor) + " 俯视", 20, INK, face)
        s = sources.get(sensor["source_id"])
        if not s:
            text(d, left+16, 556, "本批次缺样", 24, GOLD, face)
            continue
        cloud = view.payload(s["artifact_ref"])
        view.validate_cloud(s, cloud)
        extent = extent_of(original["points"])
        plot_cloud(d, cloud["points"], ((left+right)//2, 592), 70, extent, .9, face)
        text(d, left+16, 677, f"外圈 {extent:g} m   ·   {clock(ns(s['stamp_ns']))}", 15, MID, face, right-left-30)
    return img


def clouds(view, face):
    title = "激光回波在各自雷达坐标系中的分布"
    img = base(title, "俯视投影使用 X、Y 米制坐标，点的亮度表示反射强度。时间为 Jetson 网口接收时刻。", "点云回执  ·  单批停留", view.task["id"], face)
    d = ImageDraw.Draw(img)
    count = len(view.clouds)
    width = (1344 - (count - 1) * 18) // count
    for i, (s, cloud) in enumerate(view.clouds):
        left = 48 + i * (width + 18)
        panel(d, (left, 197, left + width, 711))
        text(d, left + 21, 220, sensor_name(s), 25, INK, face, width - 190)
        text(d, left + width - 168, 225, s["frame_id"], 17, MID, face, 150)
        points = cloud["points"]
        cx, cy, radius = left + width // 2, 438, min(152, width // 2 - 35)
        extent = extent_of(points)
        plot_cloud(d, points, (cx,cy), radius, extent, 1.4, face)
        text(d, left + 22, 611, f"外圈 {extent:g} m    预览 {len(points):,} 点    有效 {cloud['valid_point_count']:,} 点", 20, INK, face, width - 43)
        text(d, left + 22, 658, f"收到 {cloud['packet_count']} 包    {clock(ns(cloud['last_receive_ns']))}", 19, MID, face, width - 43)
    return img


def finish(view, face):
    img = base("任务已完成，可回查原始证据", "录制器和任务账本均记录了本次操作的成功终态", "原始事件与回执", view.task["id"], face)
    d = ImageDraw.Draw(img)
    panel(d, (48, 197, 875, 711))
    panel(d, (896, 197, 1392, 711))
    text(d, 76, 224, "设备事件时间线", 29, INK, face)
    labels = ("提交自然语言任务", "Agent 调用传感器工具", "工具返回成功回执", "Agent 确认步骤完成")
    for i, (e, label) in enumerate(zip(view.events, labels)):
        y = 304 + i * 79
        d.ellipse((83, y + 7, 95, y + 19), fill=GREEN)
        if i != len(labels) - 1:
            d.line((89, y + 22, 89, y + 80), fill=GRID, width=2)
        text(d, 117, y, label, 23, INK, face)
        text(d, 586, y + 2, clock(ns(e["stamp_ns"])), 24, GREEN, face)
    text(d, 76, 657, f"从任务提交至步骤确认    {view.elapsed:.1f} ms", 23, GOLD, face, 765)
    text(d, 924, 231, "SUCCEEDED", 35, GREEN, face)
    text(d, 924, 311, f"{len(view.samples)} 路传感器样本", 29, INK, face)
    text(d, 924, 369, f"{len(view.record.all)} 条录制事件", 27, INK, face)
    text(d, 924, 444, "原操作号", 19, MID, face)
    text(d, 924, 479, view.op, 21, INK, face, 439)
    text(d, 924, 564, "证据批次读取时间", 19, MID, face)
    text(d, 924, 601, clock(view.at) + "   北京时间", 23, INK, face)
    return img


def render(view, destination, face, ffmpeg, ffprobe):
    scenes = [("任务原文", 4.0, intro(view, face))]
    if view.window:
        length = (view.window[-1][0] - view.window[0][0]) / NANO
        scenes.append(("现场采集窗口", length, None))
    scenes.append(("任务读取的彩色帧与里程计", 5, color_view(view, view.samples, view.at, face, live=False,
                  gaps=view.batch.get("source_gaps", []))))
    if view.depth:
        scenes.append(("彩色图像与对齐深度", 5, depth_view(view, face)))
    if view.clouds:
        scenes.append(("MID-360 点云回执", 6, clouds(view, face)))
    scenes.append(("任务终态与原始事件", 5, finish(view, face)))
    with map_output(destination) as out:
        target = out / "task.mp4"
        chapters, cursor = [], 0
        args = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24",
                "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-preset", "veryfast",
                "-crf", "19", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)]
        with subprocess.Popen(args, stdin=subprocess.PIPE) as proc:
            try:
                for name, duration, image in scenes:
                    chapters.append({"title": name, "sec": cursor / FPS})
                    count = max(1, math.ceil(duration * FPS))
                    if image is None:
                        count += 1
                    if image is not None:
                        image.save(out / f"chapter-{len(chapters)}.png")
                        frame = image.tobytes()
                    previous = None
                    for i in range(count):
                        if image is None:
                            stamp = view.times[0] + i * NANO // FPS
                            index = max(0, bisect_right(view.times, stamp) - 1)
                            if index != previous:
                                when, samples, gaps = view.window[index]
                                scene = live_scene(view, samples, when, gaps, face)
                                if i == 0:
                                    scene.save(out / f"chapter-{len(chapters)}.png")
                                frame = scene.tobytes()
                                previous = index
                        proc.stdin.write(frame)
                    cursor += count
            finally:
                proc.stdin.close()
            require(proc.wait() == 0, "video encoding failed")
        probe = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
            "-show_entries", "stream=width,height,codec_name,nb_read_frames,duration", "-of", "json", str(target)]))
        stream, = probe["streams"]
        require((int(stream["nb_read_frames"]), stream["width"], stream["height"], stream["codec_name"]) ==
                (cursor, W, H, "h264"), "video decode mismatch")
        links = []
        assets = out / "samples"
        assets.mkdir()
        for name, path in view.inputs.items():
            shutil.copyfile(path, out / name)
        for s in view.samples:
            row = {"source_id": s["source_id"], "frame_id": s["frame_id"], "stamp_ns": str(s["stamp_ns"]),
                   "age_ms": age(s, view.at)}
            if s["kind"] == "artifact":
                path = view.artifact(s["artifact_ref"], s["media_type"])
                shutil.copyfile(path, assets / path.name)
                row.update(artifact_ref=s["artifact_ref"], file="samples/" + path.name)
            else:
                row["position_m"] = s["position_m"]
            links.append(row)
        captured = {}
        for _, batch, _ in view.window:
            for s in batch:
                captured.setdefault(s["source_id"], set()).add(ns(s["stamp_ns"]))
        window_sources = {source: {"sample_stamps": len(stamps), "first_ns": str(min(stamps)), "last_ns": str(max(stamps))}
                          for source, stamps in captured.items()}
        summary = {"task_id": view.task["id"], "operation_id": view.op, "tool": view.tool, "request": view.task["text"],
                   "task_elapsed_ms": view.elapsed, "batch_as_of_ns": str(view.at), "samples": links,
                   "source_gaps": view.batch.get("source_gaps", []), "window_batches": len(view.window),
                   "window_sources": window_sources, "window_s": (view.times[-1]-view.times[0])/NANO if view.times else 0,
                   "unique_color_frames": len({(s["stamp_ns"], s["artifact_ref"]) for _, samples, _ in view.window
                        for s in samples if s["source_id"] == view.rgb["source_id"]}),
                   "unique_color_images": len({s["artifact_ref"] for _, samples, _ in view.window for s in samples
                                               if s["source_id"] == view.rgb["source_id"]}),
                   "frames": cursor, "fps": FPS, "width": W, "height": H, "duration_s": float(stream["duration"]), "chapters": chapters,
                   "task_sha256": view.task_sha, "timeline_sha256": view.record.timeline_digest,
                   "window_sha256": view.stream_sha, "video_sha256": sha(target)}
        (out / "video.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", "utf8")
        viewer(out, view, summary)
    return summary


def viewer(out, view, summary):
    esc = html.escape
    sections = "".join(f'<button data-time="{c["sec"]}"><small>{int(c["sec"])//60:02d}:{int(c["sec"])%60:02d}</small>{esc(c["title"])}</button>'
                       for c in summary["chapters"])
    rows = "".join(f'<tr><td>{esc(s["source_id"])}</td><td>{esc(s["frame_id"])}</td><td>{s["age_ms"]:.1f} ms</td><td>' +
                   (f'<a href="{esc(s["file"])}">原始工件</a>' if "file" in s else esc(str(s.get("position_m")))) +
                   '</td></tr>' for s in summary["samples"])
    cover = 3 if view.window else 2
    page = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>边缘端传感器实测</title><style>*{{box-sizing:border-box}}body{{background:#101a21;color:#f4f2e8;font:17px/1.55 "Microsoft YaHei",sans-serif;margin:0}}main{{max-width:1450px;margin:auto;padding:22px clamp(12px,3vw,45px)}}h1{{font-size:clamp(25px,3vw,40px);margin:6px 0}}p,small{{color:#b3c2c9}}section{{display:grid;grid-template-columns:minmax(0,3fr) minmax(220px,1fr);gap:18px}}video{{width:100%;background:#0c151a;border-radius:12px}}aside{{background:#192833;border-radius:12px;padding:16px}}button{{font:inherit;background:none;color:#f4f2e8;border:0;border-top:1px solid #344756;padding:12px 0;text-align:left;width:100%;cursor:pointer}}small{{margin-right:12px;color:#84e2ba}}a{{color:#84e2ba}}table{{border-collapse:collapse}}td,th{{text-align:left;padding:8px 19px 8px 0;border-bottom:1px solid #344756}}details{{margin-top:18px;overflow:auto}}summary{{cursor:pointer}}@media(max-width:820px){{section{{display:block}}aside{{margin-top:10px}}}}</style>
<main><h1>边缘端传感器实测</h1><p>“{esc(view.task["text"])}”　·　{esc(view.task["id"])}</p><section>
<video controls playsinline preload="metadata" src="task.mp4" poster="chapter-{cover}.png"></video><aside><strong>查看任务过程</strong>{sections}</aside></section>
<p>采集窗口按设备接收时间原速播放。任务原文、取样回执和终态停留展示，事件时间使用北京时间。</p>
<details open><summary>传感器回执</summary><table><tr><th>来源</th><th>坐标系</th><th>获取时帧龄</th><th>原始数据</th></tr>{rows}</table></details>
<p><a href="task.mp4">打开 MP4</a>　<a href="task.json">原始任务回执</a>　<a href="timeline.jsonl">录制事件</a>　<a href="video.json">采样时间与完整性校验</a></p></main>
<script>const video=document.querySelector('video');for(const b of document.querySelectorAll('button'))b.onclick=()=>{{video.currentTime=+b.dataset.time;video.play()}};</script></html>'''
    (out / "index.html").write_text(page, encoding="utf8")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    for name in ("recording", "task", "artifacts", "out"):
        cli.add_argument("--" + name, type=Path, required=True)
    cli.add_argument("--stream", type=Path, help="JSONL of surrounding gateway batches with received_ns and samples")
    cli.add_argument("--font", required=True)
    cli.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    args = cli.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        cli.error("FFmpeg and FFprobe are required")
    view = Review(args.task, args.recording, args.artifacts, args.stream)
    report = render(view, args.out, args.font, args.ffmpeg, args.ffprobe)
    print(json.dumps({k: report[k] for k in ("task_id", "frames", "duration_s", "window_batches", "unique_color_frames")}))


if __name__ == "__main__":
    main()
