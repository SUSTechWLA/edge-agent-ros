"""Durable task-level exploration. Planning providers have no motion authority."""
from copy import deepcopy
import json
import math
from pathlib import Path
import re
from threading import RLock
import time
import unicodedata
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from edge_contracts.occupancy import Grid, clearance_mask, finite
from edge_contracts.service_directory import PluginUnavailable, ServiceSnapshot
from exploration.ros_sim import checkpoint
from navigation.grid_corridor import route_admission, segment_admitted


ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
TERMINAL = {"SUCCEEDED", "BLOCKED", "RECOVERY_REQUIRED"}
CORE_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELED", "RECOVERY_REQUIRED"}
FRONTIER = "exploration.frontier"
SEMANTIC = "map.semantic_observe"
RESOLVE = "map.semantic_resolve"
PUBLISH = "map.publish"
LOAD = "map.load"
ROUTE = "planning.survey_route"
CORRIDOR = "planning.corridor_review"
RECOVERABLE_CODES = {"navigation.no_safe_path", "navigation.path_blocked", "navigation.ttc_insufficient"}
NATIVE_REVIEW_CODES = {"navigation.dynamic_yield", "navigation.dynamic_observing", "navigation.dynamic_unobserved",
                       "navigation.dynamic_recheck", "navigation.dynamic_clear"}


