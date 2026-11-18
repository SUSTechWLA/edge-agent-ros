"""Install the campus robots and their native Gazebo actuators and sensors."""
import hashlib
import json
import math
from pathlib import Path
import xml.etree.ElementTree as ET
from zipfile import ZipFile


HERE = Path(__file__).parent
YAW = math.radians(-70)
DEFAULTS = {"crazyflie": [11.5,5.8,.08,YAW], "go1":[16.,6.5,.06,YAW], "g1":[20.,4.9,.06,YAW]}
KINDS = {
    "edge_rover": ("ugv","Edge Rover · 差速车","drive"),
    "crazyflie": ("quadrotor","Bitcraze Crazyflie 2 · 四旋翼","altitude"),
    "go1": ("quadruped","Unitree Go1 · 四足","leg_flex"),
    "g1": ("humanoid","Unitree G1 · 人形","arm_raise"),
}


def val(parent, tag, value):
    ET.SubElement(parent,tag).text = str(value)


def system(model, library, name, settings):
    plugin = ET.SubElement(model,"plugin",filename="gz-sim-"+library+"-system",name="gz::sim::systems::"+name)
    for key,value in settings.items():
        val(plugin,key,value)


def append_robots(world, output, poses=None):
    """Add 3 bodies to the existing rover world; positions are odom x/y/z/yaw."""
    metadata = json.loads((HERE/"assets.json").read_bytes())
    target = Path(output)/"robots"
    target.mkdir(parents=True,exist_ok=True)
    with ZipFile(HERE/"models.zip") as z:
        for item,digest in metadata["files"].items():
            data = z.read(item)
            if hashlib.sha256(data).hexdigest() != digest:
                raise ValueError(f"Invalid campus robot asset {item}")
            (target/item).write_bytes(data)
    registry = []
    for name,(kind,label,motion) in KINDS.items():
        if name == "edge_rover":
            pose = [float(x) for x in world.findtext("model[@name='edge_rover']/pose", "0 0 0 0 0 0").split()]
        else:
            x,y,z,yaw = (poses or {}).get(name,DEFAULTS[name])
            pose = [x,y,z,0,0,yaw]
            model = ET.parse(target/(name+".sdf")).getroot()
            val(model,"pose"," ".join(f"{v:.8g}" for v in pose))
            link = model.find("link[@name='base_link']")
            sensor = ET.SubElement(link,"sensor",name="imu",type="imu")
            val(sensor,"topic",f"/model/{name}/imu")
            val(sensor,"always_on","true")
            val(sensor,"update_rate","20")
            ET.SubElement(sensor,"imu")
            system(model,"odometry-publisher","OdometryPublisher",{
                "odom_topic":f"/model/{name}/odometry", "dimensions":3, "odom_publish_frequency":20,
                "odom_frame":"world", "robot_base_frame":f"{name}/base_link"})
            if name == "crazyflie":
                system(model,"velocity-control","VelocityControl",{"topic":"/model/crazyflie/cmd_vel"})
            else:
                system(model,"joint-state-publisher","JointStatePublisher",{"topic":f"/model/{name}/joint_state"})
                for joint in model.findall("joint[@type='revolute']"):
                    joint_id = joint.get("name")
                    system(model,"joint-position-controller","JointPositionController",{
                        "joint_name":joint_id, "topic":f"/model/{name}/joint/{joint_id}/cmd_pos",
                        "use_velocity_commands":"true", "cmd_max":"1.6", "cmd_min":"-1.6", "initial_position":0})
            world.append(model)
        registry.append({"id":name,"class":kind,"label":label,"motion":motion,"spawn_pose":pose})
    return registry
