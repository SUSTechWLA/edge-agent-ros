"""Local Gazebo instrument: a physical cart moves across the rover's corridor."""
import json
from pathlib import Path
import time
from xml.etree import ElementTree as ET

CART = "crossing_cart"
TRUTH = "/fixture/crossing/cart_world_odometry"
DRIVE = "/model/crossing_cart/joint/guide/cmd_vel"


def value(node,key,text):
    ET.SubElement(node,key).text=str(text)


def box(node,size):
    for kind in ("collision","visual"):
        part=ET.SubElement(node,kind,name=kind)
        value(ET.SubElement(ET.SubElement(part,"geometry"),"box"),"size"," ".join(map(str,size)))


def install(world):
    # The gate allows the cart to exit the lane. The side walls are physical
    # boxes, also independently included in the witness's swept clearances.
    for side in (-1,1):
        for i,(lo,hi) in enumerate(((5.3,7.10),(8.20,10.50))):
            m=ET.SubElement(world,"model",name=f"crossing_rail_{side}_{i}")
            value(m,"static","true");value(m,"pose",f"{(lo+hi)/2} {1.15*side} .35 0 0 0")
            box(ET.SubElement(m,"link",name="wall"),(hi-lo,.05,.70))
    actor=ET.SubElement(world,"model",name=CART,canonical_link="cart")
    value(actor,"pose","7.65 -0.38 0.34 0 0 0")
    cart=ET.SubElement(actor,"link",name="cart")
    inertia=ET.SubElement(cart,"inertial");value(inertia,"mass",8.)
    matrix=ET.SubElement(inertia,"inertia")
    for k,v in (("ixx",.4483),("iyy",.3883),("izz",.2733)):
        value(matrix,k,v)
    box(cart,(.40,.50,.65))
    contact=ET.SubElement(cart,"sensor",name="cart_contact",type="contact")
    value(contact,"always_on","true");value(contact,"update_rate",50)
    watch=ET.SubElement(contact,"contact")
    value(watch,"collision","collision");value(watch,"topic","/fixture/crossing/cart_contacts")
    guide=ET.SubElement(actor,"joint",name="guide",type="prismatic")
    value(guide,"parent","world");value(guide,"child","cart")
    axis=ET.SubElement(guide,"axis");value(axis,"xyz","0 1 0")
    limit=ET.SubElement(axis,"limit");value(limit,"lower",0);value(limit,"upper",1.9)
    plugin=ET.SubElement(actor,"plugin",filename="gz-sim-joint-controller-system",name="gz::sim::systems::JointController")
    value(plugin,"joint_name","guide")
    truth=ET.SubElement(actor,"plugin",filename="gz-sim-odometry-publisher-system",name="gz::sim::systems::OdometryPublisher")
    value(truth,"odom_topic",TRUTH);value(truth,"odom_frame","world");value(truth,"robot_base_frame","cart")
    value(truth,"dimensions",2);value(truth,"odom_publish_frequency",50)


def arm(directory):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from gz.msgs10.double_pb2 import Double
    from gz.transport13 import Node
    from rosgraph_msgs.msg import Clock
    from std_msgs.msg import String
    rclpy.init()
    node=rclpy.create_node("crossing_test_cart_trigger")
    gz=Node()
    rate=gz.advertise(DRIVE,Double)
    now=0; start=None; operation=None
    def clock(m):
        nonlocal now
        now=m.clock.sec*10**9+m.clock.nanosec
    def event(m):
        nonlocal start,operation
        e=json.loads(m.data)
        if start is None and now>0 and e.get("module")=="edge.gateway" and e.get("kind")=="action.started":
            start=now;operation=e["operation_id"]
            (directory/"cart-motion.json").write_text(json.dumps({"operation":operation,"start_ns":start,
                "commanded_speed_mps":.13,"actuator_topic":DRIVE}))
    node.create_subscription(Clock,"/clock",clock,qos_profile_sensor_data)
    node.create_subscription(String,"/edge/events",event,100)
    (directory/"cart-armed.json").write_text(json.dumps({"actuator_topic":DRIVE,"speed_mps":.13}))
    deadline=time.monotonic()+300
    last=-1
    try:
        while time.monotonic()<deadline:
            rclpy.spin_once(node,timeout_sec=.05)
            if start is None or now-last<50_000_000:
                continue
            last=now
            moving=(now-start)<14_000_000_000
            rate.publish(Double(data=.13 if moving else 0.))
            if not moving:
                (directory/"cart-motion.json").write_text(json.dumps({"operation":operation,"start_ns":start,
                    "stop_ns":now,"commanded_speed_mps":.13,"actuator_topic":DRIVE}))
                return
        raise TimeoutError("no crossing operation or stalled simulation clock")
    finally:
        rate.publish(Double(data=0.));node.destroy_node();rclpy.try_shutdown()


if __name__=="__main__":
    import argparse
    p=argparse.ArgumentParser();p.add_argument("--arm",required=True,type=Path)
    arm(p.parse_args().arm)
