"""Replay a model-planned read-only mission from the submitted request, journal and sensor bytes."""

import argparse
from dataclasses import dataclass
from hashlib import sha256
from html import escape
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace

from PIL import Image, ImageDraw

from edge_contracts.trace_content import encoded
from .agent_trace import utc_ns
from .direct_target_demo import client_exchange, depth_preview
from .evolution_live import ColorWindow
from .fusion_review import Fusion, SOURCE_COLORS, draw_map, source_name
from .map_view import map_output
from .operator_evidence import normalized, qualified_receipt, trace_operator
from .render import font, paragraph
from .review_media import write_video
from .sensor_review import FPS, H, W, TaskReceipt, clock, ns, pixels, read, require, sha, text
from .spatial_capture import MAX_WINDOW_SECONDS
from .target_review import artifact, paste


PAPER, INK, TEAL, MUTED = "#f4f3ed", "#102c2a", "#006852", "#4f6861"
DARK, GOLD, RULE = "#192f2c", "#f8c958", "#becbc2"


def query_geometry(program):
    # Independent reader of the published expression. The recorder never imports or executes a plugin.
    number = r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)"
    comma = r"\s*,\s*"
    fraction = "(" + number + ")"
    crop = r"crop\(\s*" + comma.join([fraction]*4) + r"\s*\)"
    grid = r"grid\(\s*([1-8])" + comma + r"([1-8])\s*\)"
    statistic = r"(mean|median|min|max|coverage|quantile\(\s*" + fraction + r"\s*\))"
    separator = r"\s*\|\s*"
    expression = (r"\s*(depth|brightness|red|green|blue)" + separator + "(?:" + crop + separator + ")?"
                  + "(?:" + grid + separator + ")?" + statistic + r"\s*")
    require(isinstance(program, str) and program.isascii() and 0 < len(program) <= 128,
            "invalid recorded frame query")
    match = re.fullmatch(expression, program)
    require(match is not None, "invalid recorded frame query")
    channel, *coordinates, rows, cols, stat, q = match.groups()
    bounds = tuple(map(float, coordinates)) if coordinates[0] else (0., 0., 1., 1.)
    rows, cols = (int(rows), int(cols)) if rows else (1, 1)
    quantile = float(q) if q is not None else None
    require(0 <= bounds[0] < bounds[2] <= 1 and 0 <= bounds[1] < bounds[3] <= 1 and rows*cols <= 32
            and (quantile is None or 0 <= quantile <= 1), "query outside supported frame geometry")
    return channel, bounds, rows, cols, stat, quantile


def region_value(values, statistic, quantile, count, scale):
    if statistic == "coverage":
        return 100 * len(values) / count
    if not values:
        return None
    if statistic == "mean":
        return sum(values) / len(values) * scale
    ordered = sorted(values)
    q = {"min":0, "median":.5, "max":1}.get(statistic, quantile)
    point = (len(ordered) - 1) * q
    lo, hi = math.floor(point), math.ceil(point)
    return (ordered[lo] + (ordered[hi] - ordered[lo]) * (point - lo)) * scale


def contents(trace):
    item = trace["artifact"]
    require("content" in item and not item.get("truncated"), "incomplete model evidence")
    return item["content"]


class Journal:
    """Use the source payload only after binding it to the same recorded journal event."""

    def __init__(self, receipt):
        rows = {r["event_id"]: r for r in receipt.task["events"]}
        self.events = []
        for record in sorted(receipt.record.by["agent_trace"], key=lambda r: r["source_seq"]):
            row = rows.pop(record["source_event_id"])
            payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            trace = payload["trace"]
            require(row["task_id"] == receipt.task["id"] and row["seq"] == record["source_seq"]
                    and row["kind"] == record["phase"] and utc_ns(row["utc"]) == ns(record["stamp_ns"])
                    and row.get("operation_id") == record.get("operation_id")
                    and trace["event_id"] == row["event_id"] and trace["trace_id"] == receipt.task["trace_id"]
                    and trace.get("call_id") == record.get("call_id"), "journal/recorder event mismatch")
            self.events.append((record, payload))
        require(not rows, "recorder omitted source journal events")

    def one(self, phase, op=None):
        matches = [(r, p) for r, p in self.events if r["phase"] == phase and r.get("operation_id") == op]
        require(len(matches) == 1, "missing or ambiguous original event: " + phase)
        return matches[0]


