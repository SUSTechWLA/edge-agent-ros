"""Capture owner. Each registered tool executes one independently requested operation."""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
from threading import RLock, Thread
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit, parse_qs
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from calibration.board_provider import Handler, LIMIT, observation
from calibration.methods import installed, selected
from calibration.board_clip import Window, avi, cloud_context, stationary, last_jpeg
from calibration import front_placement
from edge_contracts.service_directory import PluginUnavailable, ServiceSnapshot
from edge_contracts.semantic_scene import target_scene
from calibration import view_coverage
from calibration import intrinsic_collection

SERVICES = {"frame":"calibration.synced_frame","live_preview":"calibration.live_rgb_preview","detect":"perception.target_detection",
            "camera_model":"calibration.camera_stream_model","views":"calibration.capture_view_plan",
            "epoch":"calibration.clock_epoch","capture_clock":"calibration.clock_motion",
            "fit_clock":"calibration.clock_offset_fit",
            "measure":"calibration.board_plane_measurement",
            "fit":"calibration.extrinsic_fit","verify":"calibration.extrinsic_validate",
            "camera-sample":"calibration.camera_sample","camera-fit":"calibration.camera_fit",
            "camera-verify":"calibration.camera_verify"}
TOOLS = {"status":"sensor_calibration_status","observe":"observe_target",
         "inspect":"inspect_calibration_view","plan-views":"plan_capture_views",
         "capture_clock":"record_clock_motion","fit_clock":"verify_clock_offsets",
         "record":"record_board_sample","fit":"fit_sensor_extrinsics","verify":"verify_sensor_extrinsics",
         "camera-coverage":"inspect_camera_coverage","camera-record":"record_camera_calibration_pose",
         "camera-adjust":"adjust_camera_calibration_board",
         "camera-fit":"fit_camera_calibration","camera-verify":"verify_camera_intrinsics"}
CAMERA_TOOLS = {"camera-coverage","camera-record","camera-adjust","camera-fit","camera-verify"}
PASSIVE_TOOLS = ("inspect", "status", "plan-views")
TOOL_VERSIONS = {"plan-views": ("1", "2", "3", "4", "5", "6"), "camera-record":("1","2"),
                 "camera-fit":("1","2")}
VIEW_OPTION_FIELDS = (
    "ref", "path", "board_view", "board_lateral_m", "clearance_margin_m",
    "corridor_risk", "preferred_zone", "semantic_cost", "new_rgb_cells",
    "fills_missing_side", "end_xyz_m", "end_heading_deg", "target_range_m",
    "obliquity_deg", "base_xy_yaw", "geometry_score", "intrinsic_information_gain",
)


def _compact_view_plan(result, op=None, minimum_lateral_m=None):
    if not isinstance(result, dict):
        return None
    nav = result.get("navigation_context")
    clean_nav = ({k: nav[k] for k in ("stamp_ns", "robot", "nearest_observed_m", "target") if k in nav}
                 if isinstance(nav, dict) else None)
    options = [
        {k: item[k] for k in VIEW_OPTION_FIELDS if k in item}
        for item in (result.get("view_options") or [])[:6]
        if isinstance(item, dict)
    ]
    stamp = result.get("stamp_ns") or (clean_nav.get("stamp_ns") if clean_nav else None)
    plan = dict(
        operation_id=op,
        planner_version=result.get("planner_version"),
        outcome=result.get("outcome"),
        stamp_ns=str(stamp) if stamp is not None else None,
        reachability_checked=bool(result.get("reachability_checked")),
        base_goal_available=bool(result.get("base_goal_available")),
        navigation_context=clean_nav,
        view_options=options,
        rejected={k: int(v) for k, v in (result.get("rejected") or {}).items() if type(v) is int},
    )
    for key in ("captured_lateral_left_m", "captured_lateral_right_m", "captured_rgb_cells"):
        if key in result:
            plan[key] = result[key]
    if minimum_lateral_m is not None:
        plan["minimum_lateral_each_m"] = minimum_lateral_m
    if isinstance(result.get("scene_slice"), dict):
        plan["scene_slice"] = result["scene_slice"]
    return plan


def encoded(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,sort_keys=True,separators=(",",":")).encode()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def http(url, body=None, timeout=9):
    request = Request(url,data=encoded(body) if body is not None else None,
                      headers={"Content-Type":"application/json"})
    with urlopen(request,timeout=timeout) as response:
        raw = response.read(LIMIT+1)
    if len(raw)>LIMIT:
        raise ValueError("provider_reply_too_large")
    return json.loads(raw)


