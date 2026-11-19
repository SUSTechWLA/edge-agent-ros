"""SDD-030. Submit tasks to the normal API and independently read the simulated robot sensors."""
import json
import math
import sys
import time
from urllib.parse import urlencode
import uuid


def witness(robot):
    from gz.transport13 import Node
    from gz.msgs10.imu_pb2 import IMU
    from gz.msgs10.model_pb2 import Model
    from gz.msgs10.pose_v_pb2 import Pose_V
    from threading import Event
    topics,done={},Event()
    node=Node()
    def arrived(name,t,value):
        topics[name]={"stamp_s":t.sec+t.nsec*1e-9,"value":value}
        if "measurement" in topics and "imu" in topics:
            done.set()
    def imu(msg):
        a=msg.linear_acceleration
        arrived("imu",msg.header.stamp,[a.x,a.y,a.z])
    def joints(msg):
        names={"left_shoulder_pitch_joint"} if robot=="g1" else {s+"_thigh_joint" for s in ("FL","FR","RL","RR")}
        numbers={j.name:j.axis1.position for j in msg.joint if j.name in names}
        if names==numbers.keys():
            arrived("measurement",msg.header.stamp,numbers)
    def poses(msg):
        p=next((p for p in msg.pose if p.name==robot),None)
        if p is not None:
            arrived("measurement",msg.header.stamp,{"altitude":p.position.z})
    sensor = (Pose_V,"/world/edge_lab/dynamic_pose/info",poses) if robot=="crazyflie" else (
        Model,"/model/"+robot+"/joint_state",joints)
    active = []
    try:
        for message_type, topic, handler in ((IMU,"/model/"+robot+"/imu",imu), sensor):
            if not node.subscribe(message_type, topic, handler):
                raise RuntimeError(f"unable to observe {topic}")
            active.append(topic)
        if not done.wait(18):
            raise TimeoutError(f"{robot} independent Gazebo witness missed data: {list(topics)}")
    finally:
        # End both native callback registrations while their Python owners are alive.
        for topic in reversed(active):
            node.unsubscribe(topic)
    print(json.dumps(topics,allow_nan=False))


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
