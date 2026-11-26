"""Generate the isolated four-hole Gazebo world; asset geometry is never consulted by the calibration estimator."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import struct
from xml.etree import ElementTree as ET


# OpenCV DICT_6X6_250, marker first row at the top of the board, including its black border.
BLACK = {
    1:"ff f9 a1 a3 b9 bb dd ff", 2:"ff f5 cd fd 83 a9 e5 ff",
    3:"ff 9b dd a7 9f cb c3 ff", 4:"ff 95 bf c1 d3 8f d5 ff",
}


def panel_meshes(folder, board, asset):
    panel, ink = [], []
    side=board["marker_size_m"]
    mx,my=board["marker_spacing_m"]
    hx,hy=board["hole_spacing_m"]
    radius=board["hole_diameter_m"]/2
    width,height=asset["outline_m"]
    thickness=asset["render_thickness_m"]
    if (not board.get("confirmed") or not (0<2*radius<min(hx,hy) and 0<side and
            hx+2*radius<width and hy+2*radius<height and mx+side<width and my+side<height and thickness>0)):
        raise ValueError("rendered geometry requires a confirmed compatible board profile")

    def quad(store, coords):
        store.extend([(coords[0],coords[1],coords[2]),(coords[0],coords[2],coords[3])])

    def vertex(depth,uv):
        return depth,-uv[0],-uv[1]  # u goes right and v goes down as seen from the rover

    for cx in (-hx/2,hx/2):
        for cy in (-hy/2,hy/2):
            xmin,xmax = (-width/2,0) if cx < 0 else (0,width/2)
            ymin,ymax = (-height/2,0) if cy < 0 else (0,height/2)
            angles = {i*math.tau/96 for i in range(96)}
            angles.update(math.atan2(y-cy,x-cx)%math.tau for x in (xmin,xmax) for y in (ymin,ymax))
            section = []
            for angle in sorted(angles):
                dx,dy = math.cos(angle),math.sin(angle)
                extent = min((xmax-cx)/dx if dx>1e-12 else (xmin-cx)/dx if dx < -1e-12 else math.inf,
                             (ymax-cy)/dy if dy>1e-12 else (ymin-cy)/dy if dy < -1e-12 else math.inf)
                section.append(((cx+radius*dx,cy+radius*dy),(cx+extent*dx,cy+extent*dy)))
            for (inner,outer),(ninner,nouter) in zip(section,section[1:]+section[:1]):
                quad(panel, [vertex(0,p) for p in (outer,inner,ninner,nouter)])
                quad(panel, [vertex(thickness,p) for p in (nouter,ninner,inner,outer)])
                quad(panel, [vertex(0,inner),vertex(thickness,inner),vertex(thickness,ninner),vertex(0,ninner)])
                quad(panel, [vertex(0,nouter),vertex(thickness,nouter),vertex(thickness,outer),vertex(0,outer)])
    for key,u,v in [(1,-mx/2,-my/2),(2,mx/2,-my/2),(3,-mx/2,my/2),(4,mx/2,my/2)]:
        cell=side/8
        for row,bits in enumerate(bytes.fromhex(BLACK[key])):
            for col in range(8):
                if bits & (128 >> col):
                    x,y = u-side/2+col*cell,v-side/2+row*cell
                    quad(ink,[vertex(-.00025,p) for p in [(x,y),(x,y+cell),(x+cell,y+cell),(x+cell,y)]])
    for name, triangles in (("panel",panel),("ink",ink)):
        with (folder/(name+".stl")).open("wb") as stream:
            stream.write(struct.pack("<80sI",b"Fixed calibration target",len(triangles)))
            for a,b,c in triangles:
                u,v = ([p-q for p,q in zip(k,a)] for k in (b,c))
                n = [u[1]*v[2]-u[2]*v[1],u[2]*v[0]-u[0]*v[2],u[0]*v[1]-u[1]*v[0]]
                size = math.hypot(*n) or 1
                stream.write(struct.pack("<12fH",*(x/size for x in n),*a,*b,*c,0))


def shape(link, name, pose, geometry, color, collision=True):
    rgba = " ".join(map(str,(*color,1)))
    link.append(ET.fromstring(f"""<visual name="{name}"><pose>{pose}</pose><geometry>{geometry}</geometry>
      <material><ambient>{rgba}</ambient><diffuse>{rgba}</diffuse><emissive>{rgba}</emissive></material></visual>"""))
    if collision:
        link.append(ET.fromstring(f'<collision name="{name}"><pose>{pose}</pose><geometry>{geometry}</geometry></collision>'))


def scan(link, name, topic, frame, pose, horizontal, vertical, angle_min, angle_max, rate, far):
    link.append(ET.fromstring(f"""<sensor name="{name}" type="gpu_lidar"><pose>{pose}</pose><topic>{topic}</topic>
      <gz_frame_id>{frame}</gz_frame_id><update_rate>{rate}</update_rate><always_on>true</always_on><lidar><scan>
      <horizontal><samples>{horizontal}</samples><min_angle>{-math.pi}</min_angle><max_angle>{math.pi}</max_angle></horizontal>
      <vertical><samples>{vertical}</samples><min_angle>{angle_min}</min_angle><max_angle>{angle_max}</max_angle></vertical>
      </scan><range><min>0.1</min><max>{far}</max><resolution>0.002</resolution></range></lidar></sensor>"""))


def generate(path, xacro, profile, initial_board="operator"):
    path.parent.mkdir(exist_ok=True,parents=True)
    asset=profile["board_asset"]
    panel_meshes(path.parent,profile["four_hole_board"],asset)
    robot = ET.parse(xacro)
    prop = {item.get("name"):float(item.get("value")) for item in robot.getroot() if item.tag.endswith("}property")}
    front = robot.find(".//joint[@name='front_lidar_joint']/origin")
    world = ET.fromstring("""<sdf version="1.9"><world name="fixed_calibration">
      <physics name="fixed" type="ignored"><max_step_size>0.005</max_step_size><real_time_factor>1</real_time_factor></physics>
      <plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
      <plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
      <plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>
      <plugin filename="gz-sim-imu-system" name="gz::sim::systems::Imu"/>
      <scene><ambient>1 1 1 1</ambient><background>.20 .23 .25 1</background></scene>
      <model name="ground"><static>true</static><link name="floor"/></model>
      <model name="calibration_board"><static>true</static><pose/><link name="board"/></model>
      <model name="lingao"><link name="base_link"><inertial><pose>0 0 .16 0 0 0</pose><mass>25</mass>
      <inertia><ixx>.30</ixx><iyy>.56</iyy><izz>.69</izz></inertia></inertial></link></model></world></sdf>""")
    floor = world.find(".//link[@name='floor']")
    x,y=asset["front_center_xy_m"]
    if initial_board=="operator":
        x,y=0.,4.
        ET.SubElement(world.find("world"),"plugin",filename="gz-sim-user-commands-system",
                      name="gz::sim::systems::UserCommands")
    world.find(".//model[@name='calibration_board']/pose").text=f"{x} {y} {asset['outline_m'][1]/2} 0 0 0"
    shape(floor,"floor","0 0 -.05 0 0 0","<box><size>18 16 .1</size></box>",(.3,.31,.32))
    shape(floor,"far_wall","5.6 0 1.3 0 0 0","<box><size>.1 5 2.6</size></box>",(.16,.22,.24))
    board = world.find(".//link[@name='board']")
    for name,color in [("panel",(.89,.89,.89)),("ink",(.002,.002,.002))]:
        mesh = (path.parent/(name+".stl")).resolve().as_uri()
        shape(board,name,"0 0 0 0 0 0",f"<mesh><uri>{mesh}</uri></mesh>",color,name=="panel")
    rover, base = world.find(".//model[@name='lingao']"),world.find(".//link[@name='base_link']")
    # The front mount is in a cutout between two bumper ends; use the archived outer envelope.
    shape(base,"chassis","-.03 0 .16 0 0 0","<box><size>.407 .329 .190</size></box>",(.18,.21,.24))
    for side in (-1,1):
        shape(base,"bumper_"+str(side),f".202 {side*.131} .16 0 0 0","<box><size>.063 .067 .19</size></box>",(.13,.15,.17))
    radius,track,width = prop["wheel_radius"],prop["wheel_track"],prop["wheel_width"]
    for end in ("front","rear"):
        for side in ("left","right"):
            name=end+"_"+side
            x=prop["wheel_base"]*.5*(1 if end=="front" else -1)
            y=track*.5*(1 if side=="left" else -1)
            wheel=ET.SubElement(rover,"link",name=name)
            ET.SubElement(wheel,"pose").text=f"{x} {y} {radius} 0 0 0"
            wheel.append(ET.fromstring("<inertial><mass>1.5</mass><inertia><ixx>.003</ixx><iyy>.0042</iyy><izz>.003</izz></inertia></inertial>"))
            shape(wheel,"tire",f"0 0 0 {math.pi/2} 0 0",f"<cylinder><radius>{radius}</radius><length>{width}</length></cylinder>",(.04,.045,.05))
            rover.append(ET.fromstring(f"""<joint name="{name}_joint" type="revolute"><parent>base_link</parent>
                <child>{name}</child><axis><xyz>0 1 0</xyz></axis></joint>"""))
    rover.append(ET.fromstring(f"""<plugin filename="gz-sim-diff-drive-system" name="gz::sim::systems::DiffDrive">
        <left_joint>front_left_joint</left_joint><left_joint>rear_left_joint</left_joint>
        <right_joint>front_right_joint</right_joint><right_joint>rear_right_joint</right_joint>
        <wheel_separation>{track}</wheel_separation><wheel_radius>{radius}</wheel_radius>
        <topic>/model/lingao/cmd_vel</topic><odom_topic>/fixed/odom</odom_topic><tf_topic>/fixed/tf</tf_topic>
        <frame_id>odom</frame_id><child_frame_id>base_link</child_frame_id><odom_publish_frequency>30</odom_publish_frequency>
        <max_linear_velocity>.20</max_linear_velocity><max_angular_velocity>.50</max_angular_velocity>
        <max_linear_acceleration>.4</max_linear_acceleration><max_angular_acceleration>1</max_angular_acceleration></plugin>"""))
    rover.append(ET.fromstring("""<plugin filename="gz-sim-odometry-publisher-system" name="gz::sim::systems::OdometryPublisher">
        <odom_topic>/fixed/truth/odom</odom_topic><odom_frame>world</odom_frame><robot_base_frame>base_link</robot_base_frame>
        <dimensions>3</dimensions><odom_publish_frequency>30</odom_publish_frequency></plugin>"""))
    rover.append(ET.fromstring("""<plugin filename="gz-sim-pose-publisher-system" name="gz::sim::systems::PosePublisher">
        <publish_model_pose>true</publish_model_pose><use_pose_vector_msg>true</use_pose_vector_msg>
        <update_frequency>30</update_frequency></plugin>"""))
    cam_mount = " ".join(str(v) for v in profile["extrinsics"]["rgbd"]["translation_m"])
    base.append(ET.fromstring(f"""<sensor name="d435_color" type="rgbd_camera"><pose>{cam_mount} 0 0 0</pose>
        <topic>/fixed/rgbd</topic><gz_frame_id>camera_color_optical_frame</gz_frame_id><always_on>true</always_on>
        <update_rate>5</update_rate><camera><horizontal_fov>1.211</horizontal_fov>
        <optical_frame_id>camera_color_optical_frame</optical_frame_id><image><width>640</width><height>480</height>
        <format>R8G8B8</format></image><clip><near>.1</near><far>9</far></clip>
        <camera_info_topic>/fixed/rgbd/camera_info</camera_info_topic></camera></sensor>"""))
    scan(base,"mid360","/fixed/front","front_mid360",front.get("xyz")+" "+front.get("rpy"),1440,129,
         math.radians(-7),math.radians(52),5,8)
    # This IMU reports sensor-frame angular rates; its lever arm is from the archived front mount.
    front_xyz=[float(x) for x in front.get("xyz").split()]
    roll,pitch,yaw=map(float,front.get("rpy").split())
    imu=robot.find(".//joint[@name='livox_imu_joint']/origin")
    dx,dy,dz=map(float,imu.get("xyz").split())
    if (roll,yaw)!=(0,0) or any(float(a) for a in imu.get("rpy").split()):
        raise ValueError("front IMU model assumes a surveyed pitch-only mount")
    c,s=math.cos(pitch),math.sin(pitch)
    imu_xyz=[front_xyz[0]+c*dx+s*dz, front_xyz[1]+dy, front_xyz[2]-s*dx+c*dz]
    mount=profile["extrinsics"]["imu"]
    if (math.dist(imu_xyz,mount["translation_m"])>1e-7 or
            math.dist([0,math.sin(pitch/2),0,math.cos(pitch/2)],mount["rotation_xyzw"])>1e-7):
        raise ValueError("IMU installation profile differs from the simulated sensor")
    pose=" ".join(str(value) for value in (*imu_xyz,roll,pitch,yaw))
    base.append(ET.fromstring(f"""<sensor name="mid360_imu" type="imu"><pose>{pose}</pose><topic>/fixed/front/imu</topic>
        <gz_frame_id>livox_frame</gz_frame_id><always_on>true</always_on><update_rate>100</update_rate><imu/></sensor>"""))
    scan(base,"fixture_clearance","/fixed/perimeter_scan","fixture_clearance","0 0 .32 0 0 0",360,1,0,0,10,8)
    ET.ElementTree(world).write(path,encoding="utf-8",xml_declaration=True)
    # The one-shot operator receipt belongs to the previous world incarnation. A container restart
    # regenerates the staged board, so the next placement must be allowed exactly once again.
    (path.parent/"operator-placement.requested").unlink(missing_ok=True)


def truth(path):
    import numpy as np
    model = ET.parse(path).find(".//model[@name='lingao']")
    def mount(sensor):
        x,y,z,r,p,a=map(float,model.findtext(f"link/sensor[@name='{sensor}']/pose").split())
        cr,cp,ca=map(math.cos,(r,p,a))
        sr,sp,sa=map(math.sin,(r,p,a))
        return np.array([[ca*cp,ca*sp*sr-sa*cr,ca*sp*cr+sa*sr,x],
                         [sa*cp,sa*sp*sr+ca*cr,sa*sp*cr-ca*sr,y],
                         [-sp,cp*sr,cp*cr,z],[0,0,0,1]])
    optical=np.array([[0,0,1,0],[-1,0,0,0],[0,-1,0,0],[0,0,0,1]])
    transform=np.linalg.inv(mount("d435_color")@optical)@mount("mid360")
    return {"world_sha256":hashlib.sha256(path.read_bytes()).hexdigest(), "parent_frame":"camera_color_optical_frame",
            "child_frame":"front_mid360", "rotation_matrix":transform[:3,:3].tolist(),"translation_m":transform[:3,3].tolist()}


def place_board(path, profile):
    """One local fixture operator action, outside the Agent, after the workbench asks for the board."""
    if (os.getenv("ROS_DOMAIN_ID"), os.getenv("GZ_PARTITION")) != ("197", "edge_fixed_rover_197"):
        raise RuntimeError("expected the isolated Gazebo calibration project")
    world=ET.parse(path).find("world")
    board=world.find("model[@name='calibration_board']")
    if (world.get("name")!="fixed_calibration" or board.findtext("static")!="true" or
            list(map(float,board.findtext("pose").split()))[:2]!=[0.,4.]):
        raise RuntimeError("this world does not have a staged operator placement")
    from gz.transport13 import Node
    from gz.msgs10.pose_pb2 import Pose
    from gz.msgs10.boolean_pb2 import Boolean
    request=Pose(name="calibration_board")
    request.position.x,request.position.y=profile["board_asset"]["front_center_xy_m"]
    request.position.z=profile["board_asset"]["outline_m"][1]/2
    request.orientation.w=1.
    with (path.parent/"operator-placement.requested").open("x") as receipt:
        receipt.write(str(request))
    ok,reply=Node().request("/world/fixed_calibration/set_pose",request,Pose,Boolean,5000)
    if not ok or not reply.data:
        raise RuntimeError("Gazebo did not confirm placement; inspect the world before any retry")
    return {"actor":"simulated_operator","service":"/world/fixed_calibration/set_pose","accepted":True,
            "entity":request.name,"position_m":[request.position.x,request.position.y,request.position.z]}


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("action",choices=["generate","truth","place-front"])
    cli.add_argument("path",type=Path)
    cli.add_argument("--urdf",type=Path)
    cli.add_argument("--profile",type=Path)
    cli.add_argument("--initial-board", choices=["front","operator"], default="operator")
    args=cli.parse_args()
    if args.action=="generate":
        generate(args.path,args.urdf,json.loads(args.profile.read_bytes()),args.initial_board)
    elif args.action=="place-front":
        print(json.dumps(place_board(args.path,json.loads(args.profile.read_bytes()))))
    else:
        print(json.dumps(truth(args.path),allow_nan=False))
