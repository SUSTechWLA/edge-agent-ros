"""Render one actually submitted user task from its client, Agent and RGB-D records."""

import argparse
from bisect import bisect_right
from html import escape
import json
import math
from pathlib import Path
import shutil

from PIL import Image, ImageDraw

from .evolution_live import ColorWindow
from .map_view import map_output
from .review_media import write_video
from .sensor_review import FPS, H, W, clock, depth_colors, ns, pixels, read, require, sha, text
from .target_review import Target, annotate, cloud_viewer, measured_cloud, paste


PAPER, TYPE, MUTED = "#f5f4ee", "#142827", "#536861"
GREEN, GOLD, LINE = "#006b54", "#f6c454", "#c0cdc4"
PHOTO_BOX = (28, 116, 848, 731)
PANEL_X = 882
PHASES = ("提交自然语言任务", "派发只读工具", "读取彩色与深度", "标出目标测点", "返回测量结果")
HOLDS = (4, 3, 4, 4, 6)


def source_stages(view, submission):
    require(
        view.task["trigger"]["kind"] == "user" and view.events[0]["kind"] == "agent_request",
        "expected the directly submitted user's original recording",
    )
    require(
        submission["task_id"] == view.task["id"]
        and submission["request_text"] == view.task["text"]
        and submission["http_status"] in (200, 201, 202),
        "client receipt differs from the original Agent request",
    )
    sent, accepted = ns(submission["sent_ns"]), ns(view.events[0]["stamp_ns"])
    require(
        sent <= accepted <= ns(submission["acknowledged_ns"]), "Agent creation outside the client HTTP exchange"
    )
    trace = view.record.by["agent_trace"]
    (planned,) = (r for r in trace if r.get("phase") == "planned")
    (dispatched,) = (r for r in trace if r.get("phase") == "dispatch" and r.get("operation_id") == view.op)
    require(dispatched.get("tool") == view.tool, "dispatch belongs to another tool")
    stages = [
        sent,
        ns(view.events[1]["stamp_ns"]),
        ns(view.obs["as_of_ns"]),
        ns(view.events[2]["stamp_ns"]),
        ns(view.events[3]["stamp_ns"]),
    ]
    require(
        sent <= accepted <= ns(planned["stamp_ns"]) <= ns(dispatched["stamp_ns"]) <= stages[1]
        and stages == sorted(stages),
        "source events are not ordered on the edge clock",
    )
    return stages


def verified_range(view):
    radius = math.dist((0.0, 0.0, 0.0), view.xyz)
    require(
        0 < radius
        and abs(view.g["range_m"] - radius) <= 0.001
        and abs(view.g["axial_depth_m"] - view.xyz[2]) <= 0.001,
        "reported axial or straight-line distance mismatch",
    )
    return radius


def client_exchange(source, task):
    submission = read(source / "submit.json")
    require(submission["method"] == "POST" and submission["path"] == "/agent/tasks", "unexpected task endpoint")
    for name, key in (
        ("submit-body.json", "request_body_sha256"),
        ("submit-response.json", "response_body_sha256"),
    ):
        path = source / name
        require(sha(path) == submission[key], "HTTP exchange differs from the client receipt")
        body = read(path)
        require(body["id"] == task["id"] and body["text"] == task["text"], "HTTP envelope is for another task")
    return submission


def target_image(view):
    marked = annotate(view, view.rgb)
    detection = view.g.get("detection")
    if detection:
        require(detection.get("rgb_ref") == view.g["rgb_ref"], "detector used a different RGB frame")
        left, top, right, bottom = detection["bbox_xyxy"]
        u, v = view.pt
        require(
            0 <= left <= u < right <= marked.width and 0 <= top <= v < bottom <= marked.height,
            "measured pixel outside the detected instance",
        )
        draw = ImageDraw.Draw(marked)
        for u0 in range(math.ceil(left), math.ceil(right), 12):
            for y in (top, bottom - 1):
                draw.line((u0, y, min(u0 + 6, right - 1), y), fill="#83c8ff", width=2)
        for v0 in range(math.ceil(top), math.ceil(bottom), 12):
            for x in (left, right - 1):
                draw.line((x, v0, x, min(v0 + 6, bottom - 1)), fill="#83c8ff", width=2)
    return marked


