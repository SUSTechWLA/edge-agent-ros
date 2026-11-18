"""Run inside a disposable Docker image with --network none. Separate ROS publisher and health process."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import struct
import subprocess
import sys
import tempfile
import time
from urllib.request import build_opener, ProxyHandler, Request
from urllib.error import URLError, HTTPError

import rclpy
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image, Imu, LaserScan, PointCloud2, PointField
from std_msgs.msg import String

TYPES={"rgb":Image,"depth":Image,"scan":LaserScan,"points":PointCloud2,"imu":Imu,"match":String}
HTTP=build_opener(ProxyHandler({}))


def measured_matches(coherent):
    """A separate test front end matches patches in two actual byte images.

    Each patch uses a unique 5x5 neighbourhood. The most frequent translation
    defines inliers independently of the observer's quality thresholds.
    """
    import numpy as np
    rng=np.random.default_rng(276)
    source=rng.integers(0,256,(96,96),dtype=np.uint8)
    target=np.zeros_like(source)
    points=[(x,y) for x in range(8,90,16) for y in range(8,90,16)]
    for i,(x,y) in enumerate(points):
        dx,dy=(2,-2) if coherent else (i%7-3,i//7-3)
        target[y+dy-2:y+dy+3,x+dx-2:x+dx+3]=source[y-2:y+3,x-2:x+3]
    shifts=[]
    for x,y in points:
        patch=source[y-2:y+3,x-2:x+3].astype("int32")
        candidates=np.lib.stride_tricks.sliding_window_view(target[y-5:y+6,x-5:x+6],(5,5))
        error=((candidates.astype("int32")-patch)**2).sum(axis=(-1,-2))
        sy,sx=np.unravel_index(error.argmin(),error.shape)
        if error[sy,sx]==0:
            shifts.append((int(sx)-3,int(sy)-3))
    assert len(shifts)==len(points)
    return {"matches":len(shifts),"inliers":max(shifts.count(v) for v in shifts)}


MATCH_FIXTURES={}


def message(node,key,issue):
    t=node.get_clock().now().to_msg()
    if issue=="late_stamp": t.sec-=4
    if issue=="future_stamp": t.sec+=4
    frame="test_frame" if issue!="frame_mismatch" else "other_frame"
    msg=TYPES[key]()
    if key=="match":
        stats=MATCH_FIXTURES["scattered" if issue=="low_match_inliers" else "translation"]
        msg.data=json.dumps(dict(frame_id=frame,stamp_ns=t.sec*10**9+t.nanosec,**stats))
        return msg
    msg.header.stamp=t
    msg.header.frame_id=frame
    if key=="rgb":
        msg.width=msg.height=16
        msg.step=16
        msg.encoding="unrecognised" if issue=="unsupported_encoding" else "mono8"
        flat={"overexposed":255,"black_image":0,"weak_texture":100}.get(issue)
        msg.data=bytes(flat if flat is not None else [32,215][(x+y)%2] for y in range(16) for x in range(16))
    elif key=="depth":
        msg.width=8; msg.height=8; msg.step=16; msg.encoding="16UC1"
        msg.data=struct.pack("<64H",*([0 if issue=="depth_holes" else 1215]*64))
    elif key=="scan":
        msg.range_min=.1; msg.range_max=8.
        msg.ranges=[float("nan") if issue=="invalid_ranges" else float("inf") if issue=="few_returns" else 1.1]*120
    elif key=="points":
        n=8 if issue=="few_points" else 64
        msg.width=n; msg.height=1; msg.point_step=12; msg.row_step=12*n
        msg.fields=[PointField(name=k,offset=i*4,datatype=PointField.FLOAT32,count=1) for i,k in enumerate("xyz")]
        msg.data=struct.pack("<fff",float("nan") if issue=="invalid_cloud_points" else 1.,2.,3.)*n
    elif key=="imu":
        msg.angular_velocity.x=float("nan") if issue=="imu_nonfinite" else .02
        msg.linear_acceleration.z=9.8
    return msg


def run(out):
    if os.getenv("ROS_LOCALHOST_ONLY")!="1":
        raise SystemExit("use an isolated container with ROS_LOCALHOST_ONLY=1")
    MATCH_FIXTURES.update(translation=measured_matches(True),scattered=measured_matches(False))
    rclpy.init()
    node=rclpy.create_node("sensor_fault_injector")
    publishers={key:node.create_publisher(typ,"/quality_test/"+key,qos_profile_sensor_data) for key,typ in TYPES.items()}
    report={"passed":False,"cases":{}}
    with tempfile.TemporaryDirectory() as work:
        journal=Path(work)/"module-events.sqlite"
        env={**os.environ,"EDGE_HEALTH_TIMEOUT_S":".7","EDGE_HEALTH_MAX_AGE_S":"1",
             "EDGE_HEALTH_RECHECK_S":".45","EDGE_HEALTH_JOURNAL":str(journal),"EDGE_HEALTH_PORT":"18136"}
        for key in TYPES:
            env["EDGE_HEALTH_"+key.upper()+"_TOPIC"]="/quality_test/"+key
            env["EDGE_HEALTH_"+key.upper()+"_FRAME"]="test_frame"
        process=subprocess.Popen([sys.executable,"-B","/opt/edge/perception/sensor_health/provider.py"],env=env)
        try:
            def check(key,issue,predicate,label,timeout=10):
                until=time.monotonic()+timeout
                status={}
                repeat=message(node,key,None)
                while time.monotonic()<until:
                    for name,publisher in publishers.items():
                        if name==key and issue=="stream_timeout": continue
                        publisher.publish(repeat if name==key and issue=="repeated_stamp" else message(node,name,issue if name==key else None))
                    time.sleep(.14)
                    try:
                        with HTTP.open("http://127.0.0.1:18136/status",timeout=1) as response:
                            status=json.load(response)
                    except (URLError,TimeoutError):
                        if process.poll() is not None: raise RuntimeError("health process exited")
                        continue
                    if predicate(status["inputs"][key]):
                        report["cases"][label]=status["inputs"][key]
                        return status
                raise AssertionError((label,status.get("inputs",{}).get(key)))
            healthy=lambda s:s["state"] in ("observed","recovered") and s["code"]=="ok"
            initial=check("rgb",None,healthy,"initial")
            assert "lidar_match" in initial["unconfigured"]
            for key,issue in (("rgb","black_image"),("rgb","overexposed"),("rgb","weak_texture"),
                ("rgb","unsupported_encoding"),("depth","depth_holes"),("points","invalid_cloud_points"),
                ("scan","invalid_ranges"),("imu","imu_nonfinite"),("match","low_match_inliers"),
                ("rgb","frame_mismatch"),("rgb","late_stamp"),("imu","future_stamp"),("rgb","repeated_stamp"),
                ("scan","stream_timeout")):
                bad=check(key,issue,lambda s:s["state"]=="needs_attention" and s["code"]==issue,issue)
                if issue=="weak_texture":
                    assert bad["inputs"]["match"]["latest"]["metrics"]["inliers"]==MATCH_FIXTURES["translation"]["inliers"]
                if issue=="low_match_inliers":
                    assert bad["inputs"]["match"]["latest"]["metrics"]["inliers"]==MATCH_FIXTURES["scattered"]["inliers"]
                fixed=check(key,None,lambda s:s["state"]=="recovered" and s["consecutive_good"]>=3,issue+"_recovered")
                assert fixed["inputs"][key]["event_id"]!=bad["inputs"][key]["event_id"]
            for key,code in (("scan","few_returns"),("points","few_points")):
                check(key,code,lambda s:s["state"]=="observed" and s["code"]==code,code)
            with sqlite3.connect("file:"+str(journal)+"?mode=ro",uri=True) as db:
                events=[json.loads(e[0]) for e in db.execute("SELECT event FROM module_bus_events ORDER BY seq")]
            ids={e["event_id"] for e in events}
            assert all(not e.get("parent_event_id") or e["parent_event_id"] in ids for e in events)
            assert all(s["event_id"] in ids for s in report["cases"].values())
            for scope in ("导航","本机","当前","其他机器"):
                data=json.dumps({"version":"1","operation_id":"probe:"+scope,"arguments":{"scope":scope}}).encode()
                request=Request("http://127.0.0.1:18136/diagnose",data=data,headers={"Content-Type":"application/json"})
                try:
                    with HTTP.open(request,timeout=2) as response:
                        reply=json.load(response)
                    assert scope!="其他机器" and reply["operation_id"]=="probe:"+scope and reply["state"]=="SUCCEEDED"
                    assert reply["evidence"]["scope"]=="current_observation" and reply["evidence"]["summary"]
                    assert len(reply["evidence"]["observations"])==len(TYPES)
                except HTTPError as error:
                    assert scope=="其他机器" and error.code==400
            report.update(passed=True,journal_events=events,unconfigured=initial["unconfigured"],
                          patch_match_measurements=MATCH_FIXTURES,diagnosis_scopes_checked=4)
        finally:
            process.terminate()
            process.wait(timeout=6)
            node.destroy_node()
            rclpy.shutdown()
            out.parent.mkdir(parents=True,exist_ok=True)
            out.write_text(json.dumps(report,indent=2,ensure_ascii=False,allow_nan=False)+"\n",encoding="utf-8")
    print("sensor health real ROS cases:",len(report["cases"]),"; journal events:",len(report["journal_events"]))

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("--out",type=Path,required=True)
    run(p.parse_args().out)
