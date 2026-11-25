"""Isolated Gazebo board station. Only its operator and independent witness know poses."""
import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
from xml.etree import ElementTree as ET

RIG = {"rgbd": (.08, .01, .85, .04), "lidar_left": (.01, .24, .76, .12),
       "lidar_right": (.015, -.25, .93, -.14)}
# x, y, z, pitch, yaw in the stationary rig world. Pose zero stores the board behind the rig.
POSES = [(-3., 0., .85, 0., 0.), (1.6, .0, .88, 0., 0.),
         (1.8, .14, .92, .53, .08), (1.6, -.17, .78, -.12, -.59),
         (1.9, .10, .74, -.54, .17), (1.7, -.08, 1.02, .12, .57),
         (1.9, .18, .87, .42, -.46), (1.62, -.18, .88, -.42, -.44),
         (1.78, .08, .93, -.39, .52), (1.56, -.05, .78, .44, .39),
         (1.7, .16, .79, .57, -.17), (1.8, -.09, 1.04, -.57, .03),
         (1.68, .10, 1., .20, -.57)]
WORLD = """<sdf version="1.9"><world name="calibration_bench">
<physics name="clock" type="ignored"><max_step_size>0.01</max_step_size><real_time_factor>1</real_time_factor></physics>
<plugin filename="gz-sim-physics-system" name="gz::sim::systems::Physics"/>
<plugin filename="gz-sim-user-commands-system" name="gz::sim::systems::UserCommands"/>
<plugin filename="gz-sim-scene-broadcaster-system" name="gz::sim::systems::SceneBroadcaster"/>
<plugin filename="gz-sim-sensors-system" name="gz::sim::systems::Sensors"><render_engine>ogre2</render_engine></plugin>
<scene><ambient>1 1 1 1</ambient><background>0.32 0.34 0.37 1</background></scene>
<model name="campus_sign"><static>true</static><pose>2.4 1.02 1.27 0 0 0</pose>
<link name="sign"><visual name="magenta_marker"><geometry><box><size>0.025 0.19 0.15</size></box></geometry>
<material><ambient>0.35 0.004 0.20 1</ambient><diffuse>0.35 0.004 0.20 1</diffuse>
<emissive>0.35 0.004 0.20 1</emissive></material></visual></link></model>
<model name="stationary_rig"><static>true</static><link name="mount"/></model>
<model name="calibration_board"><static>true</static><pose>-3 0 0.85 0 0 0</pose><link name="board"/></model>
</world></sdf>"""

def generate(path, sensor_rate=5, real_time_factor=1.0):
    tree = ET.fromstring(WORLD)
    tree.find(".//physics/real_time_factor").text = str(real_time_factor)
    mount = tree.find(".//link[@name='mount']")
    for key, (x,y,z,yaw) in RIG.items():
        camera = key == "rgbd"
        topic = "/calibration/" + key
        sensor = ET.SubElement(mount, "sensor", name=key, type="rgbd_camera" if camera else "gpu_lidar")
        for tag, val in {"pose":f"{x} {y} {z} 0 0 {yaw}", "topic":topic,
                         "gz_frame_id":"rgbd_optical" if camera else key,
                         "update_rate":str(sensor_rate), "always_on":"true"}.items():
            ET.SubElement(sensor, tag).text = val
        if camera:
            sensor.append(ET.fromstring(f"""<camera><horizontal_fov>1.2</horizontal_fov>
<optical_frame_id>rgbd_optical</optical_frame_id><image><width>640</width><height>480</height>
<format>R8G8B8</format></image><clip><near>0.1</near><far>7</far></clip>
<camera_info_topic>{topic}/camera_info</camera_info_topic></camera>"""))
        else:
            sensor.append(ET.fromstring("""<lidar><scan><horizontal><samples>241</samples><resolution>1</resolution>
<min_angle>-0.95</min_angle><max_angle>0.95</max_angle></horizontal><vertical><samples>101</samples>
<resolution>1</resolution><min_angle>-0.65</min_angle><max_angle>0.65</max_angle></vertical>
</scan><range><min>0.10</min><max>7</max><resolution>0.002</resolution></range></lidar>"""))
    board = tree.find(".//link[@name='board']")
    def face(name, x, y, z, thickness, width, height, grey):
        rgba = f"{grey} {grey} {grey} 1"
        board.append(ET.fromstring(f"""<visual name="{name}"><pose>{x} {y} {z} 0 0 0</pose>
<geometry><box><size>{thickness} {width} {height}</size></box></geometry>
<material><ambient>{rgba}</ambient><diffuse>{rgba}</diffuse><emissive>{rgba}</emissive></material></visual>"""))
    face("backing", .006, 0, 0, .012, .64, .48, .95)
    for row in range(5):
        for col in range(7):
            if (col + row) % 2 == 0:
                face(f"ink_{row}_{col}", -.0004, (col-3)*.08, (row-2)*.08, .0008, .08, .08, .005)
    ET.ElementTree(tree).write(path, encoding="utf-8", xml_declaration=True)

