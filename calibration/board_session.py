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
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from http.server import ThreadingHTTPServer

from calibration.board_provider import Handler, LIMIT
from edge_contracts.service_directory import PluginUnavailable, ServiceSnapshot

SERVICES = {"frame":"calibration.synced_frame","detect":"perception.target_detection",
            "epoch":"calibration.clock_epoch","capture_clock":"calibration.clock_motion",
            "fit_clock":"calibration.clock_offset_fit",
            "measure":"calibration.board_plane_measurement",
            "fit":"calibration.extrinsic_fit","verify":"calibration.extrinsic_validate"}
TOOLS = {"status":"sensor_calibration_status","observe":"observe_target",
         "capture_clock":"record_clock_motion","fit_clock":"verify_clock_offsets",
         "record":"record_board_sample","fit":"fit_sensor_extrinsics","verify":"verify_sensor_extrinsics"}
START = "当前传感器组缺少已验证的基础外参。请将标定板置于相机和两台雷达的共同视野内，并按提示变换角度。"


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
        self.fingerprint = digest(encoded(profile))
        self.path = self.root/"sessions.json"
        self.data = json.loads(self.path.read_bytes()) if self.path.exists() else {"tasks":{},"ops":{}}
        self.lock = RLock()
        self.active = None
        for op, row in self.data["ops"].items():
            if row["state"] == "RUNNING":
                row.update(state="RECOVERY_REQUIRED",detail="标定工位重启，原操作未完成。请检查归档后重新发起任务。")
                self.data["last_failure"] = row["detail"]
        self.save()

    def save(self):
        checkpoint(self.path,encoded(self.data))

    def status(self, reconcile=False):
        with self.lock:
            report = self.data.get("published",{})
            current = report.get("profile_sha256") == self.fingerprint
            last_cal = self.data.get("last_calibration")
            task = self.data["tasks"].get(last_cal,{})
            state = "VERIFIED" if current else "NEEDS_CALIBRATION"
            msg = "已验证相机与两台雷达之间的基础外参。" if current else START
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
                state, msg = "CALIBRATING",task.get("message",START)
            return dict(robot_id=self.profile["robot_id"],environment=self.profile["environment"],
                        state=state,phase=task.get("phase","STARTUP"),message=msg,active_operation=active_cal,
                        task_id=last_cal,revision=report.get("revision") if current else None,
                        scope="camera_from_each_lidar",unobserved=["imu_extrinsics","base_link_extrinsics"],
                        samples=len(task.get("poses",[])),capture_feedback=task.get("capture_feedback"),
                        profile_sha256=self.fingerprint)

    def service(self,task,name,body):
        cap = SERVICES[name]
        pin = ServiceSnapshot(self.registry).pin(cap,task["bindings"].get(cap))
        task["bindings"][cap] = pin["binding"]
        result = http(pin["endpoint"],body,timeout=135 if name=="capture_clock" else 9)
        ServiceSnapshot(self.registry).pin(cap,pin["binding"])
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

    def intent(self,task_id,operation,kind,args=None):
        task = http("http://agent:8090/agent/tasks?"+urlencode({"id":task_id,"limit":1}))
        step=next((s for s in task.get("steps",[]) if s.get("operation_id")==operation and
                   s["tool"]==TOOLS[kind] and s.get("version")=="1"),None)
        if (task.get("state") not in ("RUNNING","WAITING") or not step or
                args is not None and step.get("args")!=args):
            raise ValueError("active_agent_intent_required")
        return step.get("feedback",{}).get("goal",{}).get("tool")

    def poll(self,kind,body):
        op = body["operation_id"]
        m = re.fullmatch(r"([a-zA-Z0-9][\w.-]{0,95})/step/([1-9][0-9]?)",op,re.ASCII)
        args=body["arguments"]
        if (not m or kind not in TOOLS or body["version"]!="1"
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
                return prior.copy()
        goal=self.intent(m[1],op,kind,args)
        with self.lock:
            if op in self.data["ops"]:
                return self.data["ops"][op].copy()
            if self.active:
                return dict(operation_id=op,state="BLOCKED",detail="标定采集站正处理其他操作")
            owner = self.data.get("latest")
            if owner and owner!=m[1] and self.agent_state(owner) in ("PLANNING","RUNNING","WAITING"):
                return dict(operation_id=op,state="BLOCKED",detail="已有任务占用当前标定工位")
            task = self.data["tasks"].setdefault(m[1],dict(profile=self.fingerprint,poses=[],bindings={}))
            if task["profile"] != self.fingerprint:
                raise ValueError("sensor_profile_changed")
            if task.get("goal_tool",goal)!=goal:
                raise ValueError("task_goal_changed")
            task["goal_tool"]=goal
            self.active=op
            self.data["latest"]=m[1]
            if goal=="verify_clock_offsets":
                self.data["last_clock_task"]=m[1]
            if goal=="verify_sensor_extrinsics":
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
            if end["state"]!="SUCCEEDED":
                if self.data.get("last_calibration")==task_id:
                    self.data["last_failure"]=end["detail"]
                task.update(phase="FAILED",message=end["detail"])
            row.update(end)
            self.active=None
            self.save()

    def feedback(self,task,outcome,**extra):
        return dict(outcome=outcome,accepted_poses=len(task["poses"]),required_poses=8,
                    enough_distinct_poses_for_fit=len(task["poses"])>=8,
                    candidate_ready=bool(task.get("candidate")),message=task.get("message",START),**extra)

    def perform(self,task_id,task,kind,op,args):
        if kind in ("capture_clock","fit_clock"):
            return self.clock_operation(task_id,task,kind,op)
        if kind=="status":
            state=self.status()
            return self.feedback(task,"inventory",sensor_frames=self.profile["solver"]["lidar_frames"],
                                 published_revision=state["revision"],unobserved=state["unobserved"])
        if kind in ("observe","record"):
            return self.observe(task_id,task,op,kind,args.get("target","checkerboard"))
        train=[p["obs"] for i,p in enumerate(task["poses"]) if i<6 or i>=8 and i%2==0]
        holdout=[p["obs"] for i,p in enumerate(task["poses"]) if 6<=i<8 or i>=8 and i%2==1]
        if len(train)<6 or len(holdout)<2:
            return self.feedback(task,"insufficient_distinct_poses")
        if kind=="fit":
            self.note(task,"SOLVING","正在使用训练姿态拟合双雷达到相机的变换。")
            task["candidate"]=self.service(task,"fit",{"observations":train})["candidate"]
            self.note(task,"CANDIDATE","外参候选已生成，等待独立姿态验收。")
            return self.feedback(task,"candidate_fitted")
        if not task.get("candidate"):
            return self.feedback(task,"no_fitted_candidate")
        self.note(task,"VERIFYING","正在用未参与拟合的独立板姿验证候选。")
        report=self.service(task,"verify",{"candidate":task["candidate"],"observations":holdout})
        if report.get("status")!="VERIFIED" or not report.get("transforms"):
            return self.feedback(task,"validation_rejected")
        self.intent(task_id,op,kind)
        publication=dict(report=report,profile_sha256=self.fingerprint,environment=self.profile["environment"],
                         task_id=task_id,training_ids=[s["id"] for s in train],validation_ids=[s["id"] for s in holdout],
                         samples=[p["sha256"] for p in task["poses"]],bindings=dict(task["bindings"]))
        revision=digest(encoded(publication))
        with self.lock:
            versions=self.root/"revisions"
            versions.mkdir(exist_ok=True)
            checkpoint(versions/(revision+".json"),encoded(publication))
            task["revision"]=revision
            self.data["published"]={**publication,"revision":revision}
            self.note(task,"VERIFIED","已用独立板姿验收并保存双雷达到相机的外参版本。")
        return self.feedback(task,"validation_passed",verified=True,revision=revision,
                             transforms=report["transforms"],unobserved=["imu_extrinsics","base_link_extrinsics"])

    def clock_operation(self,task_id,task,kind,op):
        spatial=self.data.get("published",{})
        if spatial.get("profile_sha256")!=self.fingerprint or not spatial.get("revision"):
            raise ValueError("请先完成当前装配的相机和双雷达空间外参标定。")
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
        def receipt(outcome, **facts):
            if kind=="record" or task.get("goal_tool")=="verify_sensor_extrinsics":
                return self.feedback(task,outcome,**facts)
            return dict(outcome=outcome,**facts)
        if target=="checkerboard":
            self.note(task,"WAITING_FOR_BOARD",START if not task["poses"] else "请将板移到新的位置并改变倾角，保持静止。")
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


class SessionHandler(Handler):
    def do_POST(self):
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
        if self.path == "/clock-status":
            return self.send(200,self.server.station.clock_status())
        if self.path != "/status":
            return self.send(404,{"error":"unknown_endpoint"})
        try:
            self.send(200,self.server.station.status(reconcile=True))
        except OSError:
            self.send(503,{"error":"agent_status_unavailable"})


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
