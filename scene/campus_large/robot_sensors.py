"""Body-fixed campus instruments. ROS bridges and static TF share the same mount definitions."""
import json
import math
from xml.etree import ElementTree as ET


# Positions are in the named robot's base_link (metres), rotations in radians.
# Optical centres are in front of the casing. In Gazebo a camera looks along +X.
MOUNTS = (
    ("edge_rover","rgbd","rgbd_camera",(.162,0,.14),(.068,.026,.025)),
    ("g1","rgbd","rgbd_camera",(.116,0,1.235),(.066,.023,.023)),
    ("crazyflie","front","camera",(.032,0,.011),(.013,.009,.008)),
    ("crazyflie","down","gpu_lidar",(0,0,-.019),(.008,.008,.003)),
    ("go1","lidar","gpu_lidar",(.035,0,.397),(.075,.075,.055)),
)


def text(node,key,value):
    ET.SubElement(node,key).text=str(value)


def values(args):
    return " ".join(f"{v:.9g}" for v in args)


def instrument_housing(link,name,xyz,size,down):
    visual=ET.SubElement(link,"visual",name=name+"_housing")
    # Place the enclosure behind the measurement plane. Keep the rotary
    # laser's horizontal rings above the top of the housing.
    if name=="lidar" or down:
        offset=(0,0,(1 if down else -1)*(size[2]/2+.001))
    else:
        offset=(-size[1]/2-.002,0,0)
    text(visual,"pose",values([*(a+b for a,b in zip(xyz,offset)),0,0,0]))
    geom=ET.SubElement(visual,"geometry")
    if name=="lidar":
        shape=ET.SubElement(geom,"cylinder")
        text(shape,"radius",size[0]/2)
        text(shape,"length",size[2])
    else:
        text(ET.SubElement(geom,"box"),"size",values((size[1],size[0],size[2])))
    material=ET.SubElement(visual,"material")
    for k in ("diffuse","ambient"):
        text(material,k,"0.12 0.17 0.20 1")


def append_instruments(world,output):
    rig={"schema_version":1,"instruments":[],"bridges":[],"transforms":[]}
    def stream(gz_topic,ros_topic,ros_type,gz_type):
        rig["bridges"].append(dict(ros_topic_name=ros_topic,gz_topic_name=gz_topic,
            ros_type_name="sensor_msgs/msg/"+ros_type,gz_type_name="gz.msgs."+gz_type,
            direction="GZ_TO_ROS",qos_profile="SENSOR_DATA",lazy=True))
        return ros_topic
    def transform(parent,child,xyz,rpy=(0,0,0)):
        rig["transforms"].append(dict(parent=parent,child=child,xyz=list(xyz),rpy=list(rpy)))
    for robot,name,kind,xyz,size in MOUNTS:
        link=world.find(f"model[@name='{robot}']/link[@name='base_link']")
        base="base_link" if robot=="edge_rover" else robot+"/base_link"
        body_frame=f"{robot}/{name}_link"
        topic=f"/model/{robot}/{name}"
        down=name=="down"
        rpy=(0,math.pi/2 if down else 0,0)
        instrument_housing(link,name,xyz,size,down)
        transform(base,body_frame,xyz,rpy)
        sensor=ET.SubElement(link,"sensor",name=name,type=kind)
        text(sensor,"pose",values((*xyz,*rpy)))
        text(sensor,"always_on","true")
        text(sensor,"topic",topic)
        hz=8 if down else 4
        text(sensor,"update_rate",hz)
        frame=body_frame
        if kind.endswith("camera"):
            frame=f"{robot}/{name}_optical"
            transform(body_frame,frame,(0,0,0),(-math.pi/2,0,-math.pi/2))
            camera=ET.SubElement(sensor,"camera")
            text(camera,"horizontal_fov",1.10)
            text(camera,"optical_frame_id",frame)
            image=ET.SubElement(camera,"image")
            text(image,"width",160)
            text(image,"height",120)
            text(image,"format","R8G8B8")
            clip=ET.SubElement(camera,"clip")
            text(clip,"near",.12 if kind=="rgbd_camera" else .04)
            text(clip,"far",12 if kind=="rgbd_camera" else 50)
            text(camera,"camera_info_topic",topic+"/camera_info")
            sources={"color":stream(topic+"/image" if kind=="rgbd_camera" else topic,topic+"/image_raw","Image","Image"),
                     "info":stream(topic+"/camera_info",topic+"/camera_info","CameraInfo","CameraInfo")}
            if kind=="rgbd_camera":
                sources["depth"]=stream(topic+"/depth_image",topic+"/depth/image_raw","Image","Image")
        else:
            lidar=ET.SubElement(sensor,"lidar")
            scan=ET.SubElement(lidar,"scan")
            for axis,n,lo,hi in (("horizontal",3 if down else 180,-.035 if down else -math.pi,
                                   .035 if down else math.pi*(1-2/180)),("vertical",1 if down else 16,0 if down else -.45,0 if down else .30)):
                part=ET.SubElement(scan,axis)
                for k,v in (("samples",n),("min_angle",lo),("max_angle",hi),("resolution",1)):
                    text(part,k,v)
            ranges=ET.SubElement(lidar,"range")
            for k,v in (("min",.02 if down else .15),("max",4 if down else 30),("resolution",.005)):
                text(ranges,k,v)
            if down:
                sources={"range":stream(topic,topic+"/scan","LaserScan","LaserScan")}
            else:
                sources={"cloud":stream(topic+"/points",topic+"/points","PointCloud2","PointCloudPacked")}
        text(sensor,"gz_frame_id",frame)
        rig["instruments"].append(dict(robot=robot,id=name,type=kind,frame=frame,rate_hz=hz,topics=sources))

    for robot in ("edge_rover","crazyflie","go1","g1"):
        model=world.find(f"model[@name='{robot}']")
        imu=model.find("link[@name='base_link']/sensor[@type='imu']")
        if robot=="edge_rover":
            transform("base_link","laser_link",(.08,0,.20))
            transform("base_link","imu_link",(0,0,0))
            plugin=ET.SubElement(model,"plugin",filename="gz-sim-joint-state-publisher-system",name="gz::sim::systems::JointStatePublisher")
            topic="/model/edge_rover/wheel_states"
            text(plugin,"topic",topic)
            text(plugin,"update_rate",20)
            for axle in ("left_axle","right_axle"):
                text(plugin,"joint_name",axle)
            stream(topic,topic,"JointState","Model")
        else:
            frame=robot+"/imu_link"
            text(imu,"gz_frame_id",frame)
            transform(robot+"/base_link",frame,(0,0,0))
    (output/"sensor-rig.json").write_text(json.dumps(rig,indent=2)+"\n",encoding="utf-8")
    return rig["instruments"]
