"""SDD-030. Submit tasks to the normal API and independently read the simulated robot sensors."""
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
import signal
import subprocess
import sys
import time
from urllib.parse import urlencode
import uuid


def native_sample(topic, message_type):
    """Let Gazebo own its reception threads until its one-sample native process exits."""
    from google.protobuf import json_format

    with subprocess.Popen(["gz", "topic", "-e", "-n", "1", "-t", topic, "--json-output"],
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          start_new_session=True) as child:
        try:
            payload, diagnostics = child.communicate(timeout=18)
        except subprocess.TimeoutExpired as error:
            # The gz launcher can have a native CLI child. Reap the entire probe on timeout.
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.communicate()
            raise TimeoutError(f"missing Gazebo sample on {topic}") from error
        if child.returncode:
            raise RuntimeError(f"Gazebo probe on {topic} exited {child.returncode}: {diagnostics[-500:]}")
    # A busy publisher can deliver a second complete frame before the native CLI exits.
    # Decode its line-delimited records separately; merging them would combine timestamps.
    samples = [json_format.Parse(line, message_type()) for line in payload.splitlines() if line.strip()]
    if not samples:
        raise ValueError(f"Gazebo probe exited without a sample on {topic}")
    msg = samples[0]
    stamp = msg.header.stamp
    if not msg.header.HasField("stamp") or stamp.sec < 0 or not 0 <= stamp.nsec < 10**9 or not (stamp.sec or stamp.nsec):
        raise ValueError(f"missing or invalid simulator stamp on {topic}")
    return msg


def witness(robot):
    from gz.msgs10.imu_pb2 import IMU
    from gz.msgs10.model_pb2 import Model
    from gz.msgs10.pose_v_pb2 import Pose_V

    if robot not in ("crazyflie", "go1", "g1"):
        raise ValueError(f"unexpected witness robot: {robot}")
    body_topic, body_type = (("/world/edge_lab/dynamic_pose/info", Pose_V) if robot == "crazyflie" else
                             (f"/model/{robot}/joint_state", Model))
    with ThreadPoolExecutor(max_workers=2) as readers:
        imu_job = readers.submit(native_sample, f"/model/{robot}/imu", IMU)
        body_job = readers.submit(native_sample, body_topic, body_type)
        imu, body = imu_job.result(), body_job.result()
    if not imu.HasField("linear_acceleration"):
        raise ValueError("Gazebo IMU has no acceleration measurement")
    accel = imu.linear_acceleration
    if robot == "crazyflie":
        matches = [pose for pose in body.pose if pose.name == robot and pose.HasField("position")]
        if len(matches) != 1:
            raise ValueError(f"expected one measured {robot} position")
        values = {"altitude":matches[0].position.z}
    else:
        names = {"left_shoulder_pitch_joint"} if robot == "g1" else {leg+"_thigh_joint" for leg in ("FL","FR","RL","RR")}
        joints = [joint for joint in body.joint if joint.name in names and joint.HasField("axis1")]
        values = {j.name:j.axis1.position for j in joints}
        if len(joints) != len(names) or values.keys() != names:
            raise ValueError(f"incomplete or duplicate measured joints for {robot}")
    def stamped(message, value):
        stamp = message.header.stamp
        return {"stamp_s":stamp.sec + stamp.nsec*1e-9, "value":value}
    print(json.dumps({"imu":stamped(imu,[accel.x,accel.y,accel.z]), "measurement":stamped(body,values)},allow_nan=False))


def verify_forms(api,dc,report):
    tasks=report.setdefault("tasks",[])
    token=uuid.uuid4().hex[:10]
    def run(text):
        key="forms-"+token+"-"+str(len(tasks))
        api("/agent/tasks",{"id":key,"text":text})
        deadline=time.monotonic()+95
        while time.monotonic()<deadline:
            task=api("/agent/tasks?"+urlencode(dict(id=key)))
            if task.get("state") in {"SUCCEEDED","BLOCKED","FAILED","RECOVERY_REQUIRED","CANCELED"}:
                return task
            time.sleep(.3)
        raise TimeoutError(text)
    for robot,text in (("crazyflie","无人机起飞"),("crazyflie","无人机降落"),
                       ("go1","四足机器人屈腿"),("go1","四足机器人伸腿"),
                       ("g1","双足机器人举起左臂"),("g1","双足机器人放下左臂")):
        task=run(text)
        if task["state"]!="SUCCEEDED":
            report["failed"]={k:task.get(k) for k in ("id","text","state","trace_id","detail","steps")}
            raise AssertionError(f"{text}: {task.get('state')} {task.get('detail')}")
        step=task["steps"][0]
        proof=step["evidence"]
        assert step["tool"]=="delegate."+robot and proof["robot_id"]==robot and proof["child_task_id"]
        assert proof["stamp_s"]>proof["before_stamp_s"] and proof["imu_stamp_s"]>0 and proof["settled_samples"]>=4
        assert max(abs(a-b) for a,b in zip(proof["measured"],proof["before"]))>.13, (text,proof)
        raw=dc("exec","-T","campus-fleet","/usr/local/bin/edge-entrypoint","python3","-B",
               "/opt/edge/tests/system/campus_forms.py","--witness",robot,timeout=30)
        observation=json.loads(raw)
        assert all(math.isfinite(x) for x in observation["imu"]["value"])
        assert all(abs(x-proof["target"])<.11 for x in observation["measurement"]["value"].values()),(text,observation)
        tasks.append(dict(id=task["id"],text=text,trace_id=task["trace_id"],tool=step["tool"],
                          evidence=proof,independent_gazebo=observation))
    rejected=run("双足机器人起飞")
    assert rejected["state"] in {"FAILED","BLOCKED"} and not rejected["steps"][0].get("evidence"),rejected
    report["rejected"]=dict(text=rejected["text"],state=rejected["state"],detail=rejected["detail"])


if __name__=="__main__" and len(sys.argv)==3 and sys.argv[1]=="--witness":
    witness(sys.argv[2])
