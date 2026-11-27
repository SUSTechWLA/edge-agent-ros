"""SDD-043/044: send one coordinate-free Agent task; record the actual cold campus run."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import ProxyHandler,Request,build_opener
import uuid

from unknown_campus_world import ASSETS,prepare_world,services as world_services,audit

ROOT=Path(__file__).resolve().parents[2]
REQUEST="探索附近的陌生校园，建立初始地图，选一个已探明的安全位置停车复查，再回到出发点"
SOURCES=["clock","rgb","map","fused_pose","world_pose","twist","mission","bus","laser_scan","imu"]
TITLES={"started":"探索工具已受理","waiting_for_observations":"等候首次有效扫描与定位","scan_ready":"第一幅在线地图就绪",
        "goal":"从当前地图选出下一段目标","accepted":"内核受理本段运动","observation_lost":"感知不再更新",
        "cancel_requested":"按原任务号请求停稳","stopped":"内核确认本段已取消","observations_restored":"已收到新的连续观测",
        "receipt_before_cancel":"复核原动作到点回执","waypoint":"本段到点取得回执","mapped":"初图已持久保存",
        "inspection_selected":"根据初图和到点回执选定复查位置","inspected":"已停稳，完成一米内地图复查",
        "home":"已回到首次观测的出发位置","worker_finished":"探索执行器已结束"}


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1",0))
        return s.getsockname()[1]


def store(path,obj):
    path.write_text(json.dumps(obj,ensure_ascii=False,indent=2,allow_nan=False)+"\n","utf8")


def recorded_sensors(directory, *, no_cameras=False):
    # Independently reread stamped measurements retained from the actual simulation.
    summary = {}
    spec = {"laser_scan":("/scan", "sensor_msgs/msg/LaserScan", "nearest_m"),
            "imu":("/imu", "sensor_msgs/msg/Imu", "angular_z_rad_s")}
    rows = [json.loads(line) for line in (directory/"timeline.jsonl").read_bytes().splitlines()]
    if no_cameras:
        forbidden={"rgb","overview","world_pose"}
        header=json.loads((directory.parents[1]/"recording.json").read_bytes())
        if forbidden.intersection(header["sources"]) or any(r["kind"] in forbidden for r in rows) or list(directory.rglob("*.png")):
            raise AssertionError("camera-free product contains an external image or ground truth")
    for kind, (topic, msg_type, metric) in spec.items():
        selected = [r for r in rows if r["kind"] == kind]
        valid = [r for r in selected if r["valid"]]
        if not valid or any(r["topic"] != topic or r["source"] != msg_type for r in selected):
            raise AssertionError(f"missing or mismatched raw sensor source: {kind}")
        if any(r["stamp_origin"] != "header" or r["header_stamp_ns"] != r["stamp_ns"]
               or r.get("clock_domain") != "gazebo_sim_ns" or not r["frame_id"] for r in valid):
            raise AssertionError(f"lost original {kind} header")
        values = [r["metrics"][metric] for r in valid]
        if len({r["stamp_ns"] for r in valid}) < 2 or not all(map(math.isfinite, values)) or min(values) == max(values):
            raise AssertionError(f"no changing measured {metric} over the mission")
        summary[kind] = dict(topic=topic,source=msg_type,frames=sorted({r["frame_id"] for r in valid}),
                             valid=len(valid),invalid=len(selected)-len(valid),metric=metric,
                             minimum=min(values),maximum=max(values))
    if no_cameras:
        return summary
    images = [r for r in rows if r["kind"] == "rgb"]
    if (any(r.get("topic") != "/commission/overview" or r.get("source") != "gz.msgs.Image" for r in images)
            or len({r.get("sha256") for r in images}) < 2):
        raise AssertionError("no changing independent overview image")
    summary["overview"] = {"topic":"/commission/overview","source":"gz.msgs.Image", "frames":len(images),
                           "distinct_images":len({r["sha256"] for r in images})}
    return summary


def continuous_map(directory,rid):
    """Replay verified native map attachments in receipt order, without camera/truth inputs."""
    from task_recorder.global_map import MapHistory
    history=MapHistory(rid)
    counts=[]
    for line in (directory/"timeline.jsonl").read_bytes().splitlines():
        event=json.loads(line)
        if event["kind"]!="map": continue
        payload=(directory/event["file"]).read_bytes()
        assert hashlib.sha256(payload).hexdigest()==event["sha256"]
        history.advance(event,json.loads(payload))
        counts.append(event["known"])
    segments=history.segments()
    assert len(segments)==1 and len(counts)>=3, (segments,len(counts))
    initial=next(i for i,count in enumerate(counts) if count>0)
    assert min(counts[initial:])>0 and max(counts)-counts[initial]>=100, counts
    return dict(revisions=len(counts),first_known=counts[initial],peak_known=max(counts),segments=segments)


def run(out,image,assets,*,no_cameras=False,continuous=False):
    if continuous and not no_cameras:
        raise ValueError("continuous control requires the camera-free fixture and separate independent witness")
    out=out.resolve()
    out.mkdir(parents=True,exist_ok=False)
    for name in ("work","factory","input","session","plugins"): (out/name).mkdir()
    token=uuid.uuid4().hex[:10]
    task="campus-"+token
    project="unknown-"+token
    robot="rover-"+token
    rid=task
    audit_rid=rid+"-witness" if no_cameras else None
    sources=[k for k in SOURCES if not no_cameras or k not in {"rgb","world_pose"}]
    port,console_port=free_port(),free_port()
    while console_port==port: console_port=free_port()
    url=f"http://127.0.0.1:{port}/recordings/{rid}"
    audit_url=f"http://127.0.0.1:{port}/recordings/{audit_rid}" if no_cameras else None
    endpoint=f"http://127.0.0.1:{console_port}"
    domain=24+int(token[:2],16)%57
    partition="commission_"+token
    opener=build_opener(ProxyHandler({}))
    def api(address,data=None):
        req=Request(address,data=json.dumps(data,ensure_ascii=False).encode() if data is not None else None,
                    headers={"Content-Type":"application/json"})
        with opener.open(req,timeout=12) as res: return json.load(res)
    def wait(fn,seconds):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            try:
                v=fn()
                if v: return v
            except (URLError,OSError): pass
            time.sleep(.18)
        raise TimeoutError("waiting for "+fn.__name__)
    markers=out/"input"/"mission-markers.jsonl"
    markers.touch()
    def mark(phase,title,**fields):
        with markers.open("a",encoding="utf8") as f:
            f.write(json.dumps(dict(phase=phase,title=title,task_id=task,wall_observed_ns=time.time_ns(),**fields),ensure_ascii=False)+"\n")
        print(phase,flush=True)
    def bind(src,dst,ro=True):
        return {"type":"bind","source":str(src),"target":dst,"read_only":ro}
    # An identified stationary fixture supplies calibration only; it contains no scene data.
    sys.path.insert(0,str(ROOT/"tests"))
    sys.path.insert(0,str(ROOT))
    from test_factory_calibration import fixture
    from calibration.factory import verify
    profile,capture=fixture(robot)
    cal=verify(profile,capture,out/"factory","fixture-"+token)
    assert cal["status"]=="VERIFIED"
    manifest=dict(id="survey_nearby",description=REQUEST,parameters={},effect="idempotent",version="1",endpoint="http://survey:8193/survey")
    store(out/"plugins"/"survey.json",manifest)
    prepare_world(out,image,assets,cameras=not no_cameras)
    env={k:v for k,v in os.environ.items() if not k.startswith(("EDGE_","COMPOSE_"))}
    env.update(COMPOSE_DISABLE_ENV_FILE="1",EDGE_IMAGE=image,EDGE_CONSOLE_PORT=str(console_port),EDGE_ROS_DOMAIN_ID=str(domain),
               EDGE_RECORDING_PORT=str(port),EDGE_RECORDING_STORAGE=str(out/"session"),EDGE_PLUGINS_DIR=str(out/"plugins"),
               EDGE_EXTRA_PLUGIN_DIR="",EDGE_PLANNER_URL="",EDGE_OLLAMA_URL="")
    items=world_services(image,domain,partition,out,assets)
    common={"GZ_PARTITION":partition,"ROS_DOMAIN_ID":str(domain),"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp"}
    items.update({k:{"environment":common.copy()} for k in ("agent","core","console","gateway")})
    items["agent"]["tmpfs"]=["/opt/edge/scene","/opt/edge/sim","/opt/edge/tests"]
    script=Path(__file__).resolve().parent
    items["survey"]=dict(image=image,pull_policy="never",init=True,working_dir="/opt/edge",
            environment={**common,"EDGE_COMMISSION_MODE":"isolated_gazebo","EDGE_ROBOT_ID":robot,"PYTHONPATH":"/opt/edge"},
            tmpfs=["/opt/edge/scene","/opt/edge/sim","/opt/edge/tests"],
            command=["python3","-B","/experiment/campus_survey_tool.py"],
            volumes=[bind(script/name,"/experiment/"+name) for name in ("campus_survey_tool.py","campus_survey_worker.py","campus_revisit.py")]
                    +[bind(out/"work","/work",False),bind(out/"factory","/factory")])
    source_args=["python3","-B","-m","task_recorder.ros_source","--url","http://task-recorder:8188",
                 "--task-id",task,"--sim","--clock-domain","gazebo_sim_ns"]
    items["rec-source"]=dict(image=image,pull_policy="never",init=True,working_dir="/opt/edge",
            environment={**common, "EDGE_RECORDING_DEPLOYMENT":"gazebo"},
            volumes=[bind(ROOT/"task_recorder","/opt/edge/task_recorder"),bind(out/"input","/input")],
            command=source_args+["--recording-id",rid,"--laser-scan","/scan","--imu","/imu","--sensor-hz","4",
                "--all-map-revisions","--markers","/input/mission-markers.jsonl"])
    if no_cameras:
        items["rec-audit"]=dict(image=image,pull_policy="never",init=True,working_dir="/opt/edge",
                environment={**common,"EDGE_RECORDING_DEPLOYMENT":"gazebo"},
                volumes=[bind(ROOT/"task_recorder","/opt/edge/task_recorder")],
                command=source_args+["--recording-id",audit_rid,"--world-pose","/ground_truth"])
    else:
        items["rec-source"]["command"].extend(["--gz-camera","/commission/overview","--world-pose","/ground_truth"])
    proof=dict(passed=False,task_id=task,recording_id=rid,image=image,project=project,text=REQUEST,
               image_id=subprocess.check_output(["docker","image","inspect","--format","{{.Id}}",image],text=True).strip(),
               calibration_revision=cal["revision"],factory="synthetic stationary fixture",submissions=0,
               no_cameras=no_cameras,continuous=continuous,independent_witness_recording=audit_rid)
    with tempfile.TemporaryDirectory(prefix=project) as temp:
        override=Path(temp)/"experiment.json"
        store(override,{"services":items})
        base=["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose/task-recorder.yaml"),
              "-f",str(override),"--profile","recording"]
        def dc(*args,check=True):
            p=subprocess.run(base+list(args),env=env,cwd=ROOT,capture_output=True,encoding="utf8",errors="replace",timeout=100)
            if check and p.returncode: raise RuntimeError(p.stderr[-2500:] or p.stdout[-1500:])
            return p.stdout.strip()
        paused=started=closed=False
        audit_started=audit_closed=False
        worker_file=out/"work"/task/"events.jsonl"
        seen=[]
        def synchronize():
            if not worker_file.exists(): return
            for line in worker_file.read_bytes().splitlines(keepends=True)[len(seen):]:
                if not line.endswith(b"\n"): return
                e=json.loads(line); seen.append(e)
                ph="worker_finished" if e["phase"]=="finished" else e["phase"]
                fields={k:v for k,v in e.items() if k not in ("phase","event_id","stamp_ns")}
                mark(ph,TITLES.get(ph,ph),source_stamp_ns=e["stamp_ns"],**fields)
        def observed(phase):
            synchronize()
            return any(e["phase"]==phase for e in seen)
        try:
            dc("up","-d","--no-build","task-recorder")
            wait(lambda:api(f"http://127.0.0.1:{port}/health"),22)
            api(f"http://127.0.0.1:{port}/recordings/start",dict(task_id=task,recording_id=rid,sources=sources))
            started=True
            if no_cameras:
                api(f"http://127.0.0.1:{port}/recordings/start",dict(task_id=task,recording_id=audit_rid,sources=["world_pose"]))
                audit_started=True
            dc("up","-d","--no-build","rec-source","survey","core","gateway","agent","console","bridge","witness","sim",
               *(["rec-audit"] if no_cameras else []))
            def observed_sources():
                counts=api(url)["counts"]
                truth=api(audit_url)["counts"] if no_cameras else counts
                return (no_cameras or counts.get("rgb",0)>=3) and truth.get("world_pose",0)>1 and all(counts.get(k,0)>1 for k in ("laser_scan","imu"))
            wait(observed_sources,85)
            mark("cold_start","首次进入校园，地图为空",detail="任务端没有预置校园地图或具名路线；感知服务尚未接入")
            cap=api(endpoint+"/agent/capabilities")
            assert "survey_nearby" in {c["id"] for c in cap["capabilities"]}
            assert "navigate_place" not in {c["id"] for c in cap["capabilities"]}
            admitted=api(endpoint+"/agent/tasks",{"id":task,"text":REQUEST})
            assert admitted["id"]==task
            proof["submissions"]+=1
            mark("submitted","收到没有坐标的中文任务",text=REQUEST)
            wait(lambda:observed("waiting_for_observations"),30)
            time.sleep(3)
            assert not any(e["phase"]=="goal" for e in seen) and "map" not in api(url)["counts"]
            mark("perception_enabled","接入车载感知",detail="测试程序解除启动延迟，机器人仍需取得新鲜观测")
            dc("up","-d","--no-build","perception")
            wait(lambda:observed("accepted"),90)
            if not continuous:
                dc("pause","perception"); paused=True
                mark("fault_injected","行驶中切断融合位姿与地图更新",detail="仿真、原始传感器和底盘独立运行")
                wait(lambda:observed("observation_lost"),15)
                wait(lambda:observed("stopped") or observed("receipt_before_cancel"),18)
                time.sleep(2)
                mark("fault_released","解除感知中断",detail="解除测试故障，不向机器人提供补救路点")
                dc("unpause","perception"); paused=False
            def terminal():
                synchronize()
                row=api(endpoint+"/agent/tasks?id="+task)
                return row if row["state"] in ("SUCCEEDED","BLOCKED","FAILED","RECOVERY_REQUIRED","CANCELED") else None
            row=wait(terminal,245)
            store(out/"task.json",row)
            synchronize()
            mark("finished","任务通过验收" if row["state"]=="SUCCEEDED" else "任务终止",
                 state=row["state"],detail=row["detail"])
            wait(lambda:api(url)["counts"].get("mission",0)>=sum(1 for _ in markers.open(encoding="utf8")),15)
            time.sleep(1.6)
            receipt=api(url+"/finish",dict(task_id=task,status=row["state"],reason="Agent terminal receipt"))
            closed=True
            if no_cameras:
                api(audit_url+"/finish",dict(task_id=task,status=row["state"],reason="independent evaluator terminal receipt"))
                audit_closed=True
            assert row["state"]=="SUCCEEDED",row["detail"]
            assert [s["tool"] for s in row["steps"]]==["survey_nearby"]
            result=json.loads((out/"work"/task/"report.json").read_bytes())
            assert result["survey_arrivals"]>=2 and result["saved_known_gain"]>=100 and result["home_error_m"]<=.13
            assert observed("inspection_selected") and observed("inspected") and result["home_arrivals"]>=1
            inspection=json.loads((out/"work"/task/result["inspection_file"]).read_bytes())
            assert inspection["counts"]["free"]>0 and inspection["initial_grid_sha256"]==result["map_sha256"]["occupancy.json"]
            assert not receipt.get("missing_sources") and all(receipt["counts"].get(source,0)>0 for source in sources)
            if continuous:
                assert result["recovered_interruptions"]==0 and not observed("observation_lost")
                proof["continuous_map"]=continuous_map(out/"session"/rid/"media"/"mission",rid)
            else:
                assert result["recovered_interruptions"]>=1 and observed("observations_restored")
            for name,h in result["map_sha256"].items():
                assert hashlib.sha256((out/"work"/task/"map"/name).read_bytes()).hexdigest()==h
            proof.update(terminal=row["state"],operation=row["steps"][0]["operation_id"],recording=receipt,
                         known_gain=result["saved_known_gain"],home_error_m=result["home_error_m"],actual_steps=len(result["steps"]))
            audit(out,rid,audit_rid,continuous=continuous)
            proof["measured_sources"] = recorded_sensors(out/"session"/rid/"media"/"mission",no_cameras=no_cameras)
            proof["passed"]=True
        except Exception as exc:
            proof["error"]=str(exc)
            (out/"service.log").write_text(dc("logs","--no-color","--tail","30",check=False),"utf8")
            raise
        finally:
            if paused: dc("unpause","perception",check=False)
            if started and not closed:
                try: api(url+"/finish",{"task_id":task,"status":"INTERRUPTED","reason":"experiment interrupted"})
                except (URLError,OSError): pass
            if audit_started and not audit_closed:
                try: api(audit_url+"/finish",{"task_id":task,"status":"INTERRUPTED","reason":"experiment interrupted"})
                except (URLError,OSError): pass
            dc("down","--volumes","--remove-orphans","--timeout","4",check=False)
            store(out/"acceptance.json",proof)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--out",type=Path,required=True)
    cli.add_argument("--image",default="edge-agent-ros:commission-0977a54")
    cli.add_argument("--assets",type=Path,default=ASSETS)
    cli.add_argument("--no-cameras",action="store_true",help="remove camera sensors and record separate audit-only ground truth")
    cli.add_argument("--continuous",action="store_true",help="camera-free control without fault injection, requiring one uninterrupted map segment")
    args=cli.parse_args()
    run(args.out,args.image,args.assets.resolve(),no_cameras=args.no_cameras,continuous=args.continuous)