def depth_preview(view):
    values = sorted(v for v in pixels(view.depth) if 0 < v < 65535)
    require(values, "depth input contains no valid pixel")
    low, high = values[len(values) // 50], values[min(len(values) - 1, len(values) * 49 // 50)]
    transform, palette = depth_colors(low, max(low + 1, high))
    indexed = Image.frombytes("P", view.depth.size, bytes(transform[v] for v in pixels(view.depth)))
    indexed.putpalette(palette)
    return indexed.convert("RGB")


def caption(draw, x, y, title, face, size=21, color=MUTED, width=505):
    text(draw, x, y, str(title), size, color, face, width)


def metric(draw, y, title, value, face):
    caption(draw, PANEL_X, y, title, face)
    caption(draw, PANEL_X + 206, y - 2, value, face, 24, TYPE, 325)


def draw_cloud(view, radius, face):
    """Project measured samples in metres into an oblique view, including the physical ray."""
    im = Image.new("RGB", (820, 615), "#19302d")
    draw = ImageDraw.Draw(im)
    stride, points = measured_cloud(view)
    points = [(p, target) for p, target in points if math.dist((0, 0, 0), p) <= 1.35 * radius]
    # Orthogonal basis. Positive optical Y is down, positive optical Z projects right.
    yaw, pitch = math.radians(40), math.radians(19)
    cy, sy, cp, sp = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch)

    def project(p):
        x, y, z = p
        return (cy * x + sy * z, sp * (sy * x - cy * z) + cp * y, cp * (-sy * x + cy * z) + sp * y)

    projected = [(project(p), selected) for p, selected in points]
    measured = project(view.xyz)
    extent = [p for p, _ in projected] + [(0.0, 0.0, 0.0), measured]
    xs, ys, _ = zip(*extent)
    x0, x1, y0, y1 = min(xs), max(xs), min(ys), max(ys)
    scale = min(670 / max(0.05, x1 - x0), 386 / max(0.05, y1 - y0))

    def at(p):
        return round(410 + scale * (p[0] - (x0 + x1) / 2)), round(303 + scale * (p[1] - (y0 + y1) / 2))

    for p, selected in sorted(projected, key=lambda row: -row[0][2]):
        x, y = at(p)
        r = 2 if selected else 1
        draw.ellipse((x - r, y - r, x + r, y + r), fill="#8de4b0" if selected else "#7b9890")
    ax, ay = at((0, 0, 0))
    bx, by = at(measured)
    draw.line((ax, ay, bx, by), fill=GOLD, width=3)
    for x, y, label in ((ax, ay, "相机光心"), (bx, by, "目标测点")):
        draw.ellipse((x - 6, y - 6, x + 6, y + 6), fill=GOLD, outline="#182723", width=2)
        tx = min(max(x + 8, 4), 696)
        ty = min(max(y - 22, 70), 500)
        draw.rounded_rectangle((tx, ty, tx + 115, ty + 31), 5, fill="#162724")
        caption(draw, tx + 5, ty + 4, label, face, 19, "#f2f7ed", 108)
    caption(draw, 22, 21, "实测深度点云", face, 25, "#f3f7ed", 330)
    caption(draw, 22, 549, "金线从相机光心连到选中的目标表面。", face, 20, "#edf2e8", 765)
    caption(
        draw, 22, 580, f"按 {stride} 像素间隔取样，显示测点距离 1.35 倍以内的有效点。", face, 16, "#b4c7be", 765
    )
    return im