def request(url, body=None):
    data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode() if body is not None else None
    req = Request(url, data=data, headers={"Content-Type": "application/json"} if data else {})
    with urlopen(req, timeout=1.2) as response:
        raw = response.read(2*1024*1024+1)
    if len(raw) > 2*1024*1024:
        raise ValueError("provider_response_too_large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("provider_response_invalid")
    return value


def normalize(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


def xy(pose):
    return [pose["x"], pose["y"]]


def finite_stop(stop):
    values = [stop.get(k) for k in ("last_linear_mps","last_angular_rad_s","quiet_acquisition_ms","quiet_arrival_ms")]
    return (all(type(v) in (int,float) and math.isfinite(v) for v in values)
            and abs(values[0])<=.02 and abs(values[1])<=.04 and min(values[2:])>=240)


def unknown_point(raw, bounds, pose, avoid):
    grid = Grid(raw)
    x0, y0, x1, y1 = bounds
    options = []
    for i, cell in enumerate(grid.cells):
        if cell != -1:
            continue
        point = grid.center(i)
        if (x0 <= point[0] <= x1 and y0 <= point[1] <= y1
                and all(math.dist(point, item["center"]) > item["radius"] for item in avoid)):
            options.append((math.dist(point, pose), point))
    return min(options, default=(0, None))[1]


class Survey:
    def __init__(self, rover, config, state, directory, agent):
        self.rover, self.config, self.path, self.directory, self.agent = rover, config, Path(state), directory, agent
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        self.rows = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else {}
        self.next_tick = self.missing_since = 0.0

    def save(self):
        checkpoint(self.path, self.rows)

    def event(self, row, kind, detail, **data):
        sequence = row["events"][-1]["seq"]+1 if row["events"] else 1
        row["updated"] = time.time()
        row["events"].append(dict(seq=sequence, stamp=row["updated"], kind=kind, detail=detail, data=data))
        row["events"] = row["events"][-150:]
        self.save()

    def service(self, row, name, body):
        pin = ServiceSnapshot(self.directory).pin(name)
        reply = request(pin["endpoint"], body)
        latest = ServiceSnapshot(self.directory).pin(name)
        if latest["binding"] != pin["binding"]:
            raise ValueError("provider_binding_changed")
        row["bindings"][name] = {"plugin_id": pin["plugin_id"], "version": pin["plugin_version"],
                                  "binding": pin["binding"]}
        return reply

    def show(self, key=None):
        private = {"observations", "candidates", "resume_after", "area", "active", "visited", "avoid",
                   "last_plan", "pending_seen", "retries", "loaded", "publish_input"}
        with self.lock:
            selected = self.rows.get(key) if key else (
                next((row for row in self.rows.values() if row["status"] not in TERMINAL), None)
                or next(iter(reversed(self.rows.values())), None))
            mission = None
            if selected:
                mission = deepcopy({k: v for k, v in selected.items() if k not in private})
                mission["bounds"] = selected.get("area")
                tracking = (selected.get("active") or {}).get("tracking")
                if tracking and selected["phase"] == "NAVIGATING" and 0 <= time.time() - tracking["received"] < 1.5:
                    mission["tracking"] = dict(tracking)
                if mission.get("grid"):
                    g = mission["grid"]
                    g["origin_x"], g["origin_y"], g["origin_yaw"] = g.pop("origin")
            tasks = [{k: row[k] for k in ("id", "text", "status")} for row in reversed(self.rows.values())]
            aliases = self.config.get("area", {}).get("aliases", [])
            return dict(environment="simulation", area=next(iter(aliases), None), mission=mission, tasks=tasks)

    def poll(self, kind, body):
        op = body["operation_id"]
        key, separator, step = op.rpartition("/step/")
        trace = body.get("trace", {})
        args = body["arguments"]
        field = "area" if kind == "survey" else "place"
        target = args.get(field)
        version = body["version"]
        supported = ("1","2") if kind == "navigate" else ("1",)
        if (version not in supported or not ID.fullmatch(key) or not separator or step != "1"
                or trace.get("task_id") != key or not trace.get("parent_event_id")
                or set(args) != {field} or not isinstance(target, str) or not 0 < len(target) <= 120):
            raise ValueError("invalid_task_binding")
        with self.lock:
            existing = self.rows.get(key)
        if not existing:
            source = request(self.agent+"/agent/tasks?"+urlencode({"id":key, "limit":1}))
            steps = source.get("steps", [])
            tool = "survey_area" if kind == "survey" else "navigate_survey_place"
            if (source.get("id") != key or len(steps) != 1 or steps[0].get("operation_id") != op
                    or steps[0].get("tool") != tool or steps[0].get("args") != args or steps[0].get("version") != version
                    or source.get("state") not in {"RUNNING", "WAITING"}):
                raise ValueError("agent_intent_unavailable")
            with self.lock:
                existing = self.rows.get(key)
                if not existing:
                    if any(row["status"] not in TERMINAL or row.get("active")
                           for row in self.rows.values()):
                        return self.reply(op, "BLOCKED", "该机体已有任务或尚在对账")
                    existing = self.create(key, op, source["text"], trace, kind, target, version)
        with self.lock:
            if (existing["operation_id"], existing["kind"], existing["target"], existing.get("tool_version","1")) != (op, kind, target, version):
                raise ValueError("operation_identity_conflict")
            if existing["status"] == "SUCCEEDED":
                return self.reply(op, "SUCCEEDED", evidence=existing["evidence"])
            if existing["status"] in TERMINAL:
                return self.reply(op, existing["status"], existing.get("reason", "现场任务未完成"),
                                  evidence=existing.get("evidence"))
            return self.reply(op, "RUNNING")

    @staticmethod
    def reply(op, state, detail="", evidence=None):
        result = {"operation_id": op, "state": state}
        if evidence is not None:
            result["evidence"] = evidence
        if detail:
            result.update(detail=detail, error={"code":"exploration.operation_incomplete", "message":detail})
        return result

    def create(self, key, op, text, trace, kind, target, version="1"):
        row = dict(id=key, operation_id=op, text=text, kind=kind, target=target, tool_version=version, trace=trace,
                   status="RUNNING", phase="ADMITTED", created=time.time(), updated=time.time(), events=[],
                   coverage={"known":0, "total":0, "unknown":0}, grid=None, pose=None, map_ref=None, frontier=None,
                   path=[], markers=[], candidates=[], observations=[], active=None, anomaly=None, history=[],
                   visited=[], avoid=[], sequence=0, steps=0, bindings={}, retries=0, stagnant=0)
        self.rows[key] = row
        self.event(row, "task_admitted", "Agent 已绑定原任务与工具", operation_id=op, tool=kind)
        return row

    def intervene(self, body):
        key, issue, decision = (body[k] for k in ("task_id", "anomaly_id", "decision"))
        if not ID.fullmatch(key) or not ID.fullmatch(issue) or decision not in {"skip", "retry"}:
            raise ValueError("invalid_operator_decision")
        with self.lock:
            row = self.rows.get(key)
            if not row:
                raise KeyError(key)
            if any(item["id"] == issue and item["decision"] == decision for item in row["history"]):
                return self.show(key)
            anomaly = row["anomaly"]
            if (row["status"] != "WAITING_OPERATOR" or row["active"] or not anomaly or anomaly["id"] != issue):
                raise ValueError("anomaly_is_not_waiting")
            snapshot = self.rover.snapshot()
            if snapshot is None or abs(snapshot["pose"]["speed"]) > .025 or abs(snapshot["pose"]["yaw_rate"]) > .05:
                raise ValueError("stop_or_observation_unverified")
            row["history"].append({"id":issue, "decision":decision, "xy":anomaly["xy"]})
            if decision == "skip":
                row["avoid"].append({"center":anomaly["xy"], "radius":.33})
                row["anomaly"] = None
            else:
                anomaly.update(status="reviewing", attempts=0, viewpoints=[])
            row.update(status="RUNNING", phase="RESUMED", stagnant=0, resume_after=snapshot["stamp"])
            self.event(row, "resumed", "处理决定已接纳，原任务继续", decision=decision, anomaly_id=issue,
                       operation_id=row["operation_id"], xy=anomaly["xy"])
            return self.show(key)

    def finish(self, row, status, phase, reason, **evidence):
        row.update(status=status, phase=phase, reason=reason, path=[], route_curve=None, frontier=None)
        if evidence:
            row["evidence"] = evidence
        self.event(row, "finished", reason, status=status, **evidence)

    def tick(self):
        if time.monotonic() < self.next_tick:
            return
        self.next_tick = time.monotonic()+.30
        with self.lock:
            row = next((v for v in self.rows.values() if v["status"] not in TERMINAL), None)
            if row is None:
                return
            try:
                self.advance(row)
            except (OSError, ValueError, KeyError, TypeError, PluginUnavailable) as error:
                row["retries"] += 1
                if row["active"]:
                    self.reconcile(row, "后端响应中断，核对原移动", cancel=True)
                elif row["retries"] >= 5:
                    proof = {}
                    if row["kind"] == "navigate" and not row.get("loaded"):
                        row["destination"] = dict(state="unavailable", query=row["target"], map_ref=row.get("map_ref"),
                                                  candidates=[], reason=str(error)[:160])
                        proof["destination"] = row["destination"]
                    self.finish(row, "BLOCKED", "BLOCKED", "能力或观测不可用："+str(error)[:220], **proof)
                elif row["retries"] == 1:
                    self.event(row, "waiting", "等待可用的观测或插件", reason=str(error)[:160])

    def advance(self, row):
        snapshot = self.rover.snapshot()
        row["local_observation"] = snapshot.get("local_observation") if snapshot else None
        if snapshot:
            row["pose"], row["scan_m"] = snapshot["pose"], snapshot["scan_m"]
            pose_stamp = snapshot.get("pose_stamp_ns")
            speeds = row.setdefault("measured_speeds", [])
            if type(pose_stamp) is int and (not speeds or pose_stamp-speeds[-1][0] >= 200_000_000):
                speeds.append([pose_stamp, snapshot["pose"]["speed"]])
                del speeds[:-180]
            # Preserve measured positions with the task so a browser reload can replay its trail.
            trail = row.setdefault("trajectory", [])
            position = xy(snapshot["pose"])
            if not trail or math.dist(trail[-1], position) >= .025:
                trail.append(position)
                del trail[:-600]
        if row["active"]:
            if not snapshot:
                self.reconcile(row, "观测失效，核对原动作并停车", cancel=True,
                               observation_issue=getattr(self.rover,"observation_issue",None))
            else:
                self.reconcile(row)
            return
        if row["status"] == "WAITING_OPERATOR":
            return
        if row.get("recovery"):
            if time.time() >= row["recovery"]["deadline"]:
                return self.recovery_blocked(row, row["recovery"], "复查期结束，通道仍未取得可通行证据")
            if snapshot:
                return self.stationary_review(row, snapshot)
            return
        if time.time() - row["created"] > self.config["timeout_s"]:
            self.finish(row, "BLOCKED", "BLOCKED", "任务时间预算耗尽")
            return
        if snapshot is None:
            if not self.missing_since:
                self.missing_since = time.monotonic()
            if time.monotonic() - self.missing_since > 14:
                self.finish(row, "BLOCKED", "BLOCKED", "缺少同坐标系下的新鲜位姿、栅格或激光")
            return
        self.missing_since = 0.0
        if row["kind"] == "survey":
            if "epoch_ns" not in row:
                return self.admit_mapping(row, snapshot)
            if snapshot["stamp"] <= max(row["epoch_ns"], row.get("resume_after", 0)):
                return
            return self.map_step(row, snapshot)
        return self.nav_step(row, snapshot)

    def admit_mapping(self, row, snapshot):
        area = self.config["area"]
        if normalize(row["target"]) not in {normalize(alias) for alias in area["aliases"]}:
            return self.finish(row, "BLOCKED", "BLOCKED", "该区域没有探索授权")
        # Persist the intent before the reset. A crash here cannot reset a partly built map.
        if row.get("reset_prepared"):
            return self.finish(row, "RECOVERY_REQUIRED", "BLOCKED", "空图初始化结果未知，禁止重复清空")
        row["reset_prepared"] = True
        row["area"] = area["bounds"]
        self.save()
        epoch = self.rover.begin()
        initial = {**snapshot["grid"], "cells": [-1]*len(snapshot["grid"]["cells"])}
        total = self.coverage(initial, row["area"])["total"]
        row.update(phase="EMPTY_MAP", epoch_ns=epoch, grid=initial,
                   coverage={"known":0, "total":total, "unknown":total}, initial_pose=snapshot["pose"])
        self.event(row, "empty_map", "已开新建图会话，范围内栅格全部未知", known=0, total=total, ros_stamp_ns=epoch)
        self.next_tick = time.monotonic()+1.7

    @staticmethod
    def coverage(raw, bounds):
        grid = Grid(raw)
        x0,y0,x1,y1 = bounds
        values = [v for i,v in enumerate(grid.cells) if (p:=grid.center(i)) and x0 <= p[0] <= x1 and y0 <= p[1] <= y1]
        known = sum(v >= 0 for v in values)
        return {"known":known, "total":len(values), "unknown":len(values)-known}

    def update_semantic(self, row, snap, pose):
        for sampled in self.rover.observations:
            previous = row["observations"][-1] if row["observations"] else None
            if sampled["stamp_s"]*1e9 <= row["epoch_ns"] or previous and (
                sampled["stamp_s"] <= previous["stamp_s"] or
                sampled.get("label") == previous.get("label") and
                math.dist(sampled["pose"][:2],previous["pose"][:2]) < .11 and sampled["stamp_s"]-previous["stamp_s"] < 4
            ):
                continue
            row["observations"] = [*row["observations"][-94:], sampled]
        if not row["observations"]:
            return
        if time.time()-row.get("semantic_checked",0) < 1.3:
            return
        row["semantic_checked"] = time.time()
        try:
            reply = self.service(row, SEMANTIC, dict(grid=snap["grid"], map_id=row["id"], frame=snap["grid"]["frame"],
                                 observations=row["observations"], pose=pose, bounds=row["area"],
                                 footprint_radius=self.config["radius"]))
        except (PluginUnavailable, OSError) as error:
            if not row.get("semantic_unavailable"):
                row["semantic_unavailable"] = True
                self.event(row,"semantic_unavailable","语义提供者暂不可用，继续建立通行图", reason=str(error)[:160])
            return
        row.pop("semantic_unavailable",None)
        if reply.get("status") != "ok" or reply.get("map_id") != row["id"] or reply.get("frame") != snap["grid"]["frame"]:
            raise ValueError("semantic_binding_mismatch")
        previous = {m["id"]:m["status"] for m in row["markers"]}
        row["candidates"] = reply["candidates"]
        row["markers"] = [dict(id=p["entity_id"],label=p["name"],x=p["anchor"][0],y=p["anchor"][1],
                               status=p["state"], entrance=p.get("entrance_pose")) for p in row["candidates"]]
        for mark in row["markers"]:
            if previous.get(mark["id"]) != mark["status"]:
                self.event(row, "semantic_confirmed" if mark["status"]=="confirmed" else "semantic_candidate",
                           ("多视角与入口通过核验：" if mark["status"]=="confirmed" else "发现候选门牌：")+mark["label"],
                           marker=mark, binding=row["bindings"][SEMANTIC]["binding"])

    def map_step(self, row, snap):
        raw, pose = snap["grid"], xy(snap["pose"])
        row["grid"], row["coverage"] = raw, self.coverage(raw,row["area"])
        self.update_semantic(row,snap,pose)
        if row["phase"] == "EMPTY_MAP":
            row["phase"] = "EXPLORING"
            self.event(row, "observed", "收到首批新鲜激光和位姿，开始选择可达前沿",
                       **row["coverage"], ros_stamp_ns=snap["stamp"])
        if row.get("publish_input"):
            return self.publish(row, snap)
        if row["coverage"]["unknown"] == 0:
            return self.publish(row, snap)
        args = dict(grid=raw, pose=pose, bounds=row["area"], footprint_radius=self.config["radius"],
                    min_step=.17, max_step=self.config["max_step"], visited=row["visited"], avoid=row["avoid"])
        if row["anomaly"]:
            return self.review(row, snap, args)
        excluded = [dict(center=p,radius=.13) for p in row.get("blocked_goals",[])
                    if math.dist(p,pose) >= self.config["radius"]+.25]
        args["avoid"] = [*row["avoid"], *excluded]
        exhausted = row["sequence"] >= self.config["max_goals"]
        plan = ({"state":"PARTIAL","reason":"goal_budget"} if exhausted else self.service(row, FRONTIER, args))
        row["retries"] = 0
        if plan["state"] == "WAYPOINT" and row["stagnant"] < 3:
            return self.start_leg(row, snap, plan, "frontier")
        if plan["state"] == "COMPLETE":
            return self.publish(row, snap)
        if len(row["history"]) >= self.config.get("max_interventions", 1):
            return self.publish(row, snap)
        pending = next((p for p in row["candidates"] if p["state"]=="proposed" and p["reason"]=="need_alternate_view"
                        and all(math.dist(p["anchor"],a["center"]) > a["radius"] for a in row["avoid"])), None)
        point = pending["anchor"] if pending else unknown_point(raw,row["area"],pose,row["avoid"])
        if point is None or row["avoid"] and not pending and plan["state"] != "WAYPOINT":
            return self.publish(row, snap)
        row["phase"] = "RESCAN"
        row["anomaly"] = dict(id=f"review-{len(row['history'])+1}",status="reviewing",reason=(
            "候选门牌缺少不同视角" if pending else "前沿不可达或连续观测没有增加覆盖"),xy=point,
            attempts=0, viewpoints=[], area_kind="semantic" if pending else "coverage",
            initial_known=row["coverage"]["known"], label=pending["name"] if pending else None, actions=[])
        self.event(row, "anomaly", "检测到需复查区域，先尝试换视角", xy=point, reason=plan["reason"])
        self.next_tick = time.monotonic()+.7

    def review(self, row, snap, args):
        anomaly = row["anomaly"]
        resolved = (anomaly["area_kind"] == "coverage" and
                    row["coverage"]["known"] > anomaly["initial_known"] and
                    Grid(snap["grid"]).index(anomaly["xy"]) >= 0 and
                    snap["grid"]["cells"][Grid(snap["grid"]).index(anomaly["xy"])] >= 0)
        if anomaly["area_kind"] == "semantic":
            resolved = any(p["state"]=="confirmed" and p["name"]==anomaly["label"] for p in row["candidates"])
        if resolved:
            row.update(anomaly=None, stagnant=0, phase="EXPLORING")
            self.event(row, "anomaly_resolved", "新的观测已解开该区域的疑问", xy=anomaly["xy"])
            return
        if anomaly["attempts"] < self.config["max_refocus"]:
            plan = self.service(row, FRONTIER, {**args, "mode":"refocus",
                    "review":{"point":anomaly["xy"],"viewpoints":anomaly["viewpoints"]}})
            anomaly["attempts"] += 1
            if plan["state"] == "WAYPOINT":
                anomaly["viewpoints"].extend((args["pose"],plan["goal"]))
                return self.start_leg(row, snap, plan, "refocus")
            self.event(row, "refocus_unavailable", "占用栅格找不到安全的替代观察点", xy=anomaly["xy"],
                       reason=plan["reason"])
        if abs(snap["pose"]["speed"]) > .025 or abs(snap["pose"]["yaw_rate"]) > .05:
            return
        if any(item["id"] == anomaly["id"] for item in row["history"]):
            anomaly["id"] = f"review-{len(row['history'])+1}"
        anomaly.update(status="waiting",actions=[{"id":"skip","label":"标记待复核并继续"},{"id":"retry","label":"允许再补扫"}])
        row.update(status="WAITING_OPERATOR", phase="WAITING_OPERATOR", path=[], route_curve=None, frontier=None)
        self.event(row, "intervention_requested", "自动补扫仍无法确认，等待区域处理决定",
                   xy=anomaly["xy"], attempts=anomaly["attempts"], anomaly_id=anomaly["id"])

    def publish(self, row, snap):
        if row["coverage"]["known"] <= 0:
            return self.finish(row, "BLOCKED", "BLOCKED", "地图没有有效观测")
        outcome = "PARTIAL" if row["coverage"]["unknown"] or row["avoid"] else "COMPLETE"
        publication = row.get("publish_input")
        if publication is None:
            publication = dict(map_id=row["id"], grid=row["grid"], operation_id=row["operation_id"],
                               robot_id=self.config["robot_id"], calibration_id=self.config["calibration_id"],
                               stamp_ns=snap["stamp"], state=outcome, reason="coverage_budget",
                               semantic=row["candidates"], avoid=row["avoid"])
            row.update(publish_input=publication, phase="PUBLISHING")
            self.event(row, "publishing", "冻结栅格、语义与配准证据", coverage=row["coverage"], state=outcome)
        reply = self.service(row, PUBLISH, publication)
        ref = reply["map_ref"]
        if reply.get("status")!="ok" or ref["frame"]!=publication["grid"]["frame"] or ref["id"] != row["id"]:
            raise ValueError("publication_binding_mismatch")
        row.update(map_ref=ref, grid=publication["grid"], coverage=self.coverage(publication["grid"],row["area"]))
        self.finish(row, "SUCCEEDED", "MAP_PUBLISHED", "已发布"+("部分地图，待复核区域仍保留" if outcome=="PARTIAL" else "探索地图"),
                    map_ref=ref, coverage=row["coverage"], map_status=outcome, markers=row["markers"],
                    initial_pose=row["initial_pose"], final_pose=snap["pose"], deferred=row["history"], bindings=row["bindings"])

    def nav_step(self, row, snap, recovery=None, proposed_plan=None):
        active_id = self.config.get("navigation_map_id")
        sources = [r for r in self.rows.values() if r["kind"] in ("survey","map_import")
                   and r["status"] == "SUCCEEDED" and r.get("map_ref")
                   and (active_id is None or r["map_ref"]["id"] == active_id)]
        if len(sources) != 1:
            row["destination"] = dict(state="unavailable", query=row["target"], map_ref=row.get("map_ref"), candidates=[])
            return self.finish(row, "BLOCKED", "BLOCKED", "未找到唯一的已发布导航地图，请核对站点地图配置")
        source, = sources
        if row.get("map_ref") and row["map_ref"] != source["map_ref"]:
            return self.finish(row, "BLOCKED", "BLOCKED", "地图版本发生变化，需要重新解析地点")
        if not row.get("loaded"):
            if not self.resolve_navigation_goal(row, snap, source):
                return
        if row["map_ref"]["frame"] != snap["pose"].get("frame") or row["map_ref"]["frame"] != snap["grid"]["frame"]:
            return self.finish(row,"BLOCKED","BLOCKED","现场定位或占用栅格与目标地图的坐标系不一致")
        if snap["stamp"] <= row.get("resume_after", 0):
            return
        row["grid"] = row["loaded"]
        plan = proposed_plan if proposed_plan is not None else self.navigation_plan(row, snap)
        row["retries"] = 0
        if recovery and "previous_goal" in recovery and plan.get("state") == "WAYPOINT":
            verified = recovery["goal"]
            if verified in plan.get("departure_options",[]) and verified != plan["goal"]:
                points = [plan["through_points"][0],verified,*plan["through_points"][2:]]
                plan = {**plan,"goal":verified,"through_points":points,"path":points}
                if "global_path" in plan:
                    plan["global_path"] = [*points,*plan["global_path"][len(points):]]
        if recovery and plan["state"] == "WAYPOINT" and plan["goal"] != recovery["goal"]:
            recovery.update(goal=plan["goal"],after_ns=snap["scan_stamp_ns"])
            if "previous_goal" in recovery:
                recovery["departure_options"] = plan.get("departure_options",[plan["goal"]])
            self.recovery_event(row,recovery,"dynamic_recheck","路线更新，重新核对新的通行区段",
                                result="waiting",reason="review_segment_changed")
            return
        if plan["state"]=="COMPLETE":
            if (math.dist(xy(snap["pose"]), row["goal"]) > .085 or
                    self.config.get("execution_mode") == "continuous_path" and row["sequence"] > 0
                    and not row.get("terminal_verified")):
                return self.finish(row, "RECOVERY_REQUIRED", "BLOCKED", "终点位姿与本机停靠回执尚未共同核验")
            if abs(snap["pose"]["speed"]) <= .02 and abs(snap["pose"]["yaw_rate"]) <= .05:
                self.finish(row,"SUCCEEDED","ARRIVED","到达地图中的地点入口并停稳", observed_xy=xy(snap["pose"]),
                            map_ref=row["map_ref"], goal=row["goal"], initial_pose=row["initial_pose"],
                            final_pose=snap["pose"], bindings=row["bindings"],
                            **({"destination":row["destination"]} if row.get("destination") else {}))
        elif (plan["state"]=="WAYPOINT" and row["sequence"] < self.config["max_goals"]
                and row.get("navigation_failures",0) < 3):
            row.pop("blocked_since",None)
            self.start_leg(row,snap,plan,"navigate",recovery)
        elif not recovery and time.monotonic()-row.setdefault("blocked_since",time.monotonic()) > 12:
            self.finish(row, "BLOCKED", "BLOCKED", "发布地图与当前观测之间没有可通行的局部路线")

    def navigation_plan(self, row, snap):
        return self.service(row, ROUTE, dict(published_grid=row["loaded"], live_grid=snap["grid"], pose=xy(snap["pose"]),
                            observed_prefix=self.config.get("observed_prefix",False),
                            goal=row["goal"], bounds=row["area"], footprint_radius=self.config["radius"],
                            clearance_margin_m=self.config.get("clearance_margin_m",0.),
                            departure_lookahead_m=(row.get("recovery") or {}).get("departure_lookahead_m",0.),
                            max_step=self.config["max_step"], avoid=row["avoid"],
                            execution_mode=self.config.get("execution_mode", "waypoint"),
                            rejected=[*row.get("rejected_nav", []),*(row.get("recovery") or {}).get("rejected_departures",[])][-12:]))

    def resolve_navigation_goal(self, row, snap, source):
        ref = source["map_ref"]
        publication = self.service(row, LOAD, {"map_id":ref["id"], "revision":ref["revision"]})
        if publication.get("status") != "ok" or publication.get("map_ref") != ref or publication["grid"]["frame"] != ref["frame"]:
            raise ValueError("map_load_binding_mismatch")
        row.update(map_ref=ref, grid=publication["grid"], area=source["area"], initial_pose=snap["pose"],
                   markers=source["markers"], scene_geometry=publication.get("scene_geometry"), coverage=source["coverage"])
        resolution = self.service(row, RESOLVE, {"map_ref":ref, "query":row["target"], "semantic":publication["semantic"]})
        reasons = {"unknown":"地图中没有与该名称匹配的地点","ambiguous":"该名称对应多个地点，请指定室内、室外或完整名称",
                   "unconfirmed":"匹配到的地点尚未核验", "unavailable":"地点的坐标系或停靠位尚未核验"}
        if (resolution.get("query") != row["target"] or resolution.get("map_ref") != ref or
                resolution.get("state") not in {*reasons,"resolved"} or not isinstance(resolution.get("candidates"),list)):
            raise ValueError("semantic_resolution_binding_mismatch")
        if resolution["state"] != "resolved":
            row["destination"] = resolution
            reason = reasons[resolution["state"]]
            if resolution["state"] == "ambiguous":
                reason += "：" + "、".join(m["name"] for m in resolution["candidates"][:8])
            self.finish(row, "BLOCKED", "BLOCKED", reason, destination=resolution, map_ref=ref)
            return False
        # Use only the confirmed stop from the frozen publication, even if a provider is misconfigured.
        matches = [m for m in publication["semantic"] if m.get("entity_id") == resolution.get("entity_id")]
        if len(matches) != 1 or len(resolution["candidates"]) != 1:
            raise ValueError("semantic_destination_not_in_publication")
        marker, = matches
        candidate, = resolution["candidates"]
        facts = ("entity_id","name","environment","zone","zone_label")
        if (marker["state"] != "confirmed" or candidate.get("state") != "confirmed"
                or any(marker.get(k) != candidate.get(k) or marker.get(k) != resolution.get(k) for k in facts)
                or marker.get("entrance_pose") != resolution.get("entrance_pose")):
            raise ValueError("semantic_destination_not_in_publication")
        goal = marker["entrance_pose"][:2]
        x0, y0, x1, y1 = row["area"]
        grid = Grid(publication["grid"])
        index = grid.index(goal)
        row["destination"] = resolution
        if (marker.get("frame",ref["frame"]) != ref["frame"] or snap["pose"].get("frame") != ref["frame"]
                or snap["grid"]["frame"] != ref["frame"] or index < 0
                or not (x0 <= goal[0] <= x1 and y0 <= goal[1] <= y1)
                or not clearance_mask(grid,self.config["radius"])[0][index]):
            resolution["state"] = "unavailable"
            self.finish(row, "BLOCKED", "BLOCKED", "地点停靠位未通过本机坐标系、区域或车身净距核验",
                        destination=resolution, map_ref=ref)
            return False
        row.update(loaded=publication["grid"], goal=goal, phase="NAVIGATING", avoid=publication.get("avoid",[]))
        self.event(row, "destination_resolved", "从已发布地图解析到核验过的入口", goal=goal,
                   marker_id=resolution["entity_id"], map_ref=ref, destination=resolution)
        return True

    def recovery_event(self, row, recovery, kind, message, **proof):
        evidence = {"result":"waiting","required_clearance_m":.55,**proof}
        self.event(row,kind,message,operation_id=recovery["operation_id"],task_operation_id=row["operation_id"],
                   map_ref=row["map_ref"],gateway_boot_id=recovery.get("boot"),**evidence)

    def recovery_blocked(self, row, recovery, detail):
        self.recovery_event(row,recovery,"dynamic_blocked",detail,result="blocked",
                            reason="corridor_clearance_unverified")
        self.finish(row,"BLOCKED","BLOCKED",detail,map_ref=row["map_ref"],final_pose=row["pose"],
                    operation_id=row["operation_id"])

    def stationary_review(self, row, snapshot):
        recovery = row["recovery"]
        if time.time()-recovery.get("last_check",0) < .65:
            return
        recovery["last_check"] = time.time()
        if recovery["goal"] is None:
            plan = self.navigation_plan(row, snapshot)
            if plan.get("state") == "WAYPOINT":
                recovery.update(goal=plan["goal"], after_ns=max(recovery["after_ns"], snapshot["scan_stamp_ns"]))
            return
        proposed_plan = None
        if "previous_goal" in recovery:
            proposed_plan = self.navigation_plan(row,snapshot)
            if proposed_plan.get("state") != "WAYPOINT":
                return
            fresh = proposed_plan.get("departure_options",[proposed_plan["goal"]])
            if fresh != recovery.get("departure_options"):
                recovery.update(goal=proposed_plan["goal"],departure_options=fresh,after_ns=snapshot["scan_stamp_ns"])
                return
        samples,clock = self.rover.review_window()
        # Scan references embed the native Gateway's boot identity. A different
        # observer instance cannot clear the old operation by replaying its scan.
        samples = [item for item in samples if item["dynamic"].get("scan_ref","").startswith(recovery["boot"]+"-")]
        probe = dict(goal=recovery["goal"],samples=samples,after_ns=recovery["after_ns"],clock_ns=clock)
        if "previous_goal" in recovery:
            probe["previous_goal"] = recovery["previous_goal"]
            probe["candidate_goals"] = recovery.get("departure_options",[recovery["goal"]])
        requested_at = time.monotonic()
        proof = self.service(row,CORRIDOR,probe)
        row["retries"] = 0
        recovery["proof"] = proof
        signature = [proof.get("result"),proof.get("reason"),proof.get("clear_samples")]
        if recovery.get("last_proof") != signature or time.time()-recovery.get("last_event",0)>3:
            recovery.update(last_proof=signature,last_event=time.time())
            self.recovery_event(row,recovery,"dynamic_recheck","Agent 正对照新鲜激光、车体包络和动态轨迹",**proof)
        if proof.get("result") == "clear":
            if "previous_goal" in recovery:
                if (proof.get("original_tracks_clear") is not True or proof.get("goal") not in probe["candidate_goals"]
                        or proof.get("previous_goal") != recovery["previous_goal"]
                        or not finite(proof.get("clearance_m")) or proof["clearance_m"] < .55
                        or type(proof.get("clear_samples")) is not int or proof["clear_samples"] < 5
                        or type(proof.get("clear_span_ms")) is not int or proof["clear_span_ms"] < 650
                        or type(proof.get("last_stamp_ns")) is not int or not 0 <= clock-proof["last_stamp_ns"] <= 300_000_000):
                    return
                recovery["goal"] = proof["goal"]
                recovery["proof_valid_until"] = requested_at+.30
            self.nav_step(row,snapshot,recovery,proposed_plan)
        elif recovery.get("static_replan_allowed") and proof.get("reason") == "insufficient_clearance":
            if (len(recovery.get("rejected_departures",[])) >= 4 or "previous_goal" in recovery and
                    (proof.get("last_stamp_ns") or 0)-recovery["after_ns"] < 650_000_000):
                return
            old = recovery.get("previous_goal",recovery["goal"])
            recovery["departure_lookahead_m"] = min(self.config["max_step"],math.dist(xy(snapshot["pose"]),old))
            rejected = recovery.setdefault("rejected_departures",[])
            if not any(r["goal"] == recovery["goal"] for r in rejected):
                rejected.append(dict(start=xy(snapshot["pose"]),goal=recovery["goal"]))
            plan = self.navigation_plan(row,snapshot)
            alternative = plan.get("goal") if plan.get("state") == "WAYPOINT" else None
            if isinstance(alternative,list) and len(alternative)==2 and all(finite(v) for v in alternative):
                if math.dist(alternative,recovery["goal"]) >= snapshot["grid"]["resolution"]:
                    recovery.update(previous_goal=old,goal=alternative,after_ns=snapshot["scan_stamp_ns"],
                                    departure_options=plan.get("departure_options",[alternative]))
                    self.recovery_event(row,recovery,"dynamic_recheck","现场占用图给出另一条路线，等待新观测同时复核原冲突带和新扫掠区",
                                        reason="checking_alternate_corridor",previous_goal=old,goal=alternative,
                                        candidates=recovery["departure_options"])

    @staticmethod
    def gateway_boot(event):
        identifier = event.get("event_id","")
        if not isinstance(identifier,str) or not identifier.startswith("gateway-"):
            return None
        return identifier.removeprefix("gateway-").rpartition("-")[0] or None

    def monitor_native(self, row, active, report):
        code = report.get("code")
        boot = self.gateway_boot(report)
        if boot is None:
            return
        if active.setdefault("gateway_boot",boot) != boot:
            active["boot_changed"] = True
            return
        if code not in NATIVE_REVIEW_CODES or not self.config.get("clearance_timeout_s") or active["mode"]!="navigate":
            return
        review = active.get("dynamic_review")
        if review is None and code != "navigation.dynamic_clear":
            limit = min(active["started"]+70,time.time()+self.config["clearance_timeout_s"])
            review = active["dynamic_review"] = dict(operation_id=active["id"],boot=boot,deadline=limit,
                                                     started=time.time(),last_native_stamp=0)
            row["phase"] = "RECHECKING_CLEARANCE"
            self.recovery_event(row,review,"dynamic_hold","局部运行时请求停车等待，Agent 复查原路段",
                                reason=code,clearance_m=report.get("data",{}).get("plan",{}).get("clearance_m"))
        if review is None or code not in {"navigation.dynamic_recheck","navigation.dynamic_clear"}:
            return
        plan = report.get("data",{}).get("plan",{})
        stamp = plan.get("dynamic_recheck",{}).get("scan_stamp_ns")
        if type(stamp) is not int or stamp < review["last_native_stamp"] or (
                stamp == review["last_native_stamp"] and code != "navigation.dynamic_clear"):
            return
        review["last_native_stamp"] = stamp
        try:
            proof = self.service(row,CORRIDOR,{"native":plan})
        except (OSError,PluginUnavailable,ValueError):
            review["unverified"] = True
            return
        proof["source"] = report["event_id"]
        if code=="navigation.dynamic_clear" and proof["result"]=="clear":
            row["phase"] = "NAVIGATING"
            active.pop("dynamic_review",None)
            self.recovery_event(row,review,"dynamic_resumed","Agent 核对连续净空证据，原运动操作继续",**proof)
        else:
            if code=="navigation.dynamic_clear":
                review["unverified"] = True
            self.recovery_event(row,review,"dynamic_recheck","Agent 核对当前动态跟踪与通道复查证据",**proof)

    def enter_stationary_review(self,row,active,result,snapshot):
        receipt = self.rover.action_results.get(active["id"]+"/waypoint/1")
        data = receipt.get("data",{}) if receipt else {}
        stop = data.get("stop_evidence",{})
        boot = self.gateway_boot(receipt) if receipt else None
        if (not boot or active.get("gateway_boot",boot) != boot
                or type(data.get("odom_stamp_ns")) is not int or stop.get("quiet_samples",0)<3
                or not finite_stop(stop)):
            return False
        last_scan = snapshot["scan_stamp_ns"] if snapshot else 0
        deadline = min(row.get("recovery_deadline", math.inf),
                       (active.get("dynamic_review") or {}).get("deadline", math.inf),
                       time.time() + self.config["clearance_timeout_s"])
        review = dict(operation_id=active["id"],boot=boot,deadline=deadline,
                      static_replan_allowed=(result.state=="FAILED" and receipt["code"]=="navigation.no_safe_path"
                                             and not active.get("dynamic_review") and bool(active.get("continuous"))),
                      goal=active.get("tracking_target") if active.get("continuous") else active["goal"],
                      after_ns=max(data["odom_stamp_ns"],last_scan))
        if active.get("continuous") and (not snapshot or not review["goal"] or
                not .01 <= math.dist(xy(snapshot["pose"]), review["goal"]) <= self.config["max_step"]+.01):
            review["goal"] = None
        row["recovery_deadline"] = deadline
        row.update(recovery=review,status="RUNNING",phase="RECHECKING_CLEARANCE",route_curve=None,path=[])
        self.recovery_event(row,review,"dynamic_hold","原路段已停止，Agent 复查通道后再续跑",
                            reason=receipt["code"],stop_evidence=stop)
        return True

    def start_leg(self, row, snap, plan, mode, recovery=None):
        goal = plan["goal"]
        origin = xy(snap["pose"])
        continuous = mode == "navigate" and self.config.get("execution_mode") == "continuous_path"
        points = plan.get("through_points") if continuous else None
        if continuous:
            global_points = plan.get("global_path",points)
            area = row["area"]
            prefix_mode = self.config.get("observed_prefix",False)
            if (not isinstance(points, list) or not 2 <= len(points) <= 256 or points[0] != origin
                    or not isinstance(global_points,list) or not 2<=len(global_points)<=256
                    or global_points[:len(points)] != points or global_points[-1] != row["goal"]
                    or not prefix_mode and points[-1] != row["goal"]
                    or points[1] != goal or plan.get("frame") != "odom"
                    or snap["grid"]["frame"] != "odom" or any(not isinstance(p, list) or len(p) != 2
                    or not all(finite(v) for v in p)
                    or not (area[0]<=p[0]<=area[2] and area[1]<=p[1]<=area[3])
                    or not prefix_mode and any(abs(v)>4 for v in p) for p in global_points)):
                raise ValueError("provider_continuous_path_invalid")
            grid, admitted = route_admission(row["loaded"], snap["grid"], row["area"], self.config["radius"], row["avoid"])
            if any(not 1e-7 < math.dist(a,b) <= self.config["max_step"]+1e-8 or not segment_admitted(a,b,grid,admitted)
                   for a,b in zip(points,points[1:])):
                raise ValueError("provider_continuous_path_outside_corridor")
        else:
            self.check_local_goal(origin, goal, snap, row)
        terminal = points[-1] if continuous else goal
        if recovery and "previous_goal" in recovery and (
                time.monotonic() >= recovery.get("proof_valid_until",0) or time.time() >= recovery["deadline"]):
            return
        row["sequence"] += 1
        op = f"{row['id']}/leg/{row['sequence']}"
        phase, event, detail = {
            "frontier":("EXPLORING","frontier_selected","已选出可达的未知边界观察点"),
            "refocus":("RESCAN","alternate_view","已选出不同视角的补扫点"),
            "navigate":("NAVIGATING","route_planned","已核验整条路线，机体连续跟踪并在终点停靠" if continuous
                        else "从已发布地图生成路径并校验实时净距"),
        }[mode]
        if continuous and self.config.get("observed_prefix"):
            detail = ("本次观测已覆盖余下路线，连续跟踪至目的地并核验停稳"
                      if terminal == row["goal"] else
                      "全局路线已生成，沿实时观测确认的路段行驶，到观测边界后复核后续路线")
        row.update(phase=phase, frontier=plan.get("frontier") or goal, path=points if continuous else plan.get("path",[]),
                   route_curve=plan.get("route_curve") if plan.get("frame")==snap["grid"]["frame"] else None,
                   active=dict(id=op, goal=terminal, mode=mode, estimated=row["coverage"]["known"],
                               continuous=continuous, tracking_target=goal, start_pose=origin, started=time.time()))
        if recovery:
            row.pop("recovery",None)
            row.pop("recovery_deadline",None)
        self.event(row,event,detail,goal=terminal, operation_id=op, view_yaw_rad=plan.get("view_yaw_rad"),
                   reason=plan["reason"], covered=row["coverage"]["known"],
                   **({"semantic_goal":row["goal"],"admitted_endpoint":terminal,
                       "admitted_points":len(points),"global_points":len(global_points)}
                      if continuous and self.config.get("observed_prefix") else {}),
                   **({"clearance_proof":recovery["proof"]} if recovery else {}),
                   binding=row["bindings"][ROUTE if mode=="navigate" else FRONTIER]["binding"])
        if recovery and "previous_goal" in recovery and (
                time.monotonic() >= recovery["proof_valid_until"] or time.time() >= recovery["deadline"]):
            row.update(active=None,path=[],frontier=None,recovery=recovery,recovery_deadline=recovery["deadline"],
                       phase="RECHECKING_CLEARANCE")
            self.recovery_event(row,recovery,"dynamic_recheck","派发前通行凭证已过期，等待新观测",
                                reason="proof_expired_before_dispatch",unused_operation_id=op)
            return
        reply = self.rover.dispatch_path(op,points) if continuous else self.rover.dispatch(op,goal)
        if reply.accepted and recovery:
            self.recovery_event(row,recovery,"dynamic_resumed","机体已接纳复查后的路线，沿原任务和地图续跑",
                                next_operation_id=op,**recovery["proof"])
        if reply.accepted and continuous:
            row["last_navigation_path"] = dict(frame=plan["frame"], operation_id=op, points=points)
            reference = dict(frame=plan["frame"], operation_id=op, points=global_points, map_ref=row["map_ref"])
            row["global_path"] = reference
            row.setdefault("initial_global_path",reference)
            self.save()
        if not reply.accepted:
            row.update(active=None, stagnant=row["stagnant"]+1)
            if recovery:
                recovery["after_ns"] = snap["scan_stamp_ns"]
                row.update(recovery=recovery,recovery_deadline=recovery["deadline"],phase="RECHECKING_CLEARANCE")
            self.event(row,"motion_rejected","机体拒绝本段移动", operation_id=op, reason=reply.detail)
            self.next_tick = time.monotonic()+1

    def check_local_goal(self, origin, goal, snap, row):
        x0,y0,x1,y1 = row["area"]
        if (not isinstance(goal,list) or len(goal)!=2 or not all(math.isfinite(v) and abs(v) <= 4 for v in goal)
                or not x0 <= goal[0] <= x1 or not y0 <= goal[1] <= y1
                or not .08 <= math.dist(origin,goal) <= self.config["max_step"]+.01):
            raise ValueError("provider_goal_out_of_envelope")
        grid = Grid(snap["grid"])
        safe, _ = clearance_mask(grid, self.config["radius"])
        count = math.ceil(math.dist(origin,goal)/(grid.resolution/4))
        for step in range(count+1):
            p = [a+(b-a)*step/count for a,b in zip(origin,goal)]
            index = grid.index(p)
            if index < 0 or not safe[index]:
                raise ValueError("provider_segment_not_clear_in_local_snapshot")

    def reconcile(self, row, reason="", cancel=False, observation_issue=None):
        active = row["active"]
        if active is None:
            return
        op = active["id"]
        for report in list(self.rover.motion_events):
            code, ident = report.get("code",""), report.get("event_id")
            if not ident or report.get("module")!="edge.gateway" or report["operation_id"]!=op+"/waypoint/1":
                continue
            boot = self.gateway_boot(report)
            if not boot:
                continue
            if active.setdefault("gateway_boot",boot) != boot or (
                    self.config.get("clearance_timeout_s") and active["mode"]=="navigate" and
                    row.setdefault("gateway_boot_id",boot) != boot):
                active["boot_changed"] = True
                continue
            number = ident.rpartition("-")[2]
            if not number.isdecimal() or int(number)<=active.get("last_gateway_seq",0):
                continue
            active["last_gateway_seq"] = int(number)
            if report.get("kind") == "navigation.local_plan":
                data = report.get("data")
                plan = data.get("plan") if isinstance(data, dict) else None
                target = plan.get("tracking_target_xy") if isinstance(plan, dict) else None
                if active.get("continuous") and isinstance(target, list) and len(target)==2 and all(finite(v) for v in target):
                    active["tracking_target"] = target
                    row["frontier"] = target
                if (isinstance(plan, dict) and isinstance(plan.get("tracking_mode"), str)
                        and all(type(plan.get(key)) in (int, float) and math.isfinite(plan[key])
                                for key in ("linear_mps", "angular_radps", "curvature_per_m"))):
                    active["tracking"] = dict(received=time.time(), mode=plan["tracking_mode"],
                                              speed=plan["linear_mps"], yaw_rate=plan["angular_radps"],
                                              curvature=plan["curvature_per_m"], event_id=ident)
                else:
                    active.pop("tracking", None)
            self.monitor_native(row,active,report)
            labels = {"navigation.dynamic_yield":"动态避障策略请求停车等待",
                      "navigation.dynamic_clear":"动态避障策略确认可继续",
                      "navigation.path_blocked":"机体检测到前方通行净距不足",
                      "navigation.local_avoid":"机体选择局部绕行轨迹"}
            if code in labels and code not in active.get("reported", []):
                active.setdefault("reported",[]).append(code)
                self.event(row, "obstacle", labels[code], operation_id=op, code=code, source=ident,
                           clearance_m=row.get("scan_m"))
        review = active.get("dynamic_review")
        if active.get("boot_changed"):
            cancel,reason = True,"网关实例变化，停止并核对原运动"
        elif review and (time.time()>=review["deadline"] or review.get("unverified")):
            cancel,reason = True,"复查期结束或净空回执无法核对，保留停车"
            active["clearance_expired"] = True
        elif not review and time.time()-active["started"]>(75 if active.get("continuous") else 43):
            cancel,reason = True,"本段运动超时，按原操作请求停止"
        try:
            if cancel and not active.get("cancel_requested"):
                active["cancel_requested"] = True
                proof = {"observation_issue":observation_issue} if observation_issue else {}
                self.event(row,"stop_requested",reason,operation_id=op,**proof)
                self.rover.stop(op)
            result = self.rover.status(op)
        except (ValueError,TimeoutError,OSError):
            active["query_failures"] = active.get("query_failures",0)+1
            if active["query_failures"]>=3:
                self.finish(row,"RECOVERY_REQUIRED","BLOCKED","原运动结果未知，禁止派发后续动作")
            return
        if not result.found:
            if time.time()-active["started"] > 4:
                self.finish(row, "RECOVERY_REQUIRED", "BLOCKED", "无法核对原运动号")
            return
        if result.state not in CORE_TERMINAL:
            return
        if result.state == "RECOVERY_REQUIRED":
            return self.finish(row,"RECOVERY_REQUIRED","BLOCKED","机体未核验停止，保留原操作")
        recovering_nav = self.config.get("clearance_timeout_s") and active["mode"]=="navigate"
        if active.get("boot_changed"):
            return self.finish(row,"RECOVERY_REQUIRED","BLOCKED","原动作跨网关实例，需独立对账")
        if recovering_nav and (review or active.get("clearance_expired") or result.state=="FAILED"):
            receipt = self.rover.action_results.get(op+"/waypoint/1")
            code = receipt.get("code") if receipt else None
            if receipt is None:
                if time.time()-active.setdefault("terminal_seen",time.time()) < 2:
                    return
                return self.finish(row,"RECOVERY_REQUIRED","BLOCKED","缺少网关停稳见证，保留原操作")
            if active.get("clearance_expired"):
                stop = receipt.get("data",{}).get("stop_evidence",{})
                if not finite_stop(stop) or stop.get("quiet_samples",0)<3:
                    return self.finish(row,"RECOVERY_REQUIRED","BLOCKED","无法核对持续阻塞后的停稳")
                row.update(active=None,frontier=None)
                return self.recovery_blocked(row,review,"通道在复查期内未清空，已按原动作停车")
            # A terminal result proves the native action ended. A previously
            # observed dynamic hold still needs fresh clearance, even if the
            # action exited for sensor staleness or was canceled for lost data.
            if review or code in RECOVERABLE_CODES:
                snap = self.rover.snapshot()
                if not self.enter_stationary_review(row,active,result,snap):
                    return self.finish(row,"RECOVERY_REQUIRED","BLOCKED","原路段停止证据不完整，保留原操作")
                row.update(active=None,frontier=None,retries=0)
                self.save()
                return
        row.update(active=None, frontier=None, retries=0)
        snap = self.rover.snapshot()
        if snap:
            row["resume_after"] = snap["stamp"]
        if (result.state=="SUCCEEDED" and result.verified_count==1 and len(result.evidence)==1
                and math.dist([result.evidence[0].x,result.evidence[0].y],active["goal"]) <= .12):
            row["steps"] += 1
            if active["mode"] == "frontier":
                row["visited"].append(active["goal"])
                gain = self.coverage(snap["grid"],row["area"])["known"]-active["estimated"] if snap else 0
                row["stagnant"] = 0 if gain > 0 else row["stagnant"]+1
            if active["mode"] == "navigate":
                row["navigation_failures"] = 0
                if active.get("continuous") and active["goal"] == row["goal"]:
                    row["terminal_verified"] = op
            self.event(row, "motion_verified", "机体核验本段到点和停稳", operation_id=op,
                       xy=[result.evidence[0].x,result.evidence[0].y], mode=active["mode"])
        elif active.get("cancel_requested"):
            self.finish(row, "BLOCKED", "BLOCKED", "原操作已停止，等待恢复观测")
        else:
            if active["mode"] == "frontier":
                blocked = row.setdefault("blocked_goals", [])
                if all(math.dist(p,active["goal"]) > .10 for p in blocked):
                    row["blocked_goals"] = [*blocked[-7:], active["goal"]]
            elif active["mode"] == "navigate":
                row["navigation_failures"] = row.get("navigation_failures",0)+1
                last_pose = xy(snap["pose"]) if active.get("continuous") and snap else active["start_pose"]
                row["rejected_nav"] = [*row.get("rejected_nav",[])[-11:],
                                       {"start":last_pose,"goal":active.get("tracking_target",active["goal"])}]
            row["stagnant"] += 1
            self.event(row,"obstacle","本段受阻，已停止并等待新观测", operation_id=op, reason=result.detail,
                       clearance_m=row.get("scan_m"))
            self.next_tick = time.monotonic()+1.3