def checkpoint(path,data):
    tmp = path.with_suffix(".writing")
    with tmp.open("wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


class Station:
    def __init__(self,profile,root,registry):
        self.profile, self.root, self.registry = profile, Path(root), Path(registry)
        self.root.mkdir(parents=True,exist_ok=True)
        self.method = selected(profile)
        key = self.method.spec.get("geometry_key")
        commissioning = dict(profile)
        if key and isinstance(commissioning.get(key),dict):
            commissioning[key]={k:v for k,v in commissioning[key].items() if k not in ("suggested_metrics","suggestion_source")}
        self.base_profile_sha256 = digest(encoded(commissioning))
        self.path = self.root/"sessions.json"
        self.data = json.loads(self.path.read_bytes()) if self.path.exists() else {"tasks":{},"ops":{}}
        self.lock = RLock()
        self.active = None
        self.preview = {}
        names = {"front":"前雷达","rear":"后雷达","left":"左雷达","right":"右雷达"}
        roles = self.profile["solver"]["lidar_frames"]
        self.lidar_names = "、".join(f"{names[k]}（{k}）" if k in names else k for k in roles)
        self.start_hint = f"请将标定板置于相机与{self.lidar_names}的共同视野内，并按提示变换角度。"
        if profile.get("front_placement"):
            self.start_hint=front_placement.GUIDANCE
        self.geometry = {}
        if key:
            stored = self.data.get("board_profiles",{}).get(self.base_profile_sha256)
            preset = profile.get(key,profile.get("solver",{}).get(key,{}))
            if stored:
                self.geometry = dict(board_id=stored["board_id"],metrics=self.method.metrics(stored["metrics"]))
            elif preset.get("confirmed") is True and preset.get("board_id"):
                self.geometry = dict(board_id=preset["board_id"],metrics=self.method.metrics(
                    {k:preset[k] for k in self.method.spec["metric_fields"]}))
        self.refresh_fingerprint()
        running=[op for op,row in self.data["ops"].items() if row["state"]=="RUNNING"]
        for op, row in self.data["ops"].items():
            if row["state"] == "RUNNING":
                owner=op.rpartition("/step/")[0]
                if (row.get("kind")=="inspect" and op==self.data["tasks"].get(owner,{}).get("placement_operation")
                        and self.data["tasks"][owner].get("profile")==self.fingerprint):
                    row["state"]="WAITING_RESUME"
                    continue
                if len(running)==1 and self.recover_geometry_wait(op,row):
                    continue
                row.update(state="RECOVERY_REQUIRED",detail="标定工位重启，原操作未完成。请检查归档后重新发起任务。")
                if row.get("kind") not in PASSIVE_TOOLS:
                    self.data["last_failure"] = row["detail"]
        self.save()

    def recover_geometry_wait(self, op, row):
        # Before any dimensions were available this operation could only decode image markers.
        # A capture that reached another stage needs the normal recovery/reconciliation path.
        task_id,sep,_ = op.rpartition("/step/")
        if (not sep or not re.fullmatch(r"[a-zA-Z0-9][\w.-]{0,95}",task_id,re.ASCII) or
                row.get("operation_id")!=op or row.get("kind") not in ("observe","record") or
                task_id!=self.data.get("last_calibration") or self.geometry or not self.method.spec.get("geometry_key")):
            return False
        task=self.data["tasks"].get(task_id,{})
        if (task.get("goal_tool")!="verify_sensor_extrinsics" or task.get("profile")!=self.fingerprint or
                task.get("method_sha256")!=self.method.sha256 or task.get("phase")!="WAITING_FOR_GEOMETRY" or
                task.get("capture_feedback",{}).get("condition")!="board_geometry_required" or
                task.get("poses")!=[] or task.get("candidate") or task.get("revision") or
                self.data.get("published",{}).get("task_id")==task_id):
            return False
        folder=self.root/"captures"/task_id
        if folder.exists() and any(folder.iterdir()):
            return False
        message="工位已恢复。上次等待登记板尺寸时未录入样本。请登记尺寸后续办原任务。"
        task.update(phase="PAUSED_FOR_OPERATOR",message=message)
        task["capture_feedback"].update(guidance=message,waiting_for_operator=True)
        row.update(state="SUCCEEDED",evidence=self.feedback(task,"board_geometry_required",
                   verified=False,detected=False,waiting_for_operator=True,paused_for_operator=True,
                   recovered_wait=True,target=self.method.spec["target"]))
        return True

    def save(self):
        checkpoint(self.path,encoded(self.data))

    def refresh_fingerprint(self):
        self.fingerprint = (digest(encoded([self.base_profile_sha256,self.method.sha256,self.geometry]))
                            if self.method.spec.get("geometry_key") else self.base_profile_sha256)

    def geometry_status(self):
        status = dict(profile_sha256=self.fingerprint,confirmed=bool(self.geometry),
                    required_fields=list(self.method.spec.get("metric_fields",{})),
                    board_id=self.geometry.get("board_id"),metrics=self.geometry.get("metrics",{}))
        key = self.method.spec.get("geometry_key")
        if not key or self.geometry:
            return status
        record=self.profile.get(key,{})
        path=os.getenv("EDGE_CALIBRATION_BOARD_SUGGESTIONS")
        try:
            if path:
                with Path(path).open("rb") as stream:
                    raw=stream.read(8193)
                if len(raw)>8192:
                    raise ValueError("invalid_suggestion_file_size")
                record=json.loads(raw)
                if record["robot_id"]!=self.profile["robot_id"] or record["calibration_method"]!=self.method.spec["id"]:
                    raise ValueError("suggestion_station_mismatch")
            if record.get("suggested_metrics"):
                source=record.get("suggestion_source")
                if not isinstance(source,str) or not 1<=len(source.strip())<=400:
                    raise ValueError("suggestion_source_required")
                metrics=self.method.metrics(record["suggested_metrics"],partial=True)
                status.update(suggested_metrics=metrics,suggestion_source=source.strip())
        except (OSError,ValueError,KeyError,TypeError,AttributeError):
            status["suggestion_error"]="尺寸建议文件无效或与工位不匹配，请以实物测量值为准。"
        return status

    def register_geometry(self, request):
        with self.lock:
            if not self.method.spec.get("geometry_key"):
                raise ValueError("configured_method_has_no_board_geometry_form")
            if (request.get("confirmed") is not True or not isinstance(request.get("board_id"),str)
                    or not 1 <= len(request["board_id"].strip()) <= 80
                    or any(ord(c)<32 for c in request["board_id"])):
                raise ValueError("confirmed_board_identity_required")
            geometry = dict(board_id=request["board_id"].strip(),metrics=self.method.metrics(request["metrics"]))
            if request.get("expected_profile_sha256") != self.fingerprint:
                raise ValueError("sensor_profile_changed_reload_station")
            if geometry == self.geometry:
                return self.geometry_status()
            task_id = self.data.get("last_calibration")
            task = self.data["tasks"].get(task_id,{})
            if task.get("poses") and task.get("phase") not in ("VERIFIED","FAILED"):
                if (self.active or task.get("phase")=="PAUSED_FOR_OPERATOR" or
                        self.agent_state(task_id) in ("PLANNING","RUNNING","WAITING")):
                    raise ValueError("board_profile_in_use_by_existing_samples")
            if self.geometry and self.active:
                raise ValueError("board_profile_in_use")
            self.geometry = geometry
            self.refresh_fingerprint()
            self.data.setdefault("board_profiles",{})[self.base_profile_sha256] = geometry
            if task_id and not task.get("poses"):
                task["profile"] = self.fingerprint
            self.save()
            return self.geometry_status()

    def status(self, reconcile=False):
        with self.lock:
            report = self.data.get("published",{})
            current = report.get("profile_sha256") == self.fingerprint
            last_cal = self.data.get("last_calibration")
            task = self.data["tasks"].get(last_cal,{})
            camera_task = task.get("goal_tool") == "verify_camera_intrinsics"
            if camera_task:
                report = task.get("camera_result",{})
                current = report.get("profile_sha256")==self.fingerprint
            state = "VERIFIED" if current else "NEEDS_CALIBRATION"
            msg = ("相机标定已通过独立验证。" if current and camera_task else
                   f"已验证相机与{self.lidar_names}的空间外参。" if current else
                   "当前传感器组缺少已验证的基础外参。"+self.start_hint)
            if self.data.get("last_failure"):
                ambiguous = any(row["state"]=="RECOVERY_REQUIRED" and op.startswith(str(last_cal)+"/step/")
                                for op,row in self.data["ops"].items())
                if ambiguous:
                    state, msg = "RECOVERY_REQUIRED",self.data["last_failure"]
                else:
                    msg += " 上次尝试已结束，可重新发起任务。"
            ongoing = task.get("phase") and task["phase"] not in ("VERIFIED","FAILED","OBSERVED") and not self.data.get("last_failure")
            active_cal = self.active if last_cal and self.active and self.active.rpartition("/step/")[0]==last_cal else None
            if ongoing and reconcile and not active_cal:
                ongoing = self.agent_state(last_cal) in ("PLANNING","WAITING","RUNNING")
            if active_cal or ongoing:
                state, msg = "CALIBRATING",task.get("message",self.start_hint)
            if task.get("phase")=="PAUSED_FOR_OPERATOR":
                state, msg = "PAUSED_FOR_OPERATOR",task["message"]
            feedback = task.get("capture_feedback")
            view_plan = task.get("last_view_plan")
            scene_slice = task.get("last_scene_slice") or (
                view_plan.get("scene_slice") if isinstance(view_plan, dict) else None)
            if feedback is not None or view_plan is not None or scene_slice is not None:
                preview = self.task_preview(last_cal,task) if feedback is not None else {}
                seen = preview.get("received")
                merged = dict(feedback) if isinstance(feedback, dict) else {}
                if feedback is not None:
                    merged.update(
                        preview_age_s=round(time.monotonic()-seen,3) if seen else None,
                        preview_stamp_ns=preview.get("stamp_ns"),
                        preview_source=preview.get("source"),
                    )
                if isinstance(view_plan, dict):
                    merged["view_plan"] = view_plan
                    merged.setdefault("capture_view_options", view_plan.get("view_options", []))
                if isinstance(scene_slice, dict):
                    merged.setdefault("scene_slice", scene_slice)
                cov_data, _ = self.capture_coverage(task)
                for key, val in cov_data.items():
                    merged.setdefault(key, val)
                feedback = merged
            catalog=installed()
            current_method=catalog.get(self.method.spec["id"])
            method = self.method.public()
            if camera_task:
                method = dict(method,id="camera_and_lidar" if task.get("joint_camera") else "camera_intrinsics",
                              label="D435 内参与前 MID360 外参" if task.get("joint_camera") else "D435 相机内参",
                              min_fit=intrinsic_collection.MIN_FIT,min_holdout=intrinsic_collection.MIN_HOLDOUT)
            return dict(robot_id=self.profile["robot_id"],environment=self.profile["environment"],
                        state=state,phase=task.get("phase","STARTUP"),message=msg,active_operation=active_cal,
                        task_id=last_cal,revision=report.get("revision") if current else None,
                        scope="camera_from_each_lidar",unobserved=["imu_extrinsics","base_link_extrinsics"],
                        sensor_frames=self.profile["solver"]["lidar_frames"],
                        samples=len(task.get("camera_samples" if camera_task else "poses",[])),capture_feedback=feedback,
                        camera_result=report if camera_task and current else None,
                        guidance=msg,
                        method=method,methods=[m.public() for m in catalog.values()],
                        method_available=bool(current_method and current_method.sha256==self.method.sha256),
                        board_geometry=self.geometry_status(),profile_sha256=self.fingerprint)

    def service(self,task,name,body):
        cap = SERVICES[name]
        pin = ServiceSnapshot(self.registry).pin(cap,task["bindings"].get(cap))
        task["bindings"][cap] = pin["binding"]
        if name in ("detect","measure","fit","verify","views","camera-sample","camera-fit","camera-verify"):
            if selected(self.profile).sha256 != self.method.sha256:
                raise ValueError("active_calibration_method_changed")
            if task.setdefault("method_sha256",self.method.sha256) != self.method.sha256:
                raise ValueError("task_calibration_method_changed")
            body = dict(body,method_sha256=self.method.sha256)
            if self.geometry:
                body["board_metrics"] = self.geometry["metrics"]
        result = http(pin["endpoint"],body,timeout=135 if name=="capture_clock" else
                      30 if name in ("measure","camera-fit","camera-verify") else 9)
        ServiceSnapshot(self.registry).pin(cap,pin["binding"])
        if "method_sha256" in body and selected(self.profile).sha256!=body["method_sha256"]:
            raise ValueError("active_calibration_method_changed")
        return result

    def clock_status(self):
        with self.lock:
            result=self.data.get("published_clock",{}).copy()
            active=self.data.get("last_clock_task")
            task=self.data["tasks"].get(active,{})
            spatial=self.data.get("published",{})
            current=(result.get("profile_sha256")==self.fingerprint and
                     spatial.get("profile_sha256")==self.fingerprint and
                     result.get("spatial_revision")==spatial.get("revision") and result.get("task_id")==active)
            info={"robot_id":self.profile["robot_id"],"environment":self.profile["environment"],
                  "profile_sha256":self.fingerprint,"task_id":active,"phase":task.get("phase","STARTUP"),
                  "message":task.get("message","尚无经过验证的时差窗口。")}
        if current:
            reason="clock_observer_unavailable"
            try:
                epoch=self.service({"bindings":dict(result["bindings"])},"epoch",{})
                reason=epoch.get("epoch_reason","clock_epoch_changed") if epoch.get("streams_fresh") else "streams_not_fresh"
                with self.lock:
                    if (epoch.get("streams_fresh") and epoch.get("clock_epoch")==result["clock_epoch"]
                            and active==self.data.get("last_clock_task")
                            and result["revision"]==self.data.get("published_clock",{}).get("revision")
                            and spatial["revision"]==self.data.get("published",{}).get("revision")):
                        return {**result,**info,"state":"VERIFIED"}
            except (OSError,ValueError,KeyError,PluginUnavailable):
                pass
            return {**info,"state":"STALE","stale_reason":reason,"message":"时钟源已中断、重启或绑定发生变化。"}
        return {**info,"state":"NEEDS_CALIBRATION"}

    def note(self,task,phase,message):
        with self.lock:
            task.update(phase=phase,message=message,updated=time.time())
            self.save()

    def agent_state(self,task_id):
        return http("http://agent:8090/agent/tasks?"+urlencode({"id":task_id,"limit":1})).get("state")

    def intent(self,task_id,operation,kind,args=None, *, metadata=None):
        task = http("http://agent:8090/agent/tasks?"+urlencode({"id":task_id,"limit":1}))
        step=next((s for s in task.get("steps",[]) if s.get("operation_id")==operation and
                   s["tool"]==TOOLS[kind] and s.get("version") in TOOL_VERSIONS.get(kind, ("1",))),None)
        if (task.get("state") not in ("RUNNING","WAITING") or not step or
                args is not None and step.get("args")!=args):
            raise ValueError("active_agent_intent_required")
        feedback = step.get("feedback",{})
        if metadata is not None:
            metadata["active_view_requested"] = bool(feedback.get("required_motion_tools"))
            metadata["placement_required"] = feedback.get("placement_required") is True
            metadata["joint_camera"] = bool(re.search(r"外参|extrinsic",task.get("text",""),re.I))
            move = next((row for row in reversed(task["steps"]) if row.get("physical_pin")), None)
            metadata["camera_motion_seen"] = move is not None
            if move and move.get("status")=="SUCCEEDED":
                move_evidence = move.get("evidence") or {}
                proof = move_evidence.get("verification") or {}
                if (proof.get("operation_id")==move.get("operation_id")==move["physical_pin"].get("reservation_id")
                        and move_evidence.get("verifier_id")==move["physical_pin"].get("verifier_id")
                        and proof.get("sensor_clock_id")==self.profile["clock_domain"]
                        and type(proof.get("sensor_stamp_ns")) is int and proof["sensor_stamp_ns"]>0):
                    metadata["verified_camera_stop"] = {k:proof[k] for k in ("operation_id","sensor_stamp_ns")}
        return feedback.get("goal",{}).get("tool")

    def poll(self,kind,body):
        op = body["operation_id"]
        m = re.fullmatch(r"([a-zA-Z0-9][\w.-]{0,95})/step/([1-9][0-9]?)",op,re.ASCII)
        args=body["arguments"]
        if (not m or kind not in TOOLS or body["version"] not in TOOL_VERSIONS.get(kind, ("1",))
                or (kind=="observe" and (set(args)!={"target"} or not isinstance(args["target"],str)
                                        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",args["target"])))
                or (kind!="observe" and args!={})
                or body["trace"]["task_id"] != m[1] or not body["trace"].get("parent_event_id")):
            raise ValueError("invalid_agent_operation")
        with self.lock:
            prior = self.data["ops"].get(op)
            if prior:
                if prior["kind"] != kind or prior.get("arguments",{}) != args:
                    raise ValueError("operation_conflict")
                if prior["state"]!="WAITING_RESUME":
                    return prior.copy()
        metadata = {}
        goal=self.intent(m[1],op,kind,args,metadata=metadata)
        with self.lock:
            if op in self.data["ops"] and self.data["ops"][op]["state"]!="WAITING_RESUME":
                return self.data["ops"][op].copy()
            if self.active:
                return dict(operation_id=op,state="BLOCKED",detail="标定采集站正处理其他操作")
            owner = self.data.get("latest")
            if owner and owner!=m[1] and self.agent_state(owner) in ("PLANNING","RUNNING","WAITING"):
                return dict(operation_id=op,state="BLOCKED",detail="已有任务占用当前标定工位")
            if m[1] not in self.data["tasks"]:
                replacement = selected(self.profile)
                if replacement.sha256!=self.method.sha256:
                    if self.geometry:
                        replacement.metrics(self.geometry["metrics"])
                    self.method = replacement
                    self.refresh_fingerprint()
            task = self.data["tasks"].setdefault(m[1],dict(profile=self.fingerprint,poses=[],bindings={}))
            if task["profile"] != self.fingerprint:
                raise ValueError("sensor_profile_changed")
            if task.get("goal_tool",goal)!=goal:
                raise ValueError("task_goal_changed")
            task["goal_tool"]=goal
            if goal=="verify_camera_intrinsics":
                task["joint_camera"]=metadata["joint_camera"]
                task["verified_camera_stop"]=metadata.get("verified_camera_stop")
                task["camera_motion_seen"]=metadata["camera_motion_seen"]
            placement_wait = kind=="inspect" and metadata.get("placement_required") is True
            if prior and not placement_wait:
                raise ValueError("original_placement_intent_required")
            if placement_wait:
                task["placement_operation"]=op
            if metadata.get("active_view_requested"):
                task["active_view_requested"] = True
            self.active=op
            # Inspection has an operation receipt, but does not take ownership of a capture.
            if kind not in PASSIVE_TOOLS or placement_wait:
                self.data["latest"]=m[1]
            if goal=="verify_clock_offsets" and kind not in PASSIVE_TOOLS:
                self.data["last_clock_task"]=m[1]
            if goal in ("verify_sensor_extrinsics","verify_camera_intrinsics") and (kind not in PASSIVE_TOOLS or placement_wait):
                self.data["last_calibration"]=m[1]
                self.data.pop("last_failure",None)
            row=dict(operation_id=op,kind=kind,arguments=args,state="RUNNING",parent=body["trace"]["parent_event_id"])
            self.data["ops"][op]=row
            self.save()
            Thread(target=self.run,args=(m[1],task,kind,row),daemon=True).start()
            return row.copy()

    def run(self,task_id,task,kind,row):
        try:
            outcome = self.perform(task_id,task,kind,row["operation_id"],row["arguments"])
            end = dict(state="SUCCEEDED",evidence=outcome)
        except HTTPError as e:
            reason = e.read(1024).decode(errors="replace")
            if e.code == 422 and kind in ("fit","verify"):
                self.note(task,"MORE_VIEWS","当前姿态几何或验证质量不足，请补采新的板姿。")
                end = dict(state="SUCCEEDED",evidence=self.feedback(task,"geometry_rejected",error=reason))
            else:
                end = dict(state="BLOCKED",detail="标定提供者请求失败: "+reason)
        except Exception as e:
            end=dict(state="BLOCKED",detail=str(e)[:400])
        with self.lock:
            if end["state"]!="SUCCEEDED" and kind not in PASSIVE_TOOLS:
                if self.data.get("last_calibration")==task_id:
                    self.data["last_failure"]=end["detail"]
                task.update(phase="FAILED",message=end["detail"])
            row.update(end)
            self.active=None
            self.save()

    def feedback(self,task,outcome,**extra):
        required = self.method.spec["min_fit"]+self.method.spec["min_holdout"]
        data, ready = self.capture_coverage(task)
        return dict(outcome=outcome,accepted_poses=len(task["poses"]),required_poses=required,
                    enough_distinct_poses_for_fit=len(task["poses"])>=required and ready,
                    candidate_ready=bool(task.get("candidate")),message=task.get("message",self.start_hint),**data,**extra)

    def capture_coverage(self,task):
        site = self.profile.get("capture_navigation")
        if not site or not task.get("active_view_requested"):
            return {}, True
        report = view_coverage.coverage(p["obs"] for p in task["poses"])
        limit = site["minimum_lateral_m"]
        return dict(captured_lateral_left_m=round(report["left_m"],3),captured_lateral_right_m=round(report["right_m"],3),
                    minimum_lateral_each_m=limit,captured_rgb_cells=", ".join(report["cells"])), view_coverage.ready(report,limit)

    def camera_operation(self,task_id,task,kind,op):
        if (not self.profile.get("estimate_intrinsics") or not self.geometry
                or task.get("goal_tool")!="verify_camera_intrinsics"):
            raise ValueError("camera_estimation_unavailable")
        joint = task["joint_camera"]
        samples = task.get("camera_samples",[])
        progress = intrinsic_collection.report(samples,joint,mobile=task.get("active_view_requested",False))
        if kind=="camera-coverage":
            guidance = ("正在检查已采角点、底部覆盖与姿态差异。" if task.get("active_view_requested") else
                        intrinsic_collection.HINTS[progress["next_pose"]])
            task["capture_feedback"] = dict(progress,guidance=guidance)
            self.note(task,"CAMERA_COVERAGE",task["capture_feedback"]["guidance"])
            return dict(outcome="camera_coverage_checked",camera_candidate_ready=bool(task.get("camera_candidate")),**progress)
        if kind in ("camera-record","camera-adjust"):
            mobile = task.get("active_view_requested",False)
            stopped = task.get("verified_camera_stop") if mobile else None
            if mobile and task.get("camera_motion_seen") and not stopped:
                raise ValueError("missing_stop_witness_in_capture_clock")
            assessed = task.get("camera_view_decision") or {}
            if kind=="camera-adjust" and (not mobile or not stopped or not assessed.get("camera_operator_needed")
                    or assessed["aim"]!=progress["next_pose"] or assessed["samples"]!=len(samples)
                    or assessed["motion_operation"]!=stopped["operation_id"] or time.time()-assessed["observed"]>60):
                return dict(outcome="operator_adjustment_not_supported",camera_operator_needed=False,**progress)
            return intrinsic_collection.collect(self,task_id,task,op,joint,checkpoint,digest,
                                                attended=not mobile or kind=="camera-adjust",stopped=stopped)
        if not progress["camera_fit_ready"]:
            return dict(outcome="camera_samples_incomplete",camera_candidate_ready=False,**progress)
        if kind=="camera-fit":
            self.note(task,"CAMERA_SOLVING","正在用训练姿态求解相机内参和任务要求的外参。")
            training = [row for row in samples if row["role"]=="fit"]
            task["camera_candidate"] = self.service(task,"camera-fit",dict(training=training,joint=joint))
            return dict(outcome="camera_candidate_fitted",camera_candidate_ready=True,**progress)
        candidate = task.get("camera_candidate")
        if not candidate:
            return dict(outcome="camera_candidate_missing",camera_candidate_ready=False,**progress)
        self.note(task,"VERIFYING","正在用未参与拟合的板姿与角点验证。")
        holdout = [row for row in samples if row["role"]=="holdout"]
        result = self.service(task,"camera-verify",dict(candidate=candidate,holdout=holdout))
        if result["intrinsics_verified"] is not True or joint and result.get("extrinsics_verified") is not True:
            task.pop("camera_candidate",None)
            self.note(task,"FAILED","独立样本验证未通过。原始资料已保存。")
            return dict(outcome="camera_validation_rejected",intrinsics_verified=False,
                        camera_candidate_ready=False,**progress)
        self.intent(task_id,op,kind)
        publication = dict(task_id=task_id,environment=self.profile["environment"],profile_sha256=self.fingerprint,
                           method_sha256=self.method.sha256,bindings=task["bindings"],joint=joint,camera=candidate,
                           validation=result,coverage=progress,
                           samples=[{k:s[k] for k in ("id","role","archive_sha256","operation_id","source","motion_operation")
                                     if k in s} for s in samples])
        raw = encoded(publication)
        revision = digest(raw)
        directory = self.root/"camera-revisions"
        directory.mkdir(exist_ok=True)
        checkpoint(directory/(revision+".json"),raw)
        compact = dict(revision=revision,profile_sha256=self.fingerprint,k=candidate["k"],joint=joint,
                       lens_model=candidate["lens_model"],holdout_rms_px=result["holdout_rms_px"],
                       holdout_max_px=result["holdout_max_px"],training=len(candidate["fit_ids"]),
                       holdout=len(holdout),transforms=(result.get("extrinsics") or {}).get("transforms",{}))
        task["camera_result"] = compact
        task["capture_feedback"] = dict(progress,guidance="独立样本已通过验证，可以收起标定板。")
        self.note(task,"VERIFIED",task["capture_feedback"]["guidance"])
        return dict(outcome="camera_calibration_verified",intrinsics_verified=True,
                    extrinsics_verified=result.get("extrinsics_verified"), **compact)

    def perform(self,task_id,task,kind,op,args):
        if kind in CAMERA_TOOLS:
            return self.camera_operation(task_id,task,kind,op)
        if kind in ("inspect", "plan-views"):
            if kind=="inspect" and task.get("placement_operation")==op:
                return self.wait_front_placement(task_id,task,op)
            camera = {} if kind == "plan-views" else None
            result = self.inspect_view(task, camera)
            if isinstance(result.get("scene_slice"), dict):
                with self.lock:
                    task["last_scene_slice"] = result["scene_slice"]
            if camera is not None:
                prior = [p["obs"] for p in task["poses"]]
                collection = None
                if task.get("goal_tool")=="verify_camera_intrinsics" and task.get("active_view_requested"):
                    samples = task.get("camera_samples",[])
                    prior = [{"camera":dict(row["camera_pose"], image_size_px=row["size"]),"role":row["role"]}
                             for row in samples if "camera_pose" in row]
                    progress = intrinsic_collection.report(samples,task["joint_camera"],mobile=True)
                    if progress["camera_fit_ready"]:
                        guidance = "训练与独立留出样本已采齐，可以求解并验证。"
                        task["capture_feedback"] = dict(progress,guidance=guidance)
                        self.note(task,"CAMERA_COVERAGE",guidance)
                        return dict(outcome="camera_samples_ready",camera_view_recordable=False,
                                    camera_operator_needed=False,**progress)
                    stop = task.get("verified_camera_stop")
                    collection = dict(aim=progress["next_pose"],joint=task["joint_camera"],
                                      missing_cells=progress["missing_cells"],verified_move=bool(stop),
                                      needs_pair=progress["next_pose"] in ("common","holdout") and task["joint_camera"],
                                      samples=[{name:row[name] for name in ("points","tilt_deg")} for row in samples])
                result = self.service(task,"views",dict(task_id=task_id,operation_id=op,observation=result,
                                                       camera=camera,prior=prior,collection=collection))
                min_lat = (self.profile.get("capture_navigation") or {}).get("minimum_lateral_m")
                compact_plan = _compact_view_plan(result, op=op, minimum_lateral_m=min_lat)
                with self.lock:
                    if compact_plan is not None:
                        task["last_view_plan"] = compact_plan
                    if isinstance(result.get("scene_slice"), dict):
                        task["last_scene_slice"] = result["scene_slice"]
                if collection:
                    self.note_camera_view_plan(task,result,progress,stop)
            if task["profile"] != self.fingerprint:
                raise ValueError("sensor_profile_changed_during_inspection")
            return result
        if kind in ("capture_clock","fit_clock"):
            return self.clock_operation(task_id,task,kind,op)
        if kind=="status":
            state=self.status()
            return self.feedback(task,"inventory",sensor_frames=self.profile["solver"]["lidar_frames"],
                                 calibration_target=self.method.spec["target"],method=self.method.public(),
                                 board_geometry_confirmed=self.geometry_status()["confirmed"],
                                 published_revision=state["revision"],unobserved=state["unobserved"])
        if kind in ("observe","record"):
            target = args.get("target",self.method.spec["target"])
            if task.get("goal_tool") == "verify_sensor_extrinsics" and target != self.method.spec["target"]:
                return self.feedback(task,"requested_board_does_not_match_station",calibration_target=self.method.spec["target"])
            return self.observe(task_id,task,op,kind,target)
        n,h = self.method.spec["min_fit"],self.method.spec["min_holdout"]
        groups = self.method.sample_groups(p["obs"] for p in task["poses"])
        train, holdout = groups["fit"], groups["holdout"]
        if len(train)<n or len(holdout)<h:
            return self.feedback(task,"insufficient_distinct_poses")
        coverage, ready = self.capture_coverage(task)
        if not ready:
            return self.feedback(task,"insufficient_lateral_baseline")
        if kind=="fit":
            self.note(task,"SOLVING",f"正在使用训练姿态拟合{self.lidar_names}到相机的变换。")
            task["candidate"]=self.service(task,"fit",{"observations":train})["candidate"]
            self.note(task,"CANDIDATE","外参候选已生成，等待独立姿态验收。")
            return self.feedback(task,"candidate_fitted")
        if not task.get("candidate"):
            return self.feedback(task,"no_fitted_candidate")
        self.note(task,"VERIFYING","正在用未参与拟合的独立板姿验证候选。")
        report=self.service(task,"verify",{"candidate":task["candidate"],"observations":holdout})
        if (report.get("status")!="VERIFIED" or not report.get("transforms") or
                set(report["transforms"]) != set(self.profile["solver"]["lidar_frames"])):
            task.pop("candidate",None)
            return self.feedback(task,"validation_rejected")
        self.intent(task_id,op,kind)
        publication=dict(report=report,profile_sha256=self.fingerprint,environment=self.profile["environment"],
                         task_id=task_id,training_ids=[s["id"] for s in train],validation_ids=[s["id"] for s in holdout],
                         samples=[p["sha256"] for p in task["poses"]],bindings=dict(task["bindings"]),
                         method=self.method.public(),method_sha256=self.method.sha256,board=self.geometry)
        if coverage:
            publication["coverage"] = coverage
        revision=digest(encoded(publication))
        with self.lock:
            versions=self.root/"revisions"
            versions.mkdir(exist_ok=True)
            checkpoint(versions/(revision+".json"),encoded(publication))
            task["revision"]=revision
            self.data["published"]={**publication,"revision":revision}
            self.note(task,"VERIFIED",f"已用独立板姿验收并保存{self.lidar_names}到相机的外参版本。")
        return self.feedback(task,"validation_passed",verified=True,revision=revision,
                             transforms=report["transforms"],unobserved=["imu_extrinsics","base_link_extrinsics"])

    def note_camera_view_plan(self, task, result, progress, stop):
        assessment = result.get("capture_reachability") or {}
        request_tilt = bool(result.get("camera_operator_needed") and stop and assessment.get("useful_safe_views")==0
                            and int(assessment["stamp_ns"])>stop["sensor_stamp_ns"])
        result["camera_operator_needed"] = request_tilt
        task["camera_view_decision"] = dict(camera_operator_needed=request_tilt,observed=time.time(),
                                           aim=progress["next_pose"],samples=len(task.get("camera_samples",[])),
                                           motion_operation=stop["operation_id"] if stop else None,
                                           reachability=assessment)
        if request_tilt:
            guidance = "当前机体的安全可达视点无法补齐此项。保持底盘停止，Agent 可请求调整标定板。"
        elif result.get("camera_view_recordable"):
            guidance = "当前停稳视点可增加独立观测，等待 Agent 采集。"
        elif result.get("base_goal_available"):
            guidance = "已按净空和目标可见性筛选换位点，Agent 将根据覆盖缺项选择。请保持标定板固定。"
        else:
            guidance = "当前没有已验证可达的新视点。检查取景与周界观测后再继续。"
        task["capture_feedback"] = dict(progress, guidance=guidance, capture_reachability=assessment,
                                        capture_view_options=result.get("view_options",[]))
        self.note(task,"CAMERA_VIEW_PLAN",guidance)

    def wait_front_placement(self,task_id,task,op):
        gate=self.profile.get("front_placement")
        if not gate:
            raise ValueError("front_placement_station_not_commissioned")
        self.note(task,"WAITING_FOR_FRONT_BOARD",front_placement.GUIDANCE)
        previous=None
        deadline=time.monotonic()+gate["operator_wait_s"]
        while time.monotonic()<deadline:
            if task["profile"]!=self.fingerprint:
                raise ValueError("sensor_profile_changed_during_inspection")
            self.intent(task_id,op,"inspect",{})
            observed={}
            result=self.inspect_view(task,placement_observation=observed)
            condition,sample=front_placement.placement_sample(self.profile,result,observed)
            confirmed=front_placement.same_placement(previous,sample,self.method.spec["clip"])
            result.update(front_placement_confirmed=confirmed,placement_condition=condition)
            if confirmed:
                result.update(placement_condition="confirmed",placement_frame=sample["base_frame"],
                              measured_board_center_base_m=sample["center_base_m"],
                              estimated_board_bottom_z_base_m=sample["estimated_bottom_z_base_m"],
                              normal_error_deg=sample["facing_error_deg"],
                              placement_observations=[{k:str(obs[k]) for k in ("archive_sha256","camera_stamp_ns","lidar_stamp_ns")}
                                                      for obs in (previous,sample)])
                with self.lock:
                    task["placement_receipt"]={"operation_id":op,"observations":result["placement_observations"]}
                    if isinstance(result.get("scene_slice"), dict):
                        task["last_scene_slice"] = result["scene_slice"]
                    task["capture_feedback"]=dict(condition="front_placement_confirmed",front_placement_confirmed=True,
                                                  camera={"marker_ids":result["camera_marker_ids"]},lidars=result["lidar_measurements"])
                    self.note(task,"FRONT_BOARD_CONFIRMED","已在两次独立观测中确认标定板正对车头，前雷达和相机均看全板。请保持板位固定。")
                return result
            previous=sample
            with self.lock:
                task["capture_feedback"]=dict(condition=condition,guidance=front_placement.GUIDANCE,
                      camera={"marker_ids":result.get("camera_marker_ids",[])},lidars=result.get("lidar_measurements",{}),
                      front_placement_confirmed=False)
                self.save()
            time.sleep(.18)
        self.note(task,"PAUSED_FOR_OPERATOR",front_placement.GUIDANCE+" 摆好后可继续原任务。")
        return self.feedback(task,"front_placement_pending",front_placement_confirmed=False,
                             paused_for_operator=True,waiting_for_operator=True)

    def inspect_view(self, task, camera=None, placement_observation=None):
        """Inspect one paired observation using the installed method, without accepting a pose."""
        target = self.method.spec["target"]
        roles = self.profile["solver"]["lidar_frames"]
        mode = "navigation" if camera is not None and self.profile.get("capture_navigation") else "synchronized"
        snapshot = self.service(task, "frame", {"mode":mode})
        streams = snapshot.get("streams", {})
        frame = snapshot.get("archive")
        facts = dict(target=target, scope="single_paired_frame", paired_frame=bool(frame),
                     expected_lidars=", ".join(sorted(roles)), lidar_frames=roles, streams=streams,
                     method=self.method.spec["id"], method_sha256=self.method.sha256,
                     profile_sha256=task["profile"])
        if self.profile.get("front_placement"):
            facts["front_placement_confirmed"]=False
        if not frame:
            stale = sorted(role for role, row in streams.items() if not row["fresh"])
            return dict(outcome="no_synchronized_observation", stale_streams=", ".join(stale),
                        board_view_recordable=False, **facts)
        detection = self.service(task, "detect", {"archive":frame, "target":target})
        rows = detection["detections"]
        facts.update(archive_sha256=detection["archive_sha256"], camera_stamp_ns=str(detection["stamp_ns"]),
                     camera_frame=detection["frame"], detector=detection["detector"], camera_detections=len(rows))
        if len(rows)!=1:
            return dict(outcome="board_not_visible" if not rows else "ambiguous_board_detection",
                        board_view_recordable=False, **facts)
        features = rows[0]["features"]
        if "marker_ids" in features:
            facts["camera_marker_count"] = len(features["marker_ids"])
            facts["camera_marker_ids"] = features["marker_ids"]
        measured_plane = features.get(self.method.spec["plane_feature"])
        view_plane = measured_plane or features.get("planning_plane")
        if camera is not None and view_plane and "corners_px" in view_plane:
            with observation({"archive":frame}) as (bundle, meta, identity):
                if any(detection.get(k) != v for k,v in identity.items()):
                    raise ValueError("view_camera_source_mismatch")
                height, width = bundle["rgb"].shape[:2]
                camera.update(plane=view_plane, height=height, width=width, k=bundle["k"].tolist(),
                              navigation=meta.get("navigation"))
        if not measured_plane:
            return dict(outcome=features.get("condition", "camera_board_pose_unavailable"),
                        planning_pose_available=bool(view_plane), calibration_pose_eligible=False,
                        board_view_recordable=False, **facts)
        plane = measured_plane
        measurement = self.service(task, "measure", {"archive":frame, "detections":detection,
                                                     "prior":[p["obs"] for p in task["poses"]]})
        lidars = measurement.get("lidars", {})
        if placement_observation is not None:
            placement_observation.update(measurement.get("observation") or {})
        seen = sorted(role for role, row in lidars.items() if row["condition"]=="board_observed")
        facts.update(target_center_m=plane["center_m"], target_normal=plane["normal"],
                     camera_reprojection_rms_px=plane["reprojection_rms_px"])
        facts["scene_slice"] = target_scene(target=target, frame=detection["frame"], stamp_ns=detection["stamp_ns"],
                                          archive=detection["archive_sha256"], plane=plane, detector=detection["detector"],
                                          lidar_roles=roles, seen=seen)
        return dict(outcome=measurement["condition"], camera_pose_observed=True,
                    board_view_recordable=measurement["condition"]=="distinct_pose",
                    lidar_targets_seen=", ".join(seen),
                    missing_lidar_targets=", ".join(sorted(measurement.get("missing", []))),
                    lidar_measurements=lidars, **facts)

    def clock_operation(self,task_id,task,kind,op):
        if not self.method.spec.get("clock_supported"):
            raise ValueError("当前四孔板插件支持静态空间外参。尚无适配此板的时间偏移测量插件。")
        spatial=self.data.get("published",{})
        if spatial.get("profile_sha256")!=self.fingerprint or not spatial.get("revision"):
            raise ValueError("请先完成当前装配的相机与已登记雷达的空间外参标定。")
        folder=self.root/"clock-captures"/task_id
        if kind=="capture_clock":
            self.note(task,"RECORD_CLOCK","请保持车体固定，将板在共同视野内前后不规则往返，覆盖整个录制窗口。")
            capture=self.service(task,"capture_clock",{})
            capture["spatial_revision"]=spatial["revision"]
            capture["profile_sha256"]=self.fingerprint
            self.intent(task_id,op,kind)
            raw=encoded(capture)
            sha=digest(raw)
            folder.mkdir(parents=True,exist_ok=True)
            checkpoint(folder/(sha+".json"),raw)
            with self.lock:
                task["clock_trace"]=sha
                self.note(task,"CLOCK_RECORDED","已记录各传感器独立采集时间和板面，可检验时间偏移。")
            return dict(outcome="motion_window_recorded",motion_window_ready=True,archive_sha256=sha,
                        sample_counts={k:len(v) for k,v in capture["traces"].items()})
        if not task.get("clock_trace"):
            return dict(outcome="missing_motion_window",motion_window_ready=False,
                        message="需要先录制一次具有共同观测的非周期运动窗口。")
        sha=task["clock_trace"]
        raw=(folder/(sha+".json")).read_bytes()
        capture=json.loads(raw)
        if (digest(raw)!=sha or capture["profile_sha256"]!=self.fingerprint or
                capture["spatial_revision"]!=spatial["revision"]):
            raise ValueError("clock capture or spatial revision changed")
        self.note(task,"VALIDATE_CLOCK","正在由训练段估计常量偏移，并在留出运动上独立检验。")
        report=self.service(task,"fit_clock",{"traces":capture["traces"],"transforms":spatial["report"]["transforms"]})
        if report.get("status")!="VERIFIED" or set(report["sources"])!=set(self.profile["solver"]["lidar_frames"]):
            raise ValueError("one or more clock sources failed held-out verification")
        for key,row in report["sources"].items():
            support=capture["exposure_bound_ns"]+capture["lidar_slice_bound_ns"][key]
            row["uncertainty_ns"]+=support
            row["valid_raw_ns"][0]+=support
            row["valid_raw_ns"][1]-=support
            if row["valid_raw_ns"][0]>=row["valid_raw_ns"][1]:
                raise ValueError("empty validity window after exposure and LiDAR scan margins")
        epoch=self.service(task,"epoch",{})
        if not epoch.get("streams_fresh") or epoch.get("clock_epoch")!=capture["clock_epoch"]:
            raise ValueError("clock source changed or stopped after capture")
        self.intent(task_id,op,kind)
        publication=dict(**report,robot_id=self.profile["robot_id"],environment=self.profile["environment"],
                         profile_sha256=self.fingerprint,spatial_revision=spatial["revision"],
                         clock_epoch=capture["clock_epoch"],topics=capture["topics"],task_id=task_id,
                         bindings=dict(task["bindings"]),capture_sha256=sha)
        revision=digest(encoded(publication))
        with self.lock:
            versions=self.root/"clock-revisions"
            versions.mkdir(exist_ok=True)
            checkpoint(versions/(revision+".json"),encoded(publication))
            self.data["published_clock"]={**publication,"revision":revision}
            self.note(task,"CLOCK_VERIFIED","时差已通过独立运动段检验，仅覆盖本次时钟代次和测量窗口。")
        return dict(outcome="clock_offsets_verified",verified=True,revision=revision,
                    spatial_revision=spatial["revision"],clock_epoch=capture["clock_epoch"],sources=report["sources"])

    def observe(self,task_id,task,op,kind,target):
        if (self.method.spec.get("clip") and target==self.method.spec["target"]
                and (kind=="record" or task.get("goal_tool")=="verify_sensor_extrinsics")):
            return self.capture_board(task_id,task,op,kind)
        def receipt(outcome, **facts):
            if kind=="record" or task.get("goal_tool")=="verify_sensor_extrinsics":
                return self.feedback(task,outcome,**facts)
            return dict(outcome=outcome,**facts)
        if target=="checkerboard":
            self.note(task,"WAITING_FOR_BOARD",self.start_hint if not task["poses"] else "请将板移到新的位置并改变倾角，保持静止。")
        else:
            self.note(task,"OBSERVING","正在现场相机图像中查找所请求的目标。")
        until=time.monotonic()+(180 if task.get("awaited_target")==target else 12)
        condition="no_synchronized_observation"
        check_owner=time.monotonic()+5
        while time.monotonic()<until:
            if time.monotonic()>=check_owner:
                self.intent(task_id,op,kind)
                check_owner=time.monotonic()+5
            frame=self.service(task,"frame",{"mode":"rgb" if kind=="observe" else "synchronized"}).get("archive")
            if frame is None:
                continue
            detection=self.service(task,"detect",{"archive":frame,"target":target})
            if not detection["detections"]:
                condition=("board_not_visible" if target=="checkerboard" and task.get("goal_tool")=="verify_sensor_extrinsics"
                           else "target_absent")
                time.sleep(.15)
                continue
            if kind=="observe":
                self.note(task,"BOARD_VISIBLE" if target=="checkerboard" else "OBSERVED","已从当前相机观测识别指定目标。")
                return receipt("target_visible",target=target,detected=True,frame=detection["frame"],
                               stamp_ns=detection["stamp_ns"],detections=detection["detections"],
                               detector=detection["detector"],archive_sha256=detection["archive_sha256"])
            detector=detection["detector"]
            if task.get("detector",detector)!=detector:
                raise ValueError("board_recognizer_binding_changed")
            measurement=self.service(task,"measure",{"archive":frame,"detections":detection,
                                          "prior":[p["obs"] for p in task["poses"]]})
            condition=measurement["condition"]
            if condition not in ("distinct_pose","repeated_pose"):
                feedback=dict(outcome=condition,missing_sensors=measurement.get("missing",[]),camera_visible=True)
                if task.get("capture_feedback")!=feedback:
                    with self.lock:
                        task["capture_feedback"]=feedback
                        missing="、".join(feedback["missing_sensors"]) or "激光雷达"
                        self.note(task,"ADJUST_BOARD","相机已看见标定板，"+missing+
                                  " 未测得唯一有效板面。请改变板的倾角，拉开与邻近物体的距离。")
                time.sleep(.2)
                continue
            if condition=="repeated_pose":
                if task.get("phase")!="MORE_VIEWS":
                    self.note(task,"MORE_VIEWS","这个姿态已经录制，请变换标定板的倾角和位置。")
                if task.get("awaiting_distinct") and len(task["poses"])<8:
                    time.sleep(.2)
                    continue
                task["awaiting_distinct"]=True
                break
            self.intent(task_id,op,kind)
            raw=base64.b64decode(frame,validate=True)
            folder=self.root/"captures"/task_id
            folder.mkdir(parents=True,exist_ok=True)
            sha=digest(raw)
            checkpoint(folder/(sha+".npz"),raw)
            with self.lock:
                task["detector"]=detector
                task.pop("awaiting_distinct",None)
                task.pop("capture_feedback",None)
                task["poses"].append(dict(sha256=sha,obs=measurement["observation"],operation_id=op,detector=detector))
                task.pop("candidate",None)
                self.note(task,"RECORDED",f"已自动录制 {len(task['poses'])} 个不同板姿。")
            return self.feedback(task,"new_sample_recorded",archive_sha256=sha)
        task["awaited_target"]=target
        return receipt(condition,target=target,detected=False,waiting_for_operator=True)

    def capture_board(self,task_id,task,op,kind):
        target = self.method.spec["target"]
        limits = self.method.spec["clip"]
        window = Window(limits)
        folder = self.root/"captures"/task_id
        condition = "no_synchronized_observation"
        lidars, camera, streams = {}, {}, {}
        last_adjustment = None
        hint = f"请将板的正面朝向相机，四个码和四个圆孔都放进相机与{self.lidar_names}的共同视野，托稳板边。"
        def show(code, guidance, phase="WAITING_FOR_BOARD", **extra):
            with self.lock:
                latest = self.preview.get(task_id,{})
                clip = window.progress()
                if code=="new_sample_recorded":
                    clip["active"]=False
                task["capture_feedback"] = dict(condition=code,outcome=code,guidance=guidance,
                    camera=camera,lidars=lidars,streams=streams,preview_stamp_ns=latest.get("stamp_ns"),
                    clip=clip,video_available=any(p.get("video") for p in task["poses"]),**extra)
                self.note(task,phase,guidance)
        active_view = task.get("active_view_requested") is True
        show(condition,"正在测量当前视角。" if active_view else hint)
        end = time.monotonic()+(25 if active_view else 180)
        check_owner = time.monotonic()+5
        while time.monotonic()<end:
            if time.monotonic()>=check_owner:
                self.intent(task_id,op,kind)
                check_owner=time.monotonic()+5
            synchronized = kind=="record" and bool(self.geometry)
            snapshot = self.service(task,"frame",{"mode":"synchronized" if synchronized else "rgb"})
            archive = snapshot.get("archive")
            streams = snapshot.get("streams",{})
            if not archive:
                window.clear()
                camera,lidars = {},{}
                last_adjustment = None
                condition = "no_synchronized_observation"
                show(condition,"等待时间戳匹配的传感器消息，请检查页面中的在线状态。")
                continue
            detection = self.service(task,"detect",{"archive":archive,"target":target,"preview":True})
            preview = detection.pop("preview")
            jpg = base64.b64decode(preview["jpeg"],validate=True)
            stamps = detection.pop("stamps_ns")
            with self.lock:
                self.preview = {task_id:dict(jpeg=jpg,stamp_ns=detection["stamp_ns"],received=time.monotonic(),source="live")}
            rows = detection["detections"]
            features = rows[0]["features"] if len(rows)==1 else {}
            camera = dict(marker_ids=features.get("marker_ids",[]),marker_count=len(features.get("marker_ids",[])),
                          condition=features.get("condition","board_not_visible"))
            if task.get("detector",detection["detector"])!=detection["detector"]:
                raise ValueError("board_recognizer_binding_changed")
            plane = features.get(self.method.spec["plane_feature"])
            if not self.geometry or not plane:
                window.clear()
                lidars = {}
                last_adjustment = None
                condition = "board_geometry_required" if not self.geometry else camera["condition"]
                guidance = {"board_geometry_required":"请测量并登记这块板的码边长、码中心横纵距、孔中心横纵距和孔径。登记后将自动继续。",
                            "partial_markers":"已读到部分码，请后退或调整板角度，使四个角码完整入镜，避免反光遮挡。",
                            "marker_geometry_mismatch":"已识别角码，但投影与登记尺寸不符。请核对板尺寸、相机内参与图像裁切。"}
                show(condition,guidance.get(condition,hint),"WAITING_FOR_GEOMETRY" if not self.geometry else "ADJUST_BOARD")
                time.sleep(.12)
                continue
            if kind=="observe":
                show("target_visible","四个角码已识别。请保持板面静止，等待雷达采样。","BOARD_VISIBLE")
                return self.feedback(task,"target_visible",detected=True,target=target,frame=detection["frame"],
                                     detector=detection["detector"],stamp_ns=detection["stamp_ns"],
                                     archive_sha256=detection["archive_sha256"])
            if not synchronized:
                continue
            camera["reprojection_rms_px"] = plane.get("reprojection_rms_px")
            window.add(archive,detection,jpg,preview["size"],plane,stamps)
            if not window.ready():
                if last_adjustment:
                    condition,guidance,missing = last_adjustment
                    show(condition,guidance,"ADJUST_BOARD",missing_sensors=missing)
                else:
                    condition = "hold_still"
                    show(condition,"保持相机、雷达和板静止，正在录制同一姿态的 RGB 和点云。","RECORDING")
                continue
            # Verify geometry in two disjoint scan intervals. This permits a non-repetitive LiDAR
            # to fill the rims while keeping movement and missing sensors observable.
            middle = len(window.frames)//2
            observations = []
            for interval in (window.frames[:middle],window.frames[middle:]):
                last = interval[-1]
                body = {"archive":last["archive"],"detections":last["detection"],
                        "prior":[p["obs"] for p in task["poses"]]}
                if len(interval)>1:
                    body["prior_clouds"] = cloud_context(interval[:-1])
                measured = self.service(task,"measure",body)
                condition = measured["condition"]
                lidars = measured.get("lidars",{})
                if condition!="distinct_pose":
                    break
                observations.append(measured["observation"])
            if condition=="distinct_pose" and len(observations)==2:
                first,last = observations
                if all(stationary(first["lidars"][role],last["lidars"][role],limits)
                       for role in self.profile["solver"]["lidar_frames"]):
                    self.intent(task_id,op,kind)
                    row = self.archive_clip(folder,window,observations,op,task)
                    with self.lock:
                        task["detector"]=detection["detector"]
                        task["poses"].append(row)
                        task.pop("candidate",None)
                        count = len(task["poses"])
                        required = self.method.spec["min_fit"] + self.method.spec["min_holdout"]
                        msg = (f"已保存第 {count} 个板姿与视频。请换位置和倾角，放稳后会自动录制。"
                               if count<required else f"已保存 {count} 个板姿与视频，已具备拟合和独立验收所需的数据。")
                        if active_view:
                            msg = f"本任务已归档 {count} / {required} 个独立姿态。"
                        show("new_sample_recorded",msg,"RECORDED")
                    return self.feedback(task,"new_sample_recorded",archive_sha256=row["sha256"],board_view_recordable=False)
                condition = "board_moved"
            window.clear()
            if active_view:
                break
            hints = {"repeated_pose":"此姿态已经录制。请改变板的倾角或前后位置，再保持静止。",
                     "board_moved":"两段雷达观测间板面有移动。请扶稳板边，等录完再变换姿态。",
                     "board_missing_in_lidar":"相机已看见板。请让标红的雷达也能看全四个孔，拉开与背景的距离，再保持静止。"}
            last_adjustment = (condition,hints.get(condition,hint),measured.get("missing",[]))
            show(condition,last_adjustment[1],"MORE_VIEWS" if condition=="repeated_pose" else "ADJUST_BOARD",
                 missing_sensors=last_adjustment[2])
        window.clear()
        if active_view:
            message = f"当前采集窗口已结束。测量结果为 {condition}。"
            show(condition, message, "MORE_VIEWS")
            return self.feedback(task, condition, waiting_for_operator=False, target=target,
                                 board_view_recordable=False,
                                 missing_lidar_targets=", ".join(sorted(k for k,v in lidars.items()
                                                             if v.get("condition")!="board_observed")))
        guidance="等待已暂停。此前通过检查的板姿已保存。请保持传感器装配不变，板就位后点击“续办原步骤”继续。"
        show(condition,guidance,"PAUSED_FOR_OPERATOR",waiting_for_operator=True)
        return self.feedback(task,condition,detected=False,waiting_for_operator=True,paused_for_operator=True,target=target)

    def archive_clip(self, folder, window, observations, op, task):
        folder.mkdir(parents=True,exist_ok=True)
        def store(data,suffix):
            sha = digest(data)
            checkpoint(folder/(sha+suffix),data)
            return sha
        frames = []
        for frame in window.frames:
            raw = base64.b64decode(frame["archive"],validate=True)
            if digest(raw) != frame["detection"]["archive_sha256"]:
                raise ValueError("capture_frame_digest_mismatch")
            frames.append(dict(sha256=store(raw,".npz"),stamps_ns=frame["stamps"]))
        movie = store(avi(window.frames),".avi")
        midpoint = len(frames)//2
        windows = [dict(frame_sha256=[f["sha256"] for f in group],measurement=obs)
                   for group,obs in zip((frames[:midpoint],frames[midpoint:]),observations)]
        manifest = dict(frames=frames,measurement_windows=windows,video_sha256=movie,operation_id=op,profile_sha256=self.fingerprint,
                        board=self.geometry,method_sha256=self.method.sha256,detector=task.get("detector",
                        window.frames[0]["detection"]["detector"]),sample_metrics=window.progress())
        sha = store(encoded(manifest),".json")
        return dict(sha256=sha,video=movie,obs=observations[-1],operation_id=op,detector=manifest["detector"])

    def task_preview(self,task_id,task):
        if task_id in self.preview:
            return self.preview[task_id]
        poses = task.get("poses",[])
        if not poses or not poses[-1].get("video"):
            return {}
        pose = poses[-1]
        try:
            raw = (self.root/"captures"/task_id/(pose["sha256"]+".json")).read_bytes()
            if digest(raw)!=pose["sha256"]:
                return {}
            manifest = json.loads(raw)
            if manifest["video_sha256"]!=pose["video"]:
                return {}
            jpg = last_jpeg(self.media("/capture.avi",task_id))
            restored=dict(jpeg=jpg,stamp_ns=manifest["frames"][-1]["stamps_ns"]["camera"],source="archive")
            if not self.active or self.active.rpartition("/step/")[0]==task_id:
                self.preview={task_id:restored}
            return restored
        except (OSError,ValueError,KeyError,TypeError):
            return {}

    def media(self, path, task_id, sample=None):
        with self.lock:
            if not re.fullmatch(r"[a-zA-Z0-9][\w.-]{0,95}",task_id,re.ASCII):
                raise ValueError("invalid_task_id")
            task = self.data["tasks"].get(task_id)
            if not task:
                return None
            if path=="/preview.jpg":
                return self.task_preview(task_id,task).get("jpeg")
            poses=task["poses"]
            index=len(poses)-1 if sample is None else int(sample)-1
            if not 0<=index<len(poses) or not poses[index].get("video"):
                return None
            expected=poses[index]["video"]
            raw=(self.root/"captures"/task_id/(expected+".avi")).read_bytes()
            if digest(raw)!=expected:
                raise ValueError("recorded_video_digest_mismatch")
            return raw


class SessionHandler(Handler):
    def do_POST(self):
        if self.path == "/camera-stream-model":
            try:
                n = int(self.headers.get("Content-Length",0))
                if not 0 < n <= 16384:
                    return self.send(413,{"error":"invalid_payload_size"})
                body = json.loads(self.rfile.read(n))
                station = self.server.station
                return self.send(200,camera_model_receipt(body,station))
            except (ValueError, TypeError, KeyError) as exc:
                return self.send(422,{"error":str(exc)[:200]})
            except (OSError, PluginUnavailable):
                return self.send(503,{"error":"camera_model_provider_unavailable"})
        if self.path == "/board-geometry":
            try:
                size=int(self.headers.get("Content-Length",0))
                if not 0<size<=4096:
                    return self.send(413,{"error":"invalid_geometry_request_size"})
                return self.send(200,self.server.station.register_geometry(json.loads(self.rfile.read(size))))
            except (ValueError,TypeError,KeyError) as exc:
                return self.send(422,{"error":str(exc)[:200]})
            except OSError:
                return self.send(503,{"error":"calibration_station_unavailable"})
        if self.path != "/assess-clock-pair":
            return super().do_POST()
        from calibration.time_pairing import assess_pair
        try:
            size = int(self.headers.get("Content-Length", 0))
            if not 0 < size <= 4096:
                return self.send(413, {"error": "invalid_pair_request_size"})
            request = json.loads(self.rfile.read(size))
            station = self.server.station
            decision = assess_pair(station.clock_status(), request, station.profile["max_skew_ns"])
            self.send(200, decision)
        except (TypeError, ValueError, KeyError) as exc:
            self.send(422, {"error": str(exc)[:200]})
        except OSError:
            self.send(503, {"error": "clock_provider_unavailable"})

    def do_GET(self):
        url=urlsplit(self.path)
        if self.path == "/live-frame":
            station = self.server.station
            try:
                frame = station.service({"bindings":{}}, "live_preview", {})
                if (frame.get("available") is not True or frame.get("source") != "live_camera" or
                        frame.get("robot_id") != station.profile["robot_id"] or
                        frame.get("environment") != station.profile["environment"] or
                        frame.get("frame_id") != station.profile["camera"]["frame"] or
                        frame.get("camera_id") != station.profile["camera"]["id"]):
                    raise ValueError("no live frame from the commissioned camera")
                return self.send(200,frame)
            except (OSError, ValueError, TypeError, KeyError, PluginUnavailable):
                return self.send(503,{"code":"camera_unavailable","error":"当前没有新鲜相机帧"})
        if url.path in ("/preview.jpg","/capture.avi"):
            try:
                params=parse_qs(url.query)
                raw=self.server.station.media(url.path,params.get("task_id",[""])[0],params.get("sample",[None])[0])
                if raw is None:
                    return self.send(404,{"error":"capture_not_available"})
                self.send_response(200)
                self.send_header("Content-Type","image/jpeg" if url.path=="/preview.jpg" else "video/x-msvideo")
                self.send_header("Content-Length",str(len(raw)))
                self.send_header("Cache-Control","no-store")
                self.end_headers()
                self.wfile.write(raw)
                return
            except (ValueError,TypeError,KeyError) as exc:
                return self.send(422,{"error":str(exc)[:200]})
        if self.path == "/clock-status":
            return self.send(200,self.server.station.clock_status())
        if self.path != "/status":
            return self.send(404,{"error":"unknown_endpoint"})
        try:
            self.send(200,self.server.station.status(reconcile=True))
        except OSError:
            self.send(503,{"error":"agent_status_unavailable"})


def camera_model_receipt(body, station):
    op = body["operation_id"]
    if (body.get("version") != "1" or body.get("arguments") != {}
            or not isinstance(op,str) or not re.fullmatch(r"[a-zA-Z0-9][\w.-]{0,95}/step/[1-9][0-9]?",op,re.ASCII)
            or body.get("trace",{}).get("task_id")!=op.rpartition("/step/")[0]
            or not body["trace"].get("parent_event_id")):
        raise ValueError("invalid_camera_model_operation")
    read = {"bindings":{}}
    result = station.service(read, "camera_model", {})
    camera = station.profile["camera"]
    if result.get("available") is not True:
        evidence = dict(outcome="camera_model_unavailable", available=False, independently_calibrated=False)
    else:
        if (result.get("robot_id")!=station.profile["robot_id"] or result.get("environment")!=station.profile["environment"]
                or result.get("frame_id")!=camera["frame"] or result.get("camera_id")!=camera["id"]
                or result.get("source")!="sensor_msgs/msg/CameraInfo" or result.get("info_topic")!=camera["info_topic"]
                or result.get("image_topic")!=camera["topic"] or result.get("source_geometry")!=camera.get("image_geometry","pinhole")
                or result.get("independently_calibrated") is not False):
            raise ValueError("camera_model_source_mismatch")
        age_ms = (time.time_ns()-int(result["received_unix_ns"]))//1_000_000
        if not 0<=age_ms<=1500:
            evidence = dict(outcome="camera_model_stale", available=False, independently_calibrated=False)
        else:
            evidence = dict(result,age_ms=age_ms)
    return dict(operation_id=op,state="SUCCEEDED",evidence=dict(evidence,bindings=read["bindings"]))


def main():
    profile=json.loads(Path(os.environ["EDGE_CALIBRATION_PROFILE"]).read_bytes())
    from calibration.clock_observer import validate_environment
    validate_environment(profile)
    station=Station(profile,"/var/lib/edge-calibration",Path(__file__).parent/"station/plugins/runtime")
    with ThreadingHTTPServer(("0.0.0.0",8141), SessionHandler) as server:
        server.station=station
        server.dispatch=lambda path,req:station.poll(path.lstrip("/"),req)
        server.serve_forever()


if __name__=="__main__":
    main()