def model_decision(journal, task):
    ctx, pctx = journal.one("planner.context")
    exchanges = [(r,p) for r,p in journal.events if r["phase"] == "model.request"
                 and p["trace"].get("module", "planner") == "planner"]
    require(1 <= len(exchanges) <= 2, "missing model request or unsupported number of attempts")
    req, preq = exchanges[-1]
    matching = [(r, p) for r, p in journal.events if r["phase"] == "model.response"
                and p["trace"].get("call_id") == preq["trace"].get("call_id")]
    require(len(matching) == 1, "model request has no unique response")
    res, pres = matching[0]
    chosen, pchosen = journal.one("planner.selected")
    frozen, pfrozen = journal.one("planner.frozen")
    pin, ppin = journal.one("planned")
    tctx, treq, tres = (item["trace"] for item in (pctx, preq, pres))
    require(treq.get("call_id") and treq["call_id"] == tres["call_id"] and
            treq["protocol"] == tres["protocol"] and treq["provider"] == tres["provider"],
            "model request and response do not match")
    require(tctx.get("authority") == "deployment.read_only", "read-only deployment authority is unproven")
    seq = [r["source_seq"] for r in (ctx, req, res, chosen, frozen, pin)]
    require(seq == sorted(seq),
            "planning events out of sequence")
    request, response = contents(treq), contents(tres)
    if treq["protocol"] == "ollama.chat":
        user, = [m for m in request["messages"] if m["role"] == "user"]
        context = json.loads(user["content"])
        answer = json.loads(response["message"]["content"])
        model = request["model"]
        require(response["model"] == model, "different responding model")
    elif treq["protocol"] == "planner.http.v1":
        context, answer, model = request, response, "HTTP planner"
    else:
        raise ValueError("unsupported recorded model protocol")
    require(context["request"] == task["text"] and context["authority"] == tctx["authority"],
            "model saw a different user request or authority")
    registered = {c["id"]: c for c in contents(tctx)}
    offered = context["capabilities"]
    require(offered and len(offered) == len({c["id"] for c in offered}), "missing or ambiguous model catalog")
    for cap in offered:
        original = registered.get(cap["id"], {})
        require(cap["effect"] == "read" and all(original.get(k) == v for k, v in cap.items()),
                "model saw an unregistered or non-read capability")
    ordered = [{"tool": s["tool"], "args": s["args"]} for s in task["steps"]]
    final = contents(pchosen["trace"])
    computation = None
    if answer["disposition"] == "read_compute":
        computation = trace_operator(journal, task, context, answer, final, contents)
    else:
        require(answer["disposition"] == "ready" and final == answer, "model output differs from execution")
    require(final["steps"] == ordered and len(ppin["steps"]) == len(ordered), "model output differs from execution")
    pins = [{k:s[k] for k in ("tool", "binding", "operation_id")} for s in task["steps"]]
    require(pfrozen["trace"]["steps"] == pins, "reservation differs from dispatched bindings")
    candidates = {c["id"] for c in offered}
    for step, saved in zip(task["steps"], ppin["steps"]):
        require(step["tool"] in candidates and all(step[k] == saved[k] for k in ("tool", "args", "binding", "version"))
                and saved["binding"] == registered[step["tool"]]["binding"], "frozen capability differs from proposal")
        cap, reservation = step.get("soft_cap", {}), step.get("soft_pin", {})
        require(not any(k in step for k in ("physical_pin", "physical_cap")) and cap.get("effect") == "read"
                and cap.get("id") == reservation.get("name") == step["tool"] and not cap.get("endpoint")
                and reservation.get("binding") == step["binding"]
                and reservation.get("reservation_id") == step["operation_id"], "unproven read-only DSH dispatch")
    revisions = []
    if len(exchanges) > 1:
        rev, prev = journal.one("planner.revision")
        initial, initial_payload = exchanges[0]
        replies = [(r, p) for r, p in journal.events if r["phase"] in ("model.response", "model.error")
                   and p["trace"].get("call_id") == initial_payload["trace"]["call_id"]]
        require(len(replies) == 1 and initial["source_seq"] < replies[0][0]["source_seq"] < rev["source_seq"]
                < req["source_seq"] and context.get("validation_feedback", {}).get("code") == prev["trace"]["code"],
                "missing recorded model revision")
        revisions.append(dict(initial_request_ns=ns(initial["stamp_ns"]), revised_ns=ns(rev["stamp_ns"]),
                              call_id=initial_payload["trace"]["call_id"], code=prev["trace"]["code"],
                              feedback=context["validation_feedback"]["message"]))
    return dict(model=model, protocol=treq["protocol"], authority=tctx["authority"], call_id=treq["call_id"],
                offered=offered, decisions=ordered, request_sha256=treq["artifact"]["sha256"],
                response_sha256=tres["artifact"]["sha256"], duration_ms=tres["duration_ms"],
                context_ns=ns(ctx["stamp_ns"]), request_ns=ns(req["stamp_ns"]),
                response_ns=ns(res["stamp_ns"]), frozen_ns=ns(frozen["stamp_ns"]), planned_ns=ns(pin["stamp_ns"]),
                revisions=revisions, computation=computation)


