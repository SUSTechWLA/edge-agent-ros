"""Campus simulation tools, explicit ROS interfaces and per-robot Agent registries."""
import json
from pathlib import Path
import shutil

ROBOTS = {
    "edge_rover": ("轮式无人车", "门厅穿门导航"),
    "crazyflie": ("Crazyflie 四旋翼", "高度闭环、定高与降落"),
    "go1": ("Unitree Go1 四足", "测试架上四腿关节联动"),
    "g1": ("Unitree G1 双足", "测试架上左臂关节控制"),
}
JOINTS = {"go1": [side+"_thigh_joint" for side in ("FL","FR","RL","RR")],
          "g1":["left_shoulder_pitch_joint"]}
# Fixed, bounded simulation test actions. Each target is an absolute setpoint,
# so retrying an accepted operation cannot accumulate an extra motion.
ACTIONS = {
    "crazyflie":{"起飞":1., "降落":.075},
    "go1":{"屈腿":.65, "伸腿":.15},
    "g1":{"举起左臂":-.85, "放下左臂":0.},
}
NAMES = {"crazyflie":["无人机","四旋翼","Crazyflie"],
         "go1":["四足机器人","机器狗","Go1"], "g1":["双足机器人","人形机器人","G1"]}


def install(world, defaults):
    registries=world/"agents"
    hub=registries/"hub"
    hub.mkdir(parents=True,exist_ok=True)
    for path in defaults.glob("*.json"):
        shutil.copyfile(path,hub/path.name)
    bridge=[]
    def connect(topic,ros,gz,direction):
        bridge.append(dict(ros_topic_name=topic,gz_topic_name=topic,ros_type_name=ros,gz_type_name=gz,direction=direction))
    for robot,actions in ACTIONS.items():
        local=registries/robot
        local.mkdir(parents=True,exist_ok=True)
        for directory,prefix,route in ((local,"robot","execute"),(hub,"delegate","delegate")):
            key=prefix+"."+robot
            manifest=dict(id=key,version="1",effect="idempotent",accepts_trace=True,
                          description=ROBOTS[robot][0]+"执行"+"/".join(actions),
                          parameters={"action":"string"},outcomes={"reading":"measured"},
                          utterances=[n+"{action}" for n in NAMES[robot]],
                          endpoint=f"http://campus-fleet:8119/{route}/{robot}")
            (directory/(key+".json")).write_text(json.dumps(manifest,ensure_ascii=False),encoding="utf-8")
        connect("/model/"+robot+"/imu","sensor_msgs/msg/Imu","gz.msgs.IMU","GZ_TO_ROS")
        for joint in JOINTS.get(robot,[]):
            connect(f"/model/{robot}/joint/{joint}/cmd_pos","std_msgs/msg/Float64","gz.msgs.Double","ROS_TO_GZ")
        if robot in JOINTS:
            connect("/model/"+robot+"/joint_state","sensor_msgs/msg/JointState","gz.msgs.Model","GZ_TO_ROS")
    connect("/model/crazyflie/cmd_vel","geometry_msgs/msg/Twist","gz.msgs.Twist","ROS_TO_GZ")
    (world/"robot-bridge.yaml").write_text(json.dumps(bridge,indent=2)+"\n",encoding="utf-8")


if __name__=="__main__":
    install(Path("/campus"),Path("/opt/edge/scene/plugins"))