def present(index):
    x, y, z, pitch, yaw = POSES[index]
    sp, cp = math.sin(pitch/2), math.cos(pitch/2)
    sy, cy = math.sin(yaw/2), math.cos(yaw/2)
    req = (f'name: "calibration_board", position: {{x: {x}, y: {y}, z: {z}}}, '
           f'orientation: {{x: {-sy*sp}, y: {cy*sp}, z: {sy*cp}, w: {cy*cp}}}')
    reply = subprocess.check_output(["gz","service","-s","/world/calibration_bench/set_pose",
        "--reqtype","gz.msgs.Pose","--reptype","gz.msgs.Boolean","--timeout","5000","--req",req], text=True)
    if "data: true" not in reply:
        raise RuntimeError("Gazebo did not move board: " + reply)
    print(json.dumps({"operator":"simulated_board_holder","fixture_pose_index":index}))


def check(candidate, sdf):
    """Score only the learner's output, using the actual world loaded by this rig."""
    import hashlib
    import numpy as np

    tree = ET.parse(sdf)
    def mount(name):
        sensor = tree.find(f".//model[@name='stationary_rig']/link/sensor[@name='{name}']")
        x,y,z,roll,pitch,yaw = map(float, sensor.findtext("pose").split())
        if roll or pitch:
            raise ValueError("audit expects a level yaw-only sensor fixture")
        c,s=math.cos(yaw),math.sin(yaw)
        out = np.array([[c,-s,0,x],[s,c,0,y],[0,0,1,z],[0,0,0,1]])
        if name == "rgbd":
            out = out @ [[0,0,1,0],[-1,0,0,0],[0,-1,0,0],[0,0,0,1]]
        return out
    optical_from_world = np.linalg.inv(mount("rgbd"))
    rows = {}
    expected = {"lidar_left", "lidar_right"}
    if set(candidate) != expected:
        raise ValueError("fixture and candidate roles differ")
    for role in sorted(expected):
        item = candidate[role]
        if item["parent_frame"] != "rgbd_optical" or item["child_frame"] != role:
            raise ValueError("fixture and candidate frame mismatch")
        target = optical_from_world @ mount(role)
        t = np.asarray(item["translation_m"], dtype=float)
        q = np.asarray(item["rotation_xyzw"], dtype=float)
        if t.shape!=(3,) or q.shape!=(4,) or abs(np.linalg.norm(q)-1.)>1e-6:
            raise ValueError("invalid estimated transform")
        x,y,z,w = q
        skew=np.array([[0,-z,y],[z,0,-x],[-y,x,0]])
        r=np.eye(3)+2*w*skew+2*skew@skew
        angle = math.degrees(math.acos(float(np.clip((np.trace(target[:3,:3].T @ r)-1)/2,-1,1))))
        offset = float(np.linalg.norm(t-target[:3,3]))
        rows[role] = dict(translation_error_m=offset, rotation_error_deg=angle,
                          within_limits=bool(offset <= .035 and angle <= 1.5))
    return dict(source="Gazebo loaded SDF mounting poses, independently evaluated after solve",
                world_sha256=hashlib.sha256(sdf.read_bytes()).hexdigest(),
                thresholds=dict(translation_m=.035, rotation_deg=1.5), errors=rows,
                passed=all(r["within_limits"] for r in rows.values()))

if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    commands = cli.add_subparsers(dest="command", required=True)
    generator = commands.add_parser("generate")
    generator.add_argument("path", type=Path)
    generator.add_argument("--sensor-rate", type=int, choices=(5, 15), default=5)
    generator.add_argument("--real-time-factor", type=float, choices=(.5, 1.), default=1.)
    commands.add_parser("present").add_argument("index", type=int, choices=range(len(POSES)))
    commands.add_parser("check").add_argument("--world", default=Path("/tmp/board.sdf"), type=Path)
    args = cli.parse_args()
    if args.command == "generate":
        generate(args.path, args.sensor_rate, args.real_time_factor)
    elif args.command == "present":
        present(args.index)
    else:
        result = check(json.load(sys.stdin), args.world)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        sys.exit(0 if result["passed"] else 1)