class QueryFrame:
    def __init__(self, receipt, directory, arguments):
        self.analysis, obs = receipt.batch["analysis"], receipt.batch["observations"]
        a = self.analysis
        self.actor = obs["actor_ref"]
        require(obs["basis"] == "published" and obs["clock_id"] == "unix" and
                ns(receipt.events[1]["stamp_ns"]) <= ns(obs["as_of_ns"]) <= ns(receipt.events[2]["stamp_ns"]),
                "measurement outside recorded call")
        require(a["program"] == frame_program(arguments), "returned analysis ran a different program")
        self.samples = {}
        for role in ("rgb", "depth", "calibration"):
            ref = a[role + "_ref"]
            matches = [s for s in obs["samples"] if s.get("artifact_ref") == ref and s["kind"] == "artifact"
                       and s["actor_ref"] == self.actor and s["frame_id"] == a["frame_id"] and s["clock_id"] == "unix"
                       and s["source_id"] == a[role + "_source_id"] and ns(s["stamp_ns"]) == ns(a[role + "_stamp_ns"])
                       and s["calibration_ref"] == a["calibration_ref"] and ns(s["stamp_ns"]) <= ns(obs["as_of_ns"])]
            require(len(matches) == 1, "analysis input does not match the operation's observations")
            self.samples[role] = matches[0]
            path = artifact(directory, ref, ".json" if role == "calibration" else ".png")
            if role == "calibration":
                calibration = read(path)
                require(calibration["frame_id"] == a["frame_id"] and
                        (calibration["width"], calibration["height"]) == self.rgb.size, "camera calibration mismatch")
            else:
                with Image.open(path) as im:
                    modes = ("RGB", "RGBA") if role == "rgb" else ("I", "I;16", "I;16B", "I;16L")
                    require(im.format == "PNG" and im.mode in modes, "unexpected source image format")
                    setattr(self, role, im.convert("RGB") if role == "rgb" else im.copy())
        self.g = {"rgb_source_id": self.samples["rgb"]["source_id"], "frame_id": a["frame_id"],
                  "calibration_ref": a["calibration_ref"]}
        w, h = self.rgb.size
        require((w, h) == self.depth.size == (a["width"], a["height"]), "raster geometry mismatch")
        require(a["actor_ref"] == self.actor and a["clock_id"] == "unix", "cross-actor or cross-clock query")
        self.verify_pixels()

    def verify_pixels(self):
        a = self.analysis
        channel, crop, rows, cols, statistic, quantile = query_geometry(a["program"])
        w, h = self.rgb.size
        left, top, right, bottom = (math.floor(w*crop[0]), math.floor(h*crop[1]),
                                    math.ceil(w*crop[2]), math.ceil(h*crop[3]))
        require(a["grid"] == [rows, cols] and len(a["regions"]) == rows * cols and
                (a["channel"], a["statistic"]) == (channel, statistic), "program does not match measured grid")
        unit = "%" if statistic == "coverage" else "m" if channel == "depth" else "0..255"
        require(a["unit"] == unit and 0 < a["depth_scale_m"] <= .1 and a["depth_scale_ref"], "invalid unit or scale")
        for i, region in enumerate(a["regions"]):
            r, c = divmod(i, cols)
            bounds = [left + c*(right-left)//cols, top + r*(bottom-top)//rows,
                      left + (c+1)*(right-left)//cols, top + (r+1)*(bottom-top)//rows]
            count = (bounds[2]-bounds[0]) * (bounds[3]-bounds[1])
            require(region["bounds_xyxy"] == bounds and region["pixels"] == count and count > 0,
                    "invalid region geometry or pixel count")
            if channel == "depth":
                values = [v for v in pixels(self.depth.crop(bounds)) if 0 < v < 65535]
            elif channel == "brightness":
                values = [(299*r + 587*g + 114*b)/1000 for r,g,b in pixels(self.rgb.crop(bounds))]
            else:
                values = list(pixels(self.rgb.crop(bounds).getchannel(channel[0].upper())))
            measured = region_value(values, statistic, quantile, count, a["depth_scale_m"] if channel == "depth" else 1)
            value = region["value"]
            require(region["valid_pixels"] == len(values) and (value is measured is None or
                    measured is not None and type(value) in (int, float) and math.isclose(measured, value, abs_tol=1e-9)),
                    "published measurement differs from archived pixels")
        values = [r["value"] for r in a["regions"]]
        numeric = [v for v in values if v is not None]
        require(numeric and a["minimum_indices"] == [i for i, v in enumerate(values) if v == min(numeric)] and
                a["maximum_indices"] == [i for i, v in enumerate(values) if v == max(numeric)], "invalid region extrema")

    def annotated(self, face):
        im = self.rgb.copy()
        d = ImageDraw.Draw(im)
        for number, row in enumerate(self.analysis["regions"]):
            left, top, right, bottom = row["bounds_xyxy"]
            f = font(face, max(10, min(24, (bottom - top) // 4, (right - left) // 7)))
            value = row["value"]
            label = str(number + 1) + "   " + display_value(value, self.analysis["unit"])
            marker = GOLD
            if number in self.analysis["minimum_indices"] and len(self.analysis["minimum_indices"]) < len(self.analysis["regions"]):
                label += "  最小"
                marker = "#9ff8c9"
            elif number in self.analysis["maximum_indices"] and len(self.analysis["maximum_indices"]) < len(self.analysis["regions"]):
                label += "  最大"
            d.rectangle((left, top, right - 1, bottom - 1), outline=marker, width=3)
            bbox = d.textbbox((left + 5, top + 5), label, font=f, anchor="lt")
            d.rectangle((left + 1, top + 1, min(right - 2, bbox[2] + 5), min(bottom - 2, bbox[3] + 5)), fill=DARK)
            d.text((left + 5, top + 5), label, font=f, fill=marker, anchor="lt")
        return im


@dataclass
class Chapter:
    title: str
    stamp: int
    seconds: int
    rows: list
    image: object = None
    image_label: str = ""
    source_task: str = ""


def blocked_attempt(path, current, submitted_ns):
    task = read(path)
    rows = task["events"]
    require(task["state"] == "BLOCKED" and task["steps"] == [] and not task["events_has_more"] and
            task["id"] != current["id"] and task["text"] == current["text"] and task["trigger"]["kind"] == "user",
            "earlier attempt must be a separate blocked submission of the identical wording")
    require(rows and rows[0]["kind"] == "accepted" and rows[-1]["kind"] == "blocked" and
            all(r["kind"] not in ("dispatch", "tool.request", "tool.response", "verified") for r in rows),
            "earlier task contains a dispatch or lacks its terminal event")
    events = {}
    for r in rows:
        payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
        require(r["task_id"] == task["id"] and payload["trace"]["trace_id"] == task["trace_id"]
                and payload["trace"]["event_id"] == r["event_id"], "different source task in earlier attempt")
        events.setdefault(r["kind"], []).append(payload)
    plans = [v for v in events["model.request"] if v["trace"]["module"] == "planner"]
    request = plans[-1]
    response, = [v for v in events["model.response"] if v["trace"]["call_id"] == request["trace"]["call_id"]]
    treq, tres = request["trace"], response["trace"]
    require(treq["protocol"] == tres["protocol"] == "ollama.chat" and treq["call_id"] == tres["call_id"],
            "earlier model exchange mismatch")
    req, res = contents(treq), contents(tres)
    message, = (m for m in req["messages"] if m["role"] == "user")
    seen = json.loads(message["content"])
    answer = json.loads(res["message"]["content"])
    denial = events["blocked"][0]["diagnostic"]
    require(seen["request"] == task["text"] and req["model"] == res["model"] and
            answer["steps"] == [] and denial["message"] == task["detail"],
            "earlier task has a different decision or diagnosis")
    candidates = []
    if denial["code"] == "operator.validation_failed":
        require(answer["disposition"] == "read_compute" and contents(events["planner.decomposed"][0]["trace"]) == answer
                and all(k not in events for k in ("operator.verified", "operator.installed", "operator.reused",
                                                  "planner.resumed")), "earlier task published a candidate")
        authors = [r for r in events["model.request"] if r["trace"]["module"] == "operator-author"]
        denied = events["operator.rejected"]
        require(len(authors) == len(denied) == len(events["operator.author_request"]), "missing rejected attempt")
        for i, (issued, failed) in enumerate(zip(authors, denied), 1):
            result, = [r for r in events["model.response"] if r["trace"]["call_id"] == issued["trace"]["call_id"]]
            body = contents(result["trace"])
            candidate = json.loads(body["message"]["content"])
            require(body["model"] == contents(issued["trace"])["model"] and failed["trace"]["attempt"] == i
                    and set(candidate) == {"instructions"} and isinstance(candidate["instructions"], list),
                    "earlier rejected candidate lacks its author's response")
            candidates.append(dict(model=body["model"], instructions=candidate["instructions"], code=failed["trace"]["code"]))
    else:
        require(denial["code"] == "planner.unsupported" and answer["disposition"] == "unsupported"
                and answer["reason"] == task["detail"], "unsupported prior attempt type")
    start, end = utc_ns(rows[0]["utc"]), utc_ns(rows[-1]["utc"])
    require(start <= end < submitted_ns, "earlier task did not precede main submission")
    return dict(task_id=task["id"], text=task["text"], model=req["model"], disposition=answer["disposition"],
                reason=denial["message"], state=task["state"], code=denial["code"], dispatches=0,
                offered=[x["id"] for x in seen["capabilities"]], first_ns=str(start), final_ns=str(end),
                request_sha256=treq["artifact"]["sha256"], response_sha256=tres["artifact"]["sha256"],
                task_sha256=sha(path), rejected_authors=candidates)


def prior_chapter(prior, face):
    im = Image.new("RGB", (820, 515), DARK)
    d = ImageDraw.Draw(im)
    text(d, 36, 41, "前一次独立提交", 23, "#a3dfbd", face)
    text(d, 35, 106, prior["state"], 55, GOLD, face)
    d.line((36, 188, 783, 188), fill="#53756b")
    text(d, 36, 219, prior["task_id"], 27, "#f4f3ed", face, 747)
    text(d, 36, 282, "模型返回  " + prior["disposition"], 25, "#d5e5d9", face, 747)
    authors = prior.get("rejected_authors", [])
    text(d, 36, 347, "算子安装  0     工具派发  0" if authors else "工具派发  0", 25, "#d5e5d9", face)
    text(d, 36, 437, "账本时间  " + clock(ns(prior["first_ns"])) + " — " + clock(ns(prior["final_ns"])),
         20, "#b5cbbf", face, 747)
    if authors:
        info = [(f"被拒的候选 {i}", json.dumps(r["instructions"], ensure_ascii=False)) for i,r in enumerate(authors, 1)]
        return Chapter("回看先前被拒的候选程序", ns(prior["final_ns"]), 7, [
            ("前一次独立提交", prior["reason"]), *info, ("后续", "相同原文随后以新任务 ID 提交")],
            im, "前一任务的模型响应与验收拒绝记录", prior["task_id"])
    return Chapter("回看前一次独立提交", ns(prior["final_ns"]), 6, [
        ("同一请求的首次判断", prior["reason"]), ("当时使用的模型", prior["model"]),
        ("内核记录的终态", prior["code"]), ("接下来", "查看新任务 ID 下的完整运行")],
        im, "前一任务的模型回执与账本摘录", prior["task_id"])


def program(step):
    args = step["args"]
    return args["program"] if set(args) == {"program"} else json.dumps(args, ensure_ascii=False)


def display_value(value, unit):
    if value is None:
        return "无有效测量"
    return f"{value:.4g}" if unit == "0..255" else f"{value:.4g} {unit}"


def frame_program(arguments):
    if isinstance(arguments.get("program"), str):
        return arguments["program"]
    rows, cols = arguments.get("row_count"), arguments.get("column_count")
    require(type(rows) is int and type(cols) is int and isinstance(arguments.get("channel"), str)
            and isinstance(arguments.get("statistic"), str), "unknown frame query arguments")
    return f"{arguments['channel']} | grid({rows},{cols}) | {arguments['statistic']}"


def pages(rows, size):
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def observed_rgb(batch, root, start, end):
    """Find a real RGB camera in an arbitrary observation receipt, without imposing a source name."""
    obs = batch.get("observations", batch)
    if not obs.get("samples") or obs.get("basis") != "published":
        return None
    require(obs["clock_id"] == "unix" and start <= ns(obs["as_of_ns"]) <= end, "invalid observation time")
    for sample in obs["samples"]:
        if sample.get("media_type") != "image/png" or not sample.get("calibration_ref"):
            continue
        require(sample["kind"] == "artifact" and sample["actor_ref"] == obs["actor_ref"]
                and sample["clock_id"] == "unix" and ns(sample["stamp_ns"]) <= ns(obs["as_of_ns"]),
                "unrelated camera observation")
        with Image.open(artifact(root, sample["artifact_ref"], ".png")) as im:
            if im.format == "PNG" and im.mode in ("RGB", "RGBA"):
                binding = dict(rgb_source_id=sample["source_id"], frame_id=sample["frame_id"],
                               calibration_ref=sample["calibration_ref"])
                return SimpleNamespace(rgb=im.convert("RGB"), actor=obs["actor_ref"], g=binding,
                                       sample=sample, as_of_ns=ns(obs["as_of_ns"]))
    return None


def fusion_chapter(view, stamp, face):
    im = Image.new("RGB", (820, 515), DARK)
    d = ImageDraw.Draw(im)
    grid = view.grid
    tick = draw_map(d, (18, 53, 803, 463), grid, view.sources, face)
    width = 796 // len(view.sources)
    for i, identifier in enumerate(view.sources):
        x = 18 + i * width
        d.rectangle((x, 16, x+13, 29), fill=SOURCE_COLORS[i])
        text(d, x+21, 13, source_name(identifier), 16, "#dceade", face, width-26)
    text(d, 22, 477, f"上方为机体前方 +X   ·   左侧为 +Y   ·   网线间隔 {tick} m", 18, "#dceade", face, 770)
    rows = [(source_name(identifier), f"{n} 个命中格") for identifier, n in zip(view.sources, view.counts)]
    rows += [("多源共同命中", str(view.data["overlap_cells"]) + " 格")]
    return Chapter("返回静止多传感器融合图", stamp, 5, rows, im,
                   f"机体坐标系 {view.body}   ·   栅格边长 {grid['resolution_m']:g} m")


def operator_chapters(operation, operator_policy):
    sections = [Chapter("模型将感知与计算拆分", operation["decomposed_ns"], 5, [
        ("测量输入", json.dumps(operation["original_inputs"], ensure_ascii=False)), ("计算契约", operation["contract"]),
        ("契约描述", operation["spec"])])]
    for r in operation["events"]:
        phase, stamp = r["phase"], r["stamp_ns"]
        if phase == "operator.gap":
            sections.append(Chapter("发现尚未安装的算子", stamp, 3, [("缺失契约", operation["contract"]),
                                    ("账本诊断", r["code"])]))
        elif phase == "model.request":
            context = "数值子任务与已测量的有序数列" if r["question_scope"] == "calculation_only" else "原始任务与计算规格"
            rows = [("作者模型", r["model"]), ("作者收到的任务范围", context), ("算子规格", operation["spec"])]
            if r["feedback_code"]:
                rows.append(("上一次验证反馈", r["feedback_code"]))
            sections.append(Chapter("调用模型编写数值程序", stamp, 3, rows))
        elif phase == "model.response":
            sections.append(Chapter("模型返回候选程序", stamp, 5, [("原始程序指令",
                json.dumps(r["candidate"], ensure_ascii=False)), ("作者模型", r["model"])]))
        elif phase == "operator.rejected":
            sections.append(Chapter("候选未通过验证", stamp, 3, [("验证诊断", r["code"]), ("候选次数", r["attempt"])]))
        elif phase == "operator.verified":
            require(operator_policy, "missing the independent deployment policy")
            cases = read(operator_policy)["contracts"][operation["contract"]]["cases"]
            require(len(cases) == r["validation"]["case_count"], "case count differs from published validation")
            checked = [(f"用例 {i}   ·   输入数列", f"{json.dumps(c['sample'])}  →  {c['expected']:g}")
                       for i, c in enumerate(cases, 1)]
            for group in pages(checked, 4):
                sections.append(Chapter("独立用例验收通过", stamp, 4, [
                    ("预期值与独立复算一致", str(len(cases)) + " / " + str(len(cases)) + " 个部署方用例通过"), *group]))
        elif phase in ("operator.installed", "operator.reused"):
            sections.append(Chapter("安装通过验收的程序" if phase == "operator.installed" else "直接复用已安装的程序",
                                    stamp, 4, [("算子契约", operation["contract"]), ("内容摘要", operation["digest"])]))
    sections.append(Chapter("同一任务继续执行", operation["resumed_ns"], 3, [
        ("已锁定的运行器", operation["runner"]), ("采用的程序", operation["digest"])]))
    return sections


def mission(source, face, *, operator_file=None, operator_policy=None):
    file, recording = source / "task.json", source / "recording"
    receipt = TaskReceipt(file, recording, step_index=0)
    task = receipt.task
    journal = Journal(receipt)
    submit = client_exchange(source, task)
    require(task["trigger"]["kind"] == "user" and submit["task_id"] == task["id"]
            and submit["request_text"] == task["text"] and submit["http_status"] in (200, 201, 202)
            and ns(submit["sent_ns"]) <= ns(receipt.events[0]["stamp_ns"]) <= ns(submit["acknowledged_ns"]),
            "missing original accepted user POST")
    decision = model_decision(journal, task)
    chapters = [Chapter("提交自然语言任务", ns(submit["sent_ns"]), 5,
                        [("用户原文", task["text"]), ("任务入口", "POST /agent/tasks")])]
    for revision in decision["revisions"]:
        chapters.extend([
            Chapter("首次提交模型规划", revision["initial_request_ns"], 3, [
                ("能力范围", decision["authority"]), ("模型", decision["model"])]),
            Chapter("内核回传参数校验结果", revision["revised_ns"], 4, [
                ("校验反馈", revision["feedback"]), ("代码", revision["code"])])])
    candidates = [(c["id"], c["description"]) for c in decision["offered"]]
    for group in pages(candidates, 5):
        chapters.append(Chapter("模型读取可用能力", decision["request_ns"], 5,
                                [("本次模型输入", f"{decision['model']}   ·   {len(candidates)} 项只读能力"), *group]))
    choices = [(f"第 {i} 步    {step['tool']}", program(step)) for i, step in enumerate(task["steps"], 1)]
    if decision["computation"]:
        chapters.extend(operator_chapters(decision["computation"], operator_policy))
    else:
        for group in pages(choices, 3):
            chapters.append(Chapter("模型返回结构化计划", decision["response_ns"], 6,
                                    [("返回模型", f"{decision['model']}   ·   {decision['duration_ms']/1000:.2f} s"), *group]))
    frozen = [(s["tool"], s["version"] + "    " + s["binding"][:23] + "…") for s in task["steps"]]
    for group in pages(frozen, 4):
        chapters.append(Chapter("内核校验并锁定工具版本", decision["frozen_ns"], 4,
                                [("本次部署权限", decision["authority"]), *group]))
    visual = None
    results = []
    for index, step in enumerate(task["steps"]):
        op = step["operation_id"]
        evidence = step["evidence"]
        for event, key in (("dispatch", "dispatch_ns"), ("tool.request", "tool_request_ns"),
                           ("tool.response", "tool_response_ns"), ("verified", "verified_ns")):
            record, payload = journal.one(event, op)
            require((record.get("tool") or record["module"]) == step["tool"], "unexpected executor")
            if event == "dispatch":
                require(payload["step"]["binding"] == step["binding"], "dispatch pin differs")
                result = dict(tool=step["tool"], operation_id=op, binding=step["binding"])
            elif event == "tool.request":
                require(contents(payload["trace"])["arguments"] == step["args"], "different tool arguments")
            elif event == "tool.response":
                digest = payload["trace"]["artifact"]["sha256"]
                # Older public receipts redact the numeric language's "tokens" key. Recover only this typed
                # field from the archived installed artifact, and require the entire original receipt hash.
                derived = evidence.get("derived", {})
                if operator_file and derived.get("program") == {"tokens":"[REDACTED]"}:
                    evidence = {**evidence, "derived":{**derived, "program":read(operator_file)["program"]}}
                require(digest == sha256(encoded(evidence)).hexdigest(),
                        "tool response digest differs from final evidence")
            result[key] = ns(record["stamp_ns"])
        result["evidence_sha256"] = sha256(encoded(evidence)).hexdigest()
        results.append(result)
        position = f"{index+1} / {len(task['steps'])}"
        chapters.append(Chapter("派发只读调用", result["tool_request_ns"], 3, [
            ("执行步骤   " + position, step["tool"]), ("原始参数", program(step)), ("操作 ID", op)]))
        if "analysis" in step["evidence"] and "observations" in step["evidence"]:
            original = receipt if not index else TaskReceipt(file, recording, step_index=index)
            query = QueryFrame(original, source / "artifacts", step["args"])
            if visual is None:
                visual = query
            a, obs = query.analysis, original.batch["observations"]
            src = query.samples
            channels = [s["source_id"] for s in obs["samples"]]
            result.update(program=a["program"], analysis=a, sources=channels)
            chapters.append(Chapter("读取本次测量的原始帧", ns(obs["as_of_ns"]), 5, [
                ("RGB 来源", src["rgb"]["source_id"]), ("同批对齐深度", src["depth"]["source_id"]),
                ("对齐坐标系", a["frame_id"]), ("图像尺寸", f"{a['width']} × {a['height']} px")],
                query.rgb, f"原始 RGB   ·   {clock(ns(src['rgb']['stamp_ns']))}"))
            if a["channel"] == "depth":
                chapters.append(Chapter("核对对齐深度", result["tool_response_ns"], 4,
                                        [("深度输入", src["depth"]["source_id"]), ("测量表达式", a["program"]),
                                         ("深度文件", a["depth_ref"][:26] + "…")],
                                        depth_preview(query), f"深度伪彩   ·   {clock(ns(src['depth']['stamp_ns']))}"))
            unit = a["unit"]
            measurements = []
            for i, row in enumerate(a["regions"], 1):
                value = display_value(row["value"], unit)
                measurements.append((f"区域 {i}   ·   {row['valid_pixels']}/{row['pixels']} 有效像素", value))
            annotated = query.annotated(face)
            for group in pages(measurements, 5):
                chapters.append(Chapter("在实拍画面标出测量结果", result["tool_response_ns"], 5,
                                        [("使用的统计量", a["statistic"]), *group], annotated,
                                        "区域测量值   ·   影像强度刻度 0–255" if unit == "0..255" else
                                        "测量结果叠加在同批次的 RGB 原图"))
            chapters.append(Chapter("查看工具返回的测量结论", result["tool_response_ns"], 5,
                                    [("工具回执原文", a["summary"]), ("测量表达式", a["program"])],
                                    annotated, "区域从上到下、从左到右编号"))
            if decision["computation"]:
                op = decision["computation"]
                require(operator_file and operator_policy, "need the installed operator and the deployed policy bytes")
                calc = qualified_receipt(evidence, operator_file, operator_policy, op["digest"], op["contract"])
                if op["mode"] == "installed":
                    last = [row for row in op["events"] if row["phase"] == "model.response"][-1]
                    verified, = [row for row in op["events"] if row["phase"] == "operator.verified"]
                    require(normalized(last["candidate"]) == calc["program"] and verified["validation"] == calc["proof"],
                            "installed program differs from the authored and validated candidate")
                result["calculation"] = calc
                intensity = calc["unit"] == "0..255"
                chapters.append(Chapter("用实测数值执行算子", result["tool_response_ns"], 5, [
                    ("输入（亮度刻度 0–255）" if intensity else "输入", "、".join(display_value(v, calc["unit"]) for v in calc["inputs"])),
                    ("执行的程序", json.dumps(calc["program"]["tokens"])),
                    ("工具返回值", display_value(calc["value"], calc["unit"]))], annotated,
                    "输入取自上图指定区域，数值与回执已复算"))
        else:
            fields = ", ".join(step["evidence"])
            captured = observed_rgb(step["evidence"], source / "artifacts", result["tool_request_ns"],
                                    result["tool_response_ns"])
            if captured:
                visual = visual or captured
                chapters.append(Chapter("读取现场画面", captured.as_of_ns, 4, [
                    ("选用工具", step["tool"]), ("图像来源", captured.sample["source_id"]),
                    ("光学坐标系", captured.sample["frame_id"])], captured.rgb,
                    "回执 RGB   ·   " + clock(ns(captured.sample["stamp_ns"]))))
            fusion = step["evidence"].get("fusion", step["evidence"])
            if fusion.get("type") == "perception.stationary_fusion":
                validated = Fusion(file, recording, source / "artifacts", step_index=index)
                chapters.append(fusion_chapter(validated, result["tool_response_ns"], face))
                result.update(fusion_sources=validated.sources, occupied_cells=fusion["occupied_cells"],
                              overlap_cells=fusion["overlap_cells"])
            else:
                chapters.append(Chapter("工具返回证据", result["tool_response_ns"], 3,
                                        [("完成步骤   " + position, step["tool"]), ("返回字段", fields),
                                         ("证据摘要", result["evidence_sha256"][:30] + "…")],
                                        captured.rgb if captured else None, "工具读取的原始彩色图像" if captured else ""))
        if index < len(task["steps"]) - 1:
            chapters.append(Chapter("内核验收本步回执", result["verified_ns"], 2,
                                    [("已验证", step["tool"]), ("执行步骤", position)]))
    final, terminal = journal.one("verified", results[-1]["operation_id"])
    require(terminal["state"] == task["state"] == "SUCCEEDED", "missing task terminal receipt")
    actual = (results[-1]["verified_ns"] - ns(submit["sent_ns"])) / 1e9
    chapters.append(Chapter("任务完成", ns(final["stamp_ns"]), 5, [
        ("车端任务终态", "SUCCEEDED"), ("实际经过时间", f"{actual:.2f} s"),
        ("已验证的工具调用", "、".join(s["tool"] for s in task["steps"])),
        ("本次调用的运动工具", "0")], chapters[-1].image, chapters[-1].image_label))
    require(all(a.stamp <= b.stamp for a, b in zip(chapters, chapters[1:])), "scene source clocks regressed")
    stream = source / "sensor-timeline.jsonl"
    window = ColorWindow(stream, source / "artifacts", visual, max_seconds=MAX_WINDOW_SECONDS) if visual and stream.is_file() else None
    if window:
        require(window.start <= chapters[0].stamp <= chapters[-1].stamp <= window.end, "incomplete live capture window")
    return task, submit, decision, results, chapters, window, receipt


def frame(ch, face, pic, label, task_id, chapter_no, total):
    canvas = Image.new("RGB", (W, H), PAPER)
    d = ImageDraw.Draw(canvas)
    text(d, 39, 28, "EDGE AGENT   /   车端任务记录", 20, TEAL, face)
    text(d, 1046, 29, "源事件回放   ·   北京时间", 19, MUTED, face)
    text(d, 38, 72, ch.title, 42, INK, face, W - 95)
    d.line((38, 142, 1402, 142), fill=RULE, width=1)
    box = (38, 165, 858, 680)
    d.rectangle(box, fill=DARK)
    if pic is not None:
        paste(canvas, pic, box)
    else:
        text(d, 151, 401, "此时刻没有可核验的实时相机帧", 27, "#d7e5dc", face, 660)
    text(d, 39, 699, label, 20, MUTED, face, 820)
    d.line((879, 168, 879, 724), fill=RULE)
    d.rounded_rectangle((910, 170, 1395, 215), 7, fill="#e0e9e1")
    text(d, 926, 180, clock(ch.stamp) + "   ·   源事件", 22, TEAL, face, 455)
    # Give the original human wording, model output and measurement figures the most space.
    y = 242
    for i, (key, value) in enumerate(ch.rows):
        text(d, 913, y, key, 18, TEAL, face, 473)
        if len(ch.rows) <= 3:
            size, max_lines = (31 if ch.title == "提交自然语言任务" and i == 0 else 24), 5
        elif len(ch.rows) > 5:
            size, max_lines = 19, 1
        else:
            size, max_lines = 23, 2
        paragraph(d, (913, y + 27), str(value), size, 475, face, INK, max_lines)
        chars_width = d.textlength(str(value), font=font(face, size))
        lines = min(max_lines, max(1, int(chars_width // 475) + 1))
        y += 35 + (size + 9) * lines + (14 if len(ch.rows) <= 4 else 3)
    d.line((38, 747, 1402, 747), fill=RULE)
    text(d, 39, 768, task_id, 17, MUTED, face, 960)
    text(d, 1173, 765, f"任务回放    {chapter_no} / {total}", 19, TEAL, face, 223)
    return canvas


def chaptered_video(out, scenes, chapters, ffmpeg, ffprobe):
    raw = out / "pictures.mp4"
    _, frames = write_video(raw, scenes, ffmpeg, ffprobe)
    meta = out / "chapters.ffmeta"
    rows, offset = [";FFMETADATA1"], 0
    for chapter in chapters:
        rows.extend(("[CHAPTER]", "TIMEBASE=1/1000", f"START={offset*1000}",
                     f"END={(offset+chapter.seconds)*1000}", "title=" + chapter.title))
        offset += chapter.seconds
    meta.write_text("\n".join(rows) + "\n", encoding="utf8")
    video = out / "task.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-i", str(raw), "-f", "ffmetadata", "-i", str(meta),
                    "-map", "0:v:0", "-map_chapters", "1", "-c", "copy", "-movflags", "+faststart",
                    str(video)], check=True)
    decoded = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-select_streams", "v:0",
                         "-count_frames", "-show_entries", "stream=codec_name,nb_read_frames,width,height:"
                         "chapter=start_time,end_time:chapter_tags=title", "-of", "json", str(video)]))
    track, = decoded["streams"]
    require((track["codec_name"], int(track["nb_read_frames"]), track["width"], track["height"])
            == ("h264", frames, W, H), "chaptered video failed decode")
    original = decoded.get("chapters", [])
    require(len(original) == len(chapters), "MP4 lost source chapters")
    offset = 0
    for ch, expected in zip(original, chapters):
        require(ch["tags"]["title"] == expected.title and float(ch["start_time"]) == offset
                and float(ch["end_time"]) == offset + expected.seconds, "MP4 chapter mismatch")
        offset += expected.seconds
    raw.unlink()
    meta.unlink()
    return frames


def render(source, destination, face, ffmpeg, ffprobe, *, prior_attempt=None, reuse_source=None,
           operator_file=None, operator_policy=None):
    bindings = {"operator_file":operator_file, "operator_policy":operator_policy}
    task, submit, decision, results, chapters, window, receipt = mission(source, face, **bindings)
    elapsed = (chapters[-1].stamp - chapters[0].stamp) / 1e9
    windows = {"":window}
    followup = None
    if reuse_source:
        other, posted, chosen, steps, more, other_window, record = mission(reuse_source, face, **bindings)
        first, second = decision["computation"], chosen["computation"]
        require(first and second and first["mode"] == "installed" and second["mode"] == "reused"
                and first["digest"] == second["digest"] and first["contract"] == second["contract"]
                and task["id"] != other["id"] and task["trace_id"] != other["trace_id"]
                and chapters[-1].stamp <= more[0].stamp, "second task did not independently reuse the installed operator")
        left, = [s for s in results if "calculation" in s]
        right, = [s for s in steps if "calculation" in s]
        require(left["operation_id"] != right["operation_id"] and
                ns(left["analysis"]["rgb_stamp_ns"]) < ns(right["analysis"]["rgb_stamp_ns"]) and
                ns(left["analysis"]["depth_stamp_ns"]) < ns(right["analysis"]["depth_stamp_ns"]),
                "second task failed to capture a separate measurement")
        for chapter in more:
            chapter.source_task = other["id"]
        more[0].title = "再次提交独立感知任务"
        windows[other["id"]] = other_window
        followup = dict(task_id=other["id"], original_request=other["text"], status=other["state"], model=chosen,
                        operations=steps, task_sha256=record.task_sha, timeline_sha256=record.record.timeline_digest,
                        post_sha256=posted["request_body_sha256"], http_response_sha256=posted["response_body_sha256"],
                        source_elapsed_s=(more[-1].stamp - more[0].stamp) / 1e9)
        if other_window:
            followup["sensor_window_sha256"] = sha(reuse_source / "sensor-timeline.jsonl")
        chapters.extend(more)
    prior = blocked_attempt(prior_attempt, task, chapters[0].stamp) if prior_attempt else None
    if prior:
        chapters.insert(1, prior_chapter(prior, face))
    with map_output(destination) as out:
        preview_refs = []
        snapshots = []

        def scenes():
            for i, ch in enumerate(chapters):
                source_window = windows.get(ch.source_task)
                following = next((other for other in chapters[i+1:] if other.source_task == ch.source_task), ch)
                end = following.stamp
                cached_key, cached = object(), None
                ticks = ch.seconds * FPS
                for n in range(ticks):
                    tick = ch.stamp + (end - ch.stamp) * n // ticks
                    pic, label = ch.image, ch.image_label
                    key = "held" if pic is not None else None
                    if pic is None and source_window:
                        observed, stale = source_window.at(tick)
                        if observed:
                            stamp, ref, _ = observed
                            key = (ref, stale)
                            pic = source_window.image(ref)
                            label = ("最近收到的车载 RGB" if stale else "车载 RGB") + "   ·   " + clock(stamp) + "  采样"
                            if not preview_refs or preview_refs[-1]["ref"] != ref:
                                preview_refs.append(dict(task_id=ch.source_task or task["id"], stamp_ns=str(stamp), ref=ref))
                    if key != cached_key:
                        cached = frame(ch, face, pic, label or "相机采集窗口", ch.source_task or task["id"], i+1, len(chapters))
                        cached_key = key
                    if not n:
                        name = f"stage-{i+1:02}.png"
                        cached.save(out / name)
                        snapshots.append(name)
                    yield ch.title, 1 / FPS, cached

        count = chaptered_video(out, scenes(), chapters, ffmpeg, ffprobe)
        chapter_table = []
        offset = 0
        for ch, screenshot in zip(chapters, snapshots):
            chapter_table.append(dict(title=ch.title, sec=offset, source_ns=str(ch.stamp), snapshot=screenshot,
                                      source_task_id=ch.source_task or task["id"]))
            offset += ch.seconds
        report = dict(task_id=task["id"], original_request=task["text"], status=task["state"], model=decision,
                      operations=results, timeline_sha256=receipt.record.timeline_digest,
                      task_sha256=receipt.task_sha, post_sha256=submit["request_body_sha256"],
                      submit_sha256=sha(source / "submit.json"), http_response_sha256=submit["response_body_sha256"],
                      video_sha256=sha(out / "task.mp4"), frames=count, fps=FPS, duration_s=count/FPS,
                      timing="guided replay of verified source events; real camera frames follow source time",
                      source_elapsed_s=elapsed, chapters=chapter_table,
                      camera_previews=preview_refs)
        if prior:
            report["prior_attempt"] = prior
        if followup:
            report["reuse"] = followup
        if window:
            report["sensor_window_sha256"] = sha(source / "sensor-timeline.jsonl")
        (out / "video.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
        links = " ".join(f'<button data-time="{row["sec"]}">{escape(row["title"])}</button>' for row in chapter_table)
        (out / "index.html").write_text(f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>车端任务记录</title>
<style>body{{max-width:1440px;margin:auto;padding:24px;background:#f4f3ed;color:#102c2a;font:18px/1.5 system-ui}}
h1{{font-size:clamp(22px,3vw,36px)}}video{{width:100%}}button{{margin:4px;padding:7px;cursor:pointer}}
a{{color:#006852}}nav{{margin:12px 0}}</style><h1>{escape(task["text"])}</h1>
<video controls playsinline preload="metadata" poster="{snapshots[0]}" src="task.mp4"></video><nav>{links}</nav>
<a href="video.json">查看模型选择、工具绑定和证据记录</a>
<script>document.querySelectorAll('button').forEach(b=>b.onclick=()=>{{let v=document.querySelector('video');v.currentTime=+b.dataset.time;v.play()}})</script></html>''', encoding="utf8")
    return report


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--source", type=Path, required=True)
    cli.add_argument("--out", type=Path, required=True)
    cli.add_argument("--prior-attempt", type=Path, help="earlier blocked task ledger from the same user wording")
    cli.add_argument("--reuse-source", type=Path, help="later task which reuses the independently qualified operator")
    cli.add_argument("--operator-file", type=Path, help="installed numeric definition exported from the edge host")
    cli.add_argument("--operator-policy", type=Path, help="unaltered deployed qualification policy")
    cli.add_argument("--font", required=True)
    cli.add_argument("--ffmpeg", default=shutil.which("ffmpeg"))
    cli.add_argument("--ffprobe", default=shutil.which("ffprobe"))
    args = cli.parse_args()
    if not args.ffmpeg or not args.ffprobe:
        cli.error("FFmpeg and FFprobe are required")
    report = render(args.source, args.out, args.font, args.ffmpeg, args.ffprobe, prior_attempt=args.prior_attempt,
                    reuse_source=args.reuse_source, operator_file=args.operator_file, operator_policy=args.operator_policy)
    print(json.dumps({k: report[k] for k in ("task_id", "duration_s", "video_sha256")}))


if __name__ == "__main__":
    main()