def frame(view, face, stage, tick, stages, photo, photo_stamp, preview, marked, radius, submission):
    image = Image.new("RGB", (W, H), PAPER)
    draw = ImageDraw.Draw(image)
    caption(draw, 29, 17, "EDGE AGENT   /   车载实测", face, 19, GREEN, 450)
    caption(draw, 1030, 17, "源事件回放  ·  分步停留", face, 18, MUTED, 345)
    caption(draw, 28, 55, view.task["text"], face, 39, TYPE, W - 56)
    draw.rectangle(PHOTO_BOX, fill="#172321")
    if photo:
        paste(image, photo, PHOTO_BOX)
    else:
        caption(draw, PHOTO_BOX[0] + 45, 378, "这一时刻缺少可核验的相机画面", face, 28, PAPER, 700)
    caption(draw, 30, 746, "D435  对齐深度" if stage == 4 else "D435  彩色相机", face, 19, GREEN, 214)
    photo_caption = "本帧无实时采样" if photo_stamp is None else clock(photo_stamp) + "  北京时间"
    caption(draw, 236, 746, photo_caption, face, 18, MUTED, 315)
    caption(
        draw,
        540,
        746,
        (
            "实拍 · 按采样时刻",
            "实拍 · 按采样时刻",
            "任务选中的原图",
            "任务原图 · 像素标注",
            "光学坐标 · 三维视图",
        )[stage],
        face,
        18,
        MUTED,
        295,
    )
    draw.line((862, 120, 862, 735), fill=LINE, width=1)
    caption(draw, PANEL_X, 132, PHASES[stage], face, 37, GREEN, 513)
    caption(draw, PANEL_X, 191, f"{clock(stages[stage])}   北京时间", face, 21, MUTED)
    if stage == 0:
        caption(draw, PANEL_X, 271, "客户端向车端发送上方原文。", face, 29, TYPE)
        caption(draw, PANEL_X, 337, "POST /agent/tasks", face, 27, GREEN)
        ack = f"HTTP {submission['http_status']}" if tick >= ns(submission["acknowledged_ns"]) else "已发出请求"
        metric(draw, 413, "提交状态", ack, face)
        metric(draw, 475, "任务来源", "用户直接提交", face)
    elif stage == 1:
        caption(draw, PANEL_X, 267, "Agent 调用现场工具", face, 30, TYPE)
        caption(draw, PANEL_X, 329, view.tool, face, 35, GREEN)
        step = view.task["steps"][0]
        require(step.get("soft_cap", {}).get("effect") == "read", "expected a read-only tool")
        metric(draw, 425, "工具权限", "只读", face)
        metric(draw, 482, "本次派发", "1 次", face)
    elif stage == 2:
        paste(image, preview, (PANEL_X, 257, PANEL_X + 294, 478))
        caption(draw, PANEL_X + 307, 282, "对齐深度", face, 22, GREEN, 187)
        caption(draw, PANEL_X + 307, 337, f"{view.rgb.width} × {view.rgb.height}", face, 22, TYPE, 189)
        caption(draw, PANEL_X + 307, 382, "近远伪彩", face, 18, MUTED, 187)
        delta = abs(ns(view.g["rgb_stamp_ns"]) - ns(view.g["depth_stamp_ns"])) / 1e6
        metric(draw, 510, "彩色与深度时差", f"{delta:.1f} ms", face)
        age = (stages[0] - ns(view.g["rgb_stamp_ns"])) / 1e6
        note = "读取同时间戳的最新帧" if delta == 0 else "读取已配对的最新帧"
        caption(draw, PANEL_X, 556, note, face, 19, GREEN)
        caption(
            draw,
            PANEL_X,
            587,
            f"彩色采样比任务下发{'早' if age >= 0 else '晚'} {abs(age):.1f} ms",
            face,
            17,
            MUTED,
        )
    elif stage == 3:
        u, v = view.pt
        detail = min(55, max(20, view.rgb.width // 12))
        x = max(0, min(view.rgb.width - 2 * detail, u - detail))
        y = max(0, min(view.rgb.height - 2 * detail, v - detail))
        zoom = marked.crop((x, y, x + 2 * detail, y + 2 * detail))
        paste(image, zoom, (PANEL_X, 258, PANEL_X + 238, 496))
        caption(draw, PANEL_X + 256, 291, "测点放大", face, 25, GREEN, 246)
        caption(draw, PANEL_X + 256, 354, f"({u}, {v}) px", face, 28, TYPE, 246)
        caption(draw, PANEL_X + 256, 415, "金色十字为测点", face, 18, MUTED, 246)
        metric(draw, 525, "该点光轴深度", f"{view.xyz[2]:.3f} m", face)
        detection = view.g.get("detection")
        if detection:
            name = {"potted plant": "盆栽", "chair": "椅子"}.get(detection["label"], detection["label"])
            metric(draw, 570, "蓝虚线检测框", f"{name}  ·  {detection['score']:.2f}", face)
    else:
        caption(draw, PANEL_X, 252, "相机光心到测点的直线距离", face, 24, GREEN)
        caption(draw, PANEL_X, 302, f"{radius:.2f} 米", face, 72, TYPE)
        for index, (label, value) in enumerate(zip(("X  向右", "Y  向下", "Z  向前"), view.xyz)):
            metric(draw, 412 + index * 45, label, f"{value:+.3f} m", face)
    start = PANEL_X
    for i, label in enumerate(("提交", "选工具", "读传感器", "标注", "完成")):
        x = start + i * 102
        draw.rectangle((x, 626, x + 88, 631), fill=GREEN if i <= stage else LINE)
        caption(draw, x, 648, label, face, 18, GREEN if i <= stage else MUTED, 90)
    caption(draw, PANEL_X, 701, f"设备时间    +{(tick - stages[0]) / 1e6:.0f} ms", face, 19, MUTED)
    draw.line((28, 782, W - 28, 782), fill=LINE, width=1)
    return image


def render(source, destination, face, ffmpeg, ffprobe):
    view = Target(source / "task.json", source / "recording", source / "artifacts")
    submission = client_exchange(source, view.task)
    stages = source_stages(view, submission)
    radius = verified_range(view)
    trace = source / "sensor-timeline.jsonl"
    window = ColorWindow(trace, source / "artifacts", view)
    # An independently archived receipt may contain a frame captured before
    # the POST that the observer's next poll only delivered after the POST.
    # Preview source time by sensor stamps, including that verified frame.
    rgb_frames = dict(window.frames)
    receipt_stamp = ns(view.g["rgb_stamp_ns"])
    require(rgb_frames.get(receipt_stamp, view.g["rgb_ref"]) == view.g["rgb_ref"], "conflicting receipt frame")
    rgb_frames[receipt_stamp] = view.g["rgb_ref"]
    rgb_stamps = sorted(rgb_frames)
    used = []
    require(window.start <= stages[0] <= stages[-1] <= window.end, "live RGB recording misses the execution")
    marked = target_image(view)
    cloud = draw_cloud(view, radius, face)
    preview = depth_preview(view)
    chapters = []
    snapshots = []
    with map_output(destination) as out:

        def scenes():
            for phase, seconds in enumerate(HOLDS):
                chapters.append(
                    {"title": PHASES[phase], "sec": sum(HOLDS[:phase]), "source_ns": str(stages[phase])}
                )
                for idx in range(seconds * FPS):
                    end = stages[min(phase + 1, len(stages) - 1)]
                    tick = stages[phase] + (end - stages[phase]) * idx // (seconds * FPS)
                    if phase < 2:
                        pos = bisect_right(rgb_stamps, tick) - 1
                        stamp = rgb_stamps[pos] if pos >= 0 and tick - rgb_stamps[pos] <= 500_000_000 else None
                        ref = rgb_frames[stamp] if stamp is not None else None
                        photo = window.image(ref) if ref else None
                        if not used or used[-1]["rgb_ref"] != ref:
                            used.append(
                                {
                                    "first_video_sec": sum(HOLDS[:phase]) + idx / FPS,
                                    "source_ns": str(stamp) if stamp is not None else None,
                                    "rgb_ref": ref,
                                }
                            )
                    else:
                        stamp = ns(view.g["depth_stamp_ns"] if phase == 4 else view.g["rgb_stamp_ns"])
                        photo = (view.rgb, marked, cloud)[phase - 2]
                    pic = frame(
                        view, face, phase, tick, stages, photo, stamp, preview, marked, radius, submission
                    )
                    if idx == 0:
                        filename = f"stage-{phase + 1}.png"
                        pic.save(out / filename)
                        snapshots.append(filename)
                    yield PHASES[phase], 1 / FPS, pic

        _, count = write_video(out / "task.mp4", scenes(), ffmpeg, ffprobe)
        marked.save(out / "annotated-rgb.png")
        cloud_viewer(view, out / "cloud.html")
        report = {
            "task_id": view.task["id"],
            "operation_id": view.op,
            "tool": view.tool,
            "direct_user_request": view.task["text"],
            "frames": count,
            "duration_s": count / FPS,
            "source_elapsed_ms": (stages[-1] - stages[0]) / 1e6,
            "timing": "guided replay of original source events",
            "chapters": chapters,
            "snapshots": snapshots,
            "pixel": view.pt,
            "bbox_xyxy": view.bbox,
            "detection": view.g.get("detection"),
            "distance_m": radius,
            "optical_xyz_m": view.xyz,
            "rgb_ref": view.g["rgb_ref"],
            "depth_ref": view.g["depth_ref"],
            "calibration_ref": view.g["calibration_ref"],
            "task_sha256": view.task_sha,
            "submit_sha256": sha(source / "submit.json"),
            "request_body_sha256": submission["request_body_sha256"],
            "response_body_sha256": submission["response_body_sha256"],
            "rgb_stamp_ns": view.g["rgb_stamp_ns"],
            "depth_stamp_ns": view.g["depth_stamp_ns"],
            "sample_age_at_submit_ms": (stages[0] - ns(view.g["rgb_stamp_ns"])) / 1e6,
            "camera_previews_by_source_clock": used,
            "timeline_sha256": view.record.timeline_digest,
            "window_sha256": sha(trace),
            "video_sha256": sha(out / "task.mp4"),
        }
        (out / "video.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8"
        )
        viewer = f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>车端任务录像</title>
<style>body{{max-width:1440px;margin:auto;padding:24px;background:#f5f4ee;color:#142827;
font:18px/1.5 "Microsoft YaHei",sans-serif}}h1{{font-size:clamp(22px,3vw,38px)}}video{{display:block;width:100%}}
nav{{display:flex;gap:1.5em;flex-wrap:wrap;margin:18px 0}}a{{color:#006b54}}</style>
<h1>{escape(view.task["text"])}</h1><video controls playsinline preload="metadata" poster="stage-1.png"
src="task.mp4"></video><nav><a href="task.mp4">打开视频</a><a href="annotated-rgb.png">查看标注原图</a>
<a href="cloud.html">旋转查看实测点云</a><a href="video.json">查看任务与时间记录</a></nav></html>"""
        (out / "index.html").write_text(viewer, encoding="utf8")
    return report


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--source", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--font", required=True)
    cli.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    args = cli.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        cli.error("FFmpeg and FFprobe are required")
    print(json.dumps(render(args.source, args.out, args.font, args.ffmpeg, args.ffprobe), ensure_ascii=False))


if __name__ == "__main__":
    main()
