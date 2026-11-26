"""Replay a verified capability release and its original RGB sensor window at wall-clock speed."""
import argparse
from bisect import bisect_right
from calendar import timegm
from datetime import datetime, timezone
from functools import lru_cache
import json
from pathlib import Path
import shutil

from PIL import Image, ImageDraw

from .map_view import map_output
from .review_media import write_video
from .sensor_review import FPS, GOLD, GREEN, GRID, INK, MID, NANO, base, ns, panel, require, sha, text
from .target_review import Evolution, Target, annotate, artifact, paste


def utc_ns(value):
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(instant.utcoffset() is not None, "event has no UTC offset")
    instant = instant.astimezone(timezone.utc)
    return timegm(instant.utctimetuple()) * NANO + instant.microsecond * 1000


def utc_clock(stamp):
    return datetime.fromtimestamp(stamp // NANO, timezone.utc).strftime("%H:%M:%S") + f".{stamp % NANO // 1_000_000:03d}"


def milestones(view, evolution):
    by_phase = {r["phase"]: r for r in evolution.events}
    require(len(by_phase) == len(evolution.events) and evolution.qualification, "missing or ambiguous release evidence")
    gap, = (r for r in evolution.record.by["agent_trace"] if r["phase"] == "blocked")
    require(gap["source_event_id"] == by_phase["gap_observed"]["source_event"], "supervisor observed a different gap")
    qualification = evolution.qualification
    require(qualification["candidate_digest"] == by_phase["release_applied"]["candidate_digest"],
            "qualification belongs to another candidate")
    checks = len(qualification["checks"])
    rows = [
        ("user_request", "收到用户请求", ns(evolution.source_request["stamp_ns"])),
        ("capability_gap", "缺少定位工具", ns(gap["stamp_ns"])),
        ("candidate_generated", "生成只读候选工具", utc_ns(by_phase["candidate_generated"]["utc"])),
        ("candidate_verified", f"独立验收通过 · {checks} 项", utc_ns(by_phase["candidate_verified"]["utc"])),
        ("release_applied", "发布工具", utc_ns(by_phase["release_applied"]["utc"])),
        ("continuation_accepted", "Agent 接收续办任务", ns(view.events[0]["stamp_ns"])),
        ("grounding_response", "返回目标定位", ns(view.events[2]["stamp_ns"])),
        ("continuation_terminal", "续办成功", utc_ns(by_phase["continuation_terminal"]["utc"])),
    ]
    require(all(a[2] <= b[2] for a, b in zip(rows, rows[1:])), "parent and continuation timestamps are unordered")
    return rows


class ColorWindow:
    def __init__(self, stream, root, view, *, max_seconds=90):
        self.root = root
        self.times = []
        self.frames = []
        self.outages = []
        self.start = self.end = None
        refs = {}
        with stream.open(encoding="utf8") as file:
            for line in file:
                row = json.loads(line)
                received = ns(row["received_ns"])
                require(self.end is None or self.end <= received, "window receive clock regressed")
                self.start = received if self.start is None else self.start
                self.end = received
                matching = [s for s in row["samples"] if s["source_id"] == view.g["rgb_source_id"]]
                if not matching:
                    self.outages.append((received, True))
                    continue
                self.outages.append((received, False))
                require(row["basis"] == "published" and row["actor_ref"] == view.actor and
                        row["clock_id"] == "unix" and len(matching) == 1, "unrelated batch or ambiguous color source")
                sample = matching[0]
                stamp, ref = ns(sample["stamp_ns"]), sample["artifact_ref"]
                require(sample["kind"] == "artifact" and sample["media_type"] == "image/png"
                        and sample["actor_ref"] == view.actor and sample["clock_id"] == "unix"
                        and sample["frame_id"] == view.g["frame_id"] and sample["calibration_ref"] == view.g["calibration_ref"]
                        and stamp <= received, "unrelated image or future ROS stamp")
                if stamp in refs:
                    require(refs[stamp] == ref, "conflicting image at the same ROS stamp")
                    continue
                require(not self.frames or stamp > self.frames[-1][0], "camera source clock regressed")
                path = artifact(root, ref, ".png")
                with Image.open(path) as src:
                    require(src.format == "PNG" and src.mode == "RGB" and src.size == view.rgb.size,
                            "source changed image layout")
                refs[stamp] = ref
                self.times.append(received)
                self.frames.append((stamp, ref))
        require(len(self.frames) >= 2 and 0 < self.end - self.start <= max_seconds*NANO, "invalid RGB capture window")
        self.row_times = [stamp for stamp, _ in self.outages]

    def at(self, now):
        idx = bisect_right(self.times, now) - 1
        row = bisect_right(self.row_times, now) - 1
        if idx < 0:
            return None, True
        stamp, ref = self.frames[idx]
        missing = self.outages[row][1] or now - stamp > 500_000_000
        return (stamp, ref, self.times[idx]), missing

    @lru_cache(maxsize=3)
    def image(self, ref):
        with Image.open(self.root / (ref[7:] + ".png")) as image:
            return image.copy()


def render(source, destination, face, ffmpeg, ffprobe):
    view = Target(source/"task.json", source/"recording", source/"artifacts")
    evolution = Evolution(source/"evolution", view)
    stream = source/"sensor-timeline.jsonl"
    window = ColorWindow(stream, source/"artifacts", view)
    events = milestones(view, evolution)
    require(window.start <= events[0][2] <= events[-1][2] <= window.end, "RGB window does not cover this request and release")
    # The freeze is rendered only from Target's independently checked operation artifacts.
    still = annotate(view, view.rgb)
    shell = base("从自然语言请求到现场定位", evolution.gap["text"], "原时速回放  ·  Jetson UTC",
                 evolution.gap["id"], face)
    d = ImageDraw.Draw(shell)
    panel(d, (48,192,822,715))
    panel(d, (840,192,1392,715))
    text(d,68,207,"D435 彩色画面",22,INK,face)
    text(d,865,207,"Agent 任务事件",22,INK,face)
    d.line((862,494,1369,494), fill=GRID)
    frame_log = []
    dt = NANO // FPS
    frame_count = (window.end - window.start) // dt + 1

    def scenes():
        for index in range(frame_count):
            now = window.start + index * dt
            im = shell.copy()
            draw = ImageDraw.Draw(im)
            text(draw,407,209,utc_clock(now)+"  UTC",21,GREEN,face,390)
            sample, missing = window.at(now)
            record = {"index": index, "playhead_ns": str(now), "source_gap": missing}
            if sample:
                stamp, ref, received = sample
                paste(im,window.image(ref),(60,243,810,646))
                caption = "缺样，保留最近帧" if missing else "最近采集帧"
                text(draw,70,665,f"{caption}   {utc_clock(stamp)} UTC     帧龄 {(now-stamp)/1e6:.0f} ms",
                     20,GOLD if missing else MID,face,724)
                record.update(rgb_ref=ref, rgb_stamp_ns=str(stamp), first_received_ns=str(received))
            else:
                text(draw,181,425,"此时尚未收到彩色帧",25,MID,face)
            for i,(kind, label, when) in enumerate(events):
                y = 249 + i*30
                passed = now >= when
                draw.ellipse((867,y+4,877,y+14),fill=GREEN if passed else GRID)
                text(draw,892,y,label,19,INK if passed else MID,face,283)
                text(draw,1194,y,utc_clock(when) if passed else "等待",18,GREEN if passed else MID,face,166)
            if now >= ns(view.events[2]["stamp_ns"]):
                text(draw,866,509,"定位回执 · 原始帧定格",19,GREEN,face)
                paste(im,still,(866,539,1096,699))
                text(draw,1110,548,"实测光学坐标（米）",18,INK,face,257)
                for j,(axis,value) in enumerate(zip(("X","Y","Z"),view.xyz)):
                    text(draw,1113,580+25*j,f"{axis}  {value:+.4f}",18,INK,face,245)
                text(draw,1110,665,utc_clock(ns(view.g["rgb_stamp_ns"]))+" UTC",16,GOLD,face,258)
                record["annotated_inset_ref"] = view.g["rgb_ref"]
            else:
                text(draw,867,561,"等待工具返回定位回执",22,MID,face,495)
            frame_log.append(record)
            yield "现场原时速", 1/FPS, im

    with map_output(destination) as out:
        _, count = write_video(out/"task.mp4", scenes(), ffmpeg, ffprobe)
        require(count == frame_count, "frame count mismatch")
        with (out/"frames.jsonl").open("w",encoding="utf8") as file:
            for f in frame_log:
                file.write(json.dumps(f, separators=(",",":"))+"\n")
        report = {"frame_count": count, "source_frames": len(window.frames), "fps": FPS, "duration_s": count/FPS,
                  "first_unix_ns": str(window.start), "last_unix_ns": str(window.end), "playback_rate": 1,
                  "clock_id": "unix", "parent_task": evolution.gap["id"], "continuation_task": view.task["id"],
                  "operation_id": view.op, "target_rgb_ref": view.g["rgb_ref"], "task_sha256": view.task_sha,
                  "input_window_sha256": sha(stream), "parent_task_sha256": sha(evolution.inputs["gap-task.json"]),
                  "release_events_sha256": sha(evolution.inputs["events.jsonl"]), "frames_sha256": sha(out/"frames.jsonl"),
                  "video_sha256": sha(out/"task.mp4"),
                  "events": [{"phase": k, "stamp_ns": str(t)} for k, _, t in events]}
        (out/"video.json").write_text(json.dumps(report,ensure_ascii=False,indent=2)+"\n", encoding="utf8")
    return report


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--source", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--font", required=True)
    cli.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    args = cli.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        cli.error("FFmpeg and FFprobe are required")
    print(json.dumps(render(args.source, args.out, args.font, args.ffmpeg, args.ffprobe),ensure_ascii=False))
