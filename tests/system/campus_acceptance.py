"""Run a private Gazebo campus, submit a named-place mission and compare independent live observations."""
import argparse
from array import array
import base64
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import struct
import zlib
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler
import uuid
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
AREAS = ("fuel_campus", "teaching", "woods", "road", "track", "lawn")
SERVICES = ("sim", "bridge", "witness", "gateway", "core", "perception", "scene", "agent", "console")


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def write(path, value):
    stage = path.with_suffix(".tmp")
    stage.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    stage.replace(path)


def await_fact(read, message, timeout=90):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            result = read()
            if result:
                return result
        except (OSError, ValueError, KeyError) as error:
            last = error
        time.sleep(.3)
    raise AssertionError(f"Timeout: {message}: {last}")


def ros_witness():
    import rclpy
    from nav_msgs.msg import Odometry
    from rosgraph_msgs.msg import Clock
    from sensor_msgs.msg import LaserScan
    from rclpy.qos import qos_profile_sensor_data

    rclpy.init()
    node = rclpy.create_node("campus_independent_observer")
    counters = {key: 0 for key in ("clock", "odom", "truth", "scan")}
    stamps = {}
    tracks = {key: deque(maxlen=900) for key in ("odom", "truth")}
    scans = deque(maxlen=160)

    def stamp(t):
        return t.sec * 10**9 + t.nanosec
    def accept(kind, t):
        if t <= stamps.get(kind, 0):
            return False
        counters[kind] += 1
        stamps[kind] = t
        return True
    def pose(kind, msg):
        t = stamp(msg.header.stamp)
        if accept(kind, t) and (not tracks[kind] or t - tracks[kind][-1][0] >= 100_000_000):
            p = msg.pose.pose.position
            tracks[kind].append([t, round(p.x,4), round(p.y,4), round(msg.twist.twist.linear.x,4)])
    def scan(msg):
        t = stamp(msg.header.stamp)
        if not accept("scan", t):
            return
        finite = [v for v in msg.ranges if math.isfinite(v) and msg.range_min <= v <= msg.range_max]
        if finite and (not scans or t-scans[-1]["stamp_ns"] >= 300_000_000):
            scans.append({"stamp_ns":t,"frame":msg.header.frame_id,"rays":len(msg.ranges),"returns":len(finite),
                          "min_m":round(min(finite),4),"max_m":round(max(finite),4)})
    node.create_subscription(Clock, "/clock", lambda m: accept("clock", stamp(m.clock)), qos_profile_sensor_data)
    node.create_subscription(Odometry, "/odom", lambda m: pose("odom", m), qos_profile_sensor_data)
    node.create_subscription(Odometry, "/ground_truth", lambda m: pose("truth", m), qos_profile_sensor_data)
    node.create_subscription(LaserScan, "/scan", scan, qos_profile_sensor_data)
    def flush():
        write(Path("/audit/ros.json"), {"stamps":stamps,"counts":counters,"scan":list(scans),
                                       "truth":list(tracks["truth"]),"odom":list(tracks["odom"])})
    node.create_timer(.22, flush)
    try:
        rclpy.spin(node)
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


def road_connection(models):
    """Measure the join in the simulator's reconstructed SDF and the original OBJ Road polygons."""
    def pose(element):
        return tuple(map(float,(element.findtext("pose") or "0 0 0 0 0 0").split()))
    def translation(*elements):
        values=[pose(e) for e in elements]
        require(all(abs(v)<1e-7 for row in values for v in row[3:]),"unexpected rotated parent in campus mesh")
        return tuple(sum(row[i] for row in values) for i in range(3))
    def rectangle(name):
        visual=road_link.find("visual[@name='"+name+"']")
        require(visual is not None,"road section missing: "+name)
        x,y,z,roll,pitch,yaw=pose(visual)
        require(abs(roll)+abs(pitch)<1e-7,"tilted connector geometry")
        length,width,height=map(float,visual.findtext("geometry/box/size").split())
        dx,dy=math.cos(yaw),math.sin(yaw)
        x+=road_origin[0]
        y+=road_origin[1]
        return ([x-length*dx/2,y-length*dy/2], [x+length*dx/2,y+length*dy/2],
                width, road_origin[2]+z+height/2)

    road_link=models["road"].find("link")
    road_origin=translation(models["road"],road_link)
    drive_start,drive_end,drive_width,_=rectangle("campus_drive")
    names=sorted((v.get("name") for v in road_link.findall("visual") if re.fullmatch(r"north_link_\d+",v.get("name",""))),
                 key=lambda n:int(n.rsplit("_",1)[1]))
    require(len(names)>=2,"expected a multi-segment connection")
    segments=[rectangle(name) for name in names]
    first=segments[0][0]
    v=[b-a for a,b in zip(drive_start,drive_end)]
    t=sum((x-a)*component for x,a,component in zip(first,drive_start,v))/sum(k*k for k in v)
    projected=[a+t*component for a,component in zip(drive_start,v)]
    require(0<=t<=1 and math.dist(first,projected)<drive_width/2,"connector does not reach the main drive")
    gaps=[math.dist(previous[1],following[0]) for previous,following in zip(segments,segments[1:])]
    require(max(gaps)<.01,"gap between connector centerlines")
    end=segments[-1][1]
    model=models["fuel_campus"]
    link=model.find("link")
    visual=link.find("visual")
    mesh=visual.find("geometry/mesh")
    require(mesh is not None,"source visual mesh not found")
    source=Path("/assets/models/Campus/meshes/Campus_world.obj")
    require(mesh.findtext("uri","").endswith("/meshes/"+source.name),"unexpected campus mesh")
    offset=translation(model,link,visual)
    scale=list(map(float,(mesh.findtext("scale") or "1 1 1").split()))
    require(min(scale)>0,"invalid mesh scale")
    q=[(end[i]-offset[i])/scale[i] for i in range(2)]
    floor=models["floor"]
    floor_link=floor.find("link")
    floor_collision=floor_link.find("collision")
    require(floor_collision.find("geometry/plane") is not None,"continuous floor missing")
    ground=translation(floor,floor_link,floor_collision)[2]
    vertices=array("d")
    material=None
    with source.open(encoding="utf-8",errors="strict") as obj:
        for line_num,line in enumerate(obj,1):
            if line.startswith("v "):
                vertices.extend(float(x) for x in line.split()[1:4])
            elif line.startswith("usemtl "):
                material=line.split(None,1)[1].strip()
            elif material=="Road" and line.startswith("f "):
                refs=[int(token.split("/")[0]) for token in line.split()[1:]]
                xyz=[vertices[(r-1)*3:(r-1)*3+3] for r in refs]
                for i in range(1,len(xyz)-1):
                    a,b,c=xyz[0],xyz[i],xyz[i+1]
                    det=(b[1]-c[1])*(a[0]-c[0])+(c[0]-b[0])*(a[1]-c[1])
                    if abs(det)<1e-12:
                        continue
                    u=((b[1]-c[1])*(q[0]-c[0])+(c[0]-b[0])*(q[1]-c[1]))/det
                    v=((c[1]-a[1])*(q[0]-c[0])+(a[0]-c[0])*(q[1]-c[1]))/det
                    weights=(u,v,1-u-v)
                    if min(weights)<-1e-7:
                        continue
                    z=offset[2]+scale[2]*sum(k*p[2] for k,p in zip(weights,(a,b,c)))
                    require(z>ground and abs(z-segments[-1][3])<.03,"buried road or vertical step at connection")
                    return {"segment_names":names,"first_xy_m":first,"tip_xy_m":end,
                            "junction_gaps_m":gaps,"obj_face_line":line_num,"material":"Road",
                            "triangle_vertex_indices":[refs[0],refs[i],refs[i+1]],"barycentric_weights":weights,
                            "mesh_surface_z_m":z,"connector_top_z_m":segments[-1][3],"ground_z_m":ground}
    raise AssertionError("connector endpoint misses every Road triangle in the source campus mesh")


def live_geometry():
    """Gazebo server serializes its live entities; no generator or layout module is imported."""
    from gz.transport13 import Node
    from gz.msgs10.empty_pb2 import Empty
    from gz.msgs10.scene_pb2 import Scene
    from gz.msgs10.sdf_generator_config_pb2 import SdfGeneratorConfig
    from gz.msgs10.stringmsg_pb2 import StringMsg

    node = Node()
    ok, scene = node.request("/world/edge_lab/scene/info", Empty(), Empty, Scene, 10000)
    require(ok and scene.model, "active scene unavailable")
    config = SdfGeneratorConfig()
    config.global_entity_gen_config.expand_include_tags.data = True
    ok, response = node.request("/world/edge_lab/generate_world_sdf", config, SdfGeneratorConfig, StringMsg, 10000)
    require(ok and response.data, "live SDF unavailable")
    world = ET.fromstring(response.data).find("world")
    require(world is not None and world.get("name") == "edge_lab", "unexpected world")
    models = {m.get("name"):m for m in world.findall("model")}
    visible = {m.name: m for m in scene.model}
    require("edge_rover" in visible, "robot missing from renderer")
    report = {}
    for name in AREAS:
        require(name in models and name in visible, f"{name} missing in live SDF or renderer; {list(models)[:20]}")
        m = models[name]
        visuals = m.findall(".//visual")
        live_visuals = sum(len(link.visual) for link in visible[name].link)
        require(visuals and live_visuals, f"{name} has no visible geometry")
        shapes = {}
        for v in visuals:
            geom = v.find("geometry")
            if geom is not None and len(geom):
                kind = geom[0].tag
                shapes[kind] = shapes.get(kind,0)+1
        report[name] = {"world_pose":(m.findtext("pose") or "0 0 0 0 0 0").split()[:3],"visuals":live_visuals,
                        "collisions":len(m.findall(".//collision")),"geometry_types":shapes,
                        "mesh_uri_samples":[x.text for x in m.findall(".//visual/geometry/mesh/uri")][:3]}
        primitive_sizes=[(v.get("name"),[float(x) for x in v.findtext("geometry/box/size").split()])
                         for v in visuals if v.find("geometry/box/size") is not None]
        if primitive_sizes:
            largest=max(primitive_sizes, key=lambda entry: entry[1][0]*entry[1][1])
            report[name]["largest_box_size_m"] = largest
    require(report["fuel_campus"]["geometry_types"].get("mesh"), "upstream campus has no mesh")
    report["road_connection"] = road_connection(models)
    require(report["teaching"]["collisions"]>=2, "building cannot be physically sensed")
    for area, length in (("track",75),("lawn",90),("woods",75),("road",200),("teaching",12)):
        require(max(report[area]["largest_box_size_m"][1][:2])>=length, area+" is too small to represent campus scale")
    report["live_model_count"] = len(visible)
    report["pine_instances"] = len([name for name in visible if name.startswith("pine_")])
    require(report["pine_instances"]>2 and report["woods"]["collisions"]>=10,"woodland has too few physical trees")
    report["world_sdf_sha256"] = hashlib.sha256(response.data.encode()).hexdigest()
    print(json.dumps(report))


def direct_render():
    from gz.msgs10.image_pb2 import Image
    from gz.transport13 import Node

    # Compare RGB samples directly on the Gazebo transport against the independently retrieved PNGs.
    gz = Node()
    samples = {}
    def collect(name):
        def accept(msg):
            if msg.pixel_format_type != 3 or min(msg.width,msg.height)<180:
                return
            pixels = memoryview(msg.data)
            colors = [bytes(pixels[y*msg.step+x*3:y*msg.step+x*3+3])
                      for y in range(7,msg.height,23) for x in range(7,msg.width,23)]
            if len(set(colors))>=8:
                sample_bytes=b"".join(colors)
                samples[name] = {"sha256":hashlib.sha256(sample_bytes).hexdigest(),"rgb":base64.b64encode(sample_bytes).decode(),
                                 "width":msg.width,"height":msg.height,"stamp_ns":msg.header.stamp.sec*10**9+msg.header.stamp.nsec}
        return accept
    views=("track","woods")
    for name in views:
        require(gz.subscribe(Image,"/campus/view/"+name+"/image",collect(name)),"Gazebo camera subscription failed")
    await_fact(lambda:len(samples)==len(views),"independent live rendered RGB",70)
    print(json.dumps(samples))


def run(args):
    from map_model_isolation import copy_shipped_contracts
    token = uuid.uuid4().hex[:10]
    project, partition, domain = "edge-campus-audit-"+token, "campus_audit_"+token, str(156+secrets.randbelow(30))
    http = build_opener(ProxyHandler({}))
    def cmd(argv, *, env=None, timeout=120):
        ans = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", timeout=timeout)
        if ans.returncode:
            raise RuntimeError(f"{argv[:3]} returned {ans.returncode}: {ans.stderr[-600:]} {ans.stdout[-600:]}")
        return ans.stdout.strip()
    def identities(prefix):
        return dict(line.split(" ", 1) for line in cmd(["docker","ps","--no-trunc","--filter",
                    "label=com.docker.compose.project="+prefix,"--format",'{{.Label "com.docker.compose.service"}} {{.ID}}']).splitlines())
    def small_clock(services):
        raw = cmd(["docker","exec",services["sim"],"/usr/local/bin/edge-entrypoint","gz","topic","-e","-n","1","-t","/clock"],timeout=18)
        match = re.search(r"\bsim\s*\{([^}]+)\}", raw)
        require(match, "small world's clock missing")
        values = dict(re.findall(r"(\w+):\s*(\d+)", match[1]))
        return int(values.get("sec",0))*10**9+int(values.get("nsec",0))
    report={"passed":False,"project":project,"ros_domain":domain,"gazebo_partition":partition,
            "utc":datetime.now(timezone.utc).isoformat(),"image":args.image,"requested_text":"前往教学楼门外"}
    with tempfile.TemporaryDirectory(prefix="edge-campus-audit-") as name:
        tmp=Path(name)
        for directory in ("audit","plugins","contracts","observations"):
            (tmp/directory).mkdir()
        copy_shipped_contracts(tmp/"contracts")
        env={**os.environ,"EDGE_IMAGE":args.image,"EDGE_ROS_DOMAIN_ID":domain,"EDGE_CONSOLE_PORT":"0",
             "EDGE_CAMPUS_CONSOLE_PORT":"0","EDGE_CAMPUS_OVERVIEW_PORT":"0",
             "EDGE_OLLAMA_URL":"","EDGE_PLANNER_URL":"","EDGE_CONTRACTS_DIR":str(tmp/"contracts"),
             "EDGE_PLUGINS_DIR":str(tmp/"plugins"),"EDGE_OBSERVATIONS_DIR":str(tmp/"observations"),
             "EDGE_CAMPUS_ASSETS_DIR":str(args.assets.resolve())}
        overrides={svc:{"environment":{"GZ_PARTITION":partition,"ROS_DOMAIN_ID":domain}} for svc in (*SERVICES,"campus-capture")}
        overrides["campus-audit-witness"]={"image":args.image,"pull_policy":"never","init":True,
            "environment":{"GZ_PARTITION":partition,"ROS_DOMAIN_ID":domain,"RMW_IMPLEMENTATION":"rmw_cyclonedds_cpp"},
            "command":["python3","-B","/opt/audit/tests/system/campus_acceptance.py","--ros-witness"],
            "volumes":[{"type":"bind","source":str(Path(__file__).resolve()),"target":"/opt/audit/tests/system/campus_acceptance.py","read_only":True},
                       {"type":"bind","source":str(args.assets.resolve()),"target":"/assets","read_only":True},
                       {"type":"bind","source":str(tmp/"audit"),"target":"/audit"}]}
        write(tmp/"isolate.json",{"services":overrides})
        compose=["docker","compose","-p",project,"-f",str(ROOT/"compose.yaml"),"-f",str(ROOT/"compose.campus.yaml"),"-f",str(tmp/"isolate.json")]
        def dc(*params,timeout=140):
            return cmd([*compose,*params],env=env,timeout=timeout)
        def observed():
            return json.loads((tmp/"audit/ros.json").read_bytes())
        def api(path,body=None):
            req=Request(base+path, data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,
                        headers={"Content-Type":"application/json"})
            with http.open(req,timeout=6) as response:
                return json.load(response)
        small = identities(args.baseline)
        small_owned = not small
        # An empty developer machine gets its own isolated baseline; the daily sim is never mutated.
        baseline_dc=["docker","compose","-p",project+"-small","-f",str(ROOT/"compose.yaml")]
        small_env={**env,"EDGE_SCENE_FILE":"/opt/edge/scene/campus.json","EDGE_ROS_DOMAIN_ID":"146"}
        try:
            if small_owned:
                cmd(baseline_dc+["up","-d","--no-build","sim"],env=small_env)
                small=identities(project+"-small")
            require("sim" in small,"baseline project exists but has no running simulator")
            first_clock=small_clock(small)
            dc("up","-d","--no-build",*SERVICES,"campus-audit-witness","campus-capture","campus-overview",timeout=180)
            url=dc("port","console","8080")
            require(url.startswith("127.0.0.1:"),"console must bind to host loopback")
            base="http://"+url
            def ready():
                o=observed()
                return (o if min(o["counts"].values())>=4 and o["scan"] and o["truth"] and o["odom"]
                        and any(c["id"]=="navigate_place" for c in api("/agent/capabilities")["capabilities"]) else None)
            start=await_fact(ready,"Gazebo ROS streams and named-place capability",180)
            require(math.dist(start["truth"][-1][1:3],(0,0))<.18,"mission must begin inside teaching lobby")
            report["live_geometry"]=json.loads(dc("exec","-T","campus-audit-witness","/usr/local/bin/edge-entrypoint","python3","-B",
                                     "/opt/audit/tests/system/campus_acceptance.py","--live-geometry",timeout=35))
            key="campus-gate-"+token
            api("/agent/tasks",{"id":key,"text":report["requested_text"],"trigger":{"kind":"user","source":"campus-audit","id":key}})
            def terminal(key=key):
                row=api("/agent/tasks?"+urlencode({"id":key,"limit":1}))
                return row if row.get("state") in {"SUCCEEDED","BLOCKED","FAILED","CANCELED","RECOVERY_REQUIRED"} else None
            task=await_fact(terminal,"named-place task terminal",120)
            require(task["state"]=="SUCCEEDED",f"mission failed: {task.get('detail')}")
            require(task["steps"] and task["steps"][0]["tool"]=="navigate_place","unexpected task execution")
            evidence=task["steps"][0]["evidence"]
            require(evidence["verified_count"]==len(evidence["waypoints"])>0 and evidence["digest"],"missing execution evidence")
            def arrived():
                o=observed()
                return o if all(len(o[k])>=3 and all(math.dist(row[1:3],(3.3,0))<.20 and abs(row[3])<.08
                                for row in o[k][-3:]) for k in ("truth","odom")) else None
            end=await_fact(arrived,"independent world pose and odometry at outside goal",12)
            paths={}
            for kind in ("truth","odom"):
                trail=[row for row in end[kind] if row[0]>=start[kind][-1][0]]
                gate=[row for row in trail if abs(row[1]-1.6)<.16]
                require(gate and max(abs(row[2]) for row in gate)<.8 and math.dist(trail[0][1:3],(0,0))<.18,
                        "did not cross actual doorway: "+kind)
                paths[kind]={"start":trail[0],"inside_door":gate[len(gate)//2],"end":trail[-1],
                             "goal_error_m":round(math.dist(trail[-1][1:3],(3.3,0)),4),"samples":len(trail)}
            require(end["stamps"]["clock"]>start["stamps"]["clock"] and end["counts"]["scan"]>start["counts"]["scan"],
                    "clock or lidar stopped updating")
            report["mission"]={"task_id":key,"trace_id":task["trace_id"],"state":task["state"],"tool":"navigate_place",
                               "evidence":evidence,"paths":paths,"row_units":["stamp_ns","x_m","y_m","v_mps"]}
            report["ros"]={"counts_start":start["counts"],"counts_end":end["counts"],"first_scan":start["scan"][-1],
                           "last_scan":end["scan"][-1],"clock_before_ns":start["stamps"]["clock"],"clock_after_ns":end["stamps"]["clock"]}
            viewer = "http://"+dc("port","campus-overview","8080")
            def assets():
                with http.open(viewer+"/capture.json",timeout=3) as response:
                    receipt = json.load(response)
                return receipt if receipt["complete"] else None
            receipt = await_fact(assets,"all seven Gazebo camera views",260)
            with http.open(viewer+"/manifest.json",timeout=3) as response:
                manifest = json.load(response)
            declared={area["id"]:area for area in manifest["areas"]}
            require(set(declared)>={"campus","teaching","track","woods","road","lawn"} and manifest["unit"]=="m",
                    "six areas and metric map required")
            require(manifest["bounds"][2]-manifest["bounds"][0]>250 and manifest["bounds"][3]-manifest["bounds"][1]>200,
                    "world unexpectedly small")
            require(manifest["source_vertices"]>100, "upstream geometry missing")
            imgs={}
            direct=json.loads(dc("exec","-T","campus-audit-witness","/usr/local/bin/edge-entrypoint","python3","-B",
                                 "/opt/audit/tests/system/campus_acceptance.py","--direct-render",timeout=85))
            for label in ("overview",*declared):
                with http.open(viewer+"/"+label+".png",timeout=5) as response:
                    img=response.read(6*1024*1024)
                meta=receipt["views"][label]
                require(img.startswith(b"\x89PNG\r\n\x1a\n") and hashlib.sha256(img).hexdigest()==meta["sha256"]
                        and meta["topic"]==f"/campus/view/{label}/image" and meta["stamp_ns"]>0,
                        "stale or unproven rendering: "+label)
                width,height=struct.unpack_from(">II",img,16)
                # The capture contract specifies RGB8 with PNG filter 0. Inspect sampled original pixels.
                at=img.index(b"IDAT")
                body=zlib.decompress(img[at+4:at+4+struct.unpack_from(">I",img,at-4)[0]])
                stride=1+3*width
                require(len(body)==height*stride and width>=300 and height>=180,"invalid render layout")
                colors={body[y*stride+1+3*x:y*stride+4+3*x] for y in range(0,height,9) for x in range(0,width,9)}
                require(len(colors)>=8,"unusable blank render: "+label)
                imgs[label]={**meta,"sampled_color_count":len(colors)}
                if label in direct:
                    actual = direct[label]
                    sample_bytes = b"".join(body[y*stride+1+x*3:y*stride+4+x*3]
                                            for y in range(7,height,23) for x in range(7,width,23))
                    reference=base64.b64decode(actual.pop("rgb"),validate=True)
                    require((width,height)==(actual["width"],actual["height"]) and len(sample_bytes)==len(reference),
                            "live static camera raster dimensions changed: "+label)
                    matched=sum(max(abs(sample_bytes[i+k]-reference[i+k]) for k in range(3))<=4
                                for i in range(0,len(reference),3))
                    actual["matching_sample_fraction"]=round(matched/(len(reference)//3),5)
                    require(actual["matching_sample_fraction"]>=.97,
                            "archive pixels disagree with live static view: "+label)
                    imgs[label]["independent_gz"] = actual
            require(len({img["sha256"] for img in imgs.values()})==len(imgs),"duplicate region views")
            report["renders"]={"world_digest":receipt["world"],"views":imgs,"declared_areas":list(declared.values()),
                               "bounds_m":manifest["bounds"],"fuel_vertices":manifest["source_vertices"]}
            active=identities(project)
            first=observed()["stamps"].copy()
            dc("stop","campus-overview")
            def survived():
                now=observed()["stamps"]
                return now if all(now[k]>first[k] for k in first) else None
            advanced=await_fact(survived,"ROS and timing remain active with the campus viewer stopped",15)
            dc("start","campus-overview")
            # Docker may assign a new host port when restarting a service published on ephemeral port 0.
            viewer = "http://"+dc("port","campus-overview","8080")
            await_fact(assets,"restored viewer shows its original capture",15)
            require(all(identities(project).get(svc)==active[svc] for svc in ("sim","gateway","agent")),"viewer affected execution")
            report["viewer_restart"]={"unchanged":["sim","gateway","agent"],"before_ns":first,"after_ns":advanced}
            if args.verify_reload:
                sys.path.insert(0,str(ROOT))
                from scene.campus_large.run import reload_campus
                old_session=api("/api/state")["session_id"]
                reload_campus(dc,dc,lambda action,*ids:cmd(["docker",action,*ids],env=env))
                base="http://"+dc("port","console","8080")
                def fresh_origin():
                    witness=ready()
                    if not witness or math.dist(witness["truth"][-1][1:3],(0,0))>=.18:
                        return None
                    session=api("/api/state")["session_id"]
                    return session if session!=old_session else None
                new_session=await_fact(fresh_origin,"a new campus clock session and independent rover at origin",60)
                previous=terminal()
                require(previous["state"]=="SUCCEEDED" and previous["steps"][0]["evidence"]==evidence,
                        "world reload changed earlier mission evidence")
                second=key+"-after-reload"
                api("/agent/tasks",{"id":second,"text":report["requested_text"],
                                    "trigger":{"kind":"user","source":"campus-audit","id":second}})
                result=await_fact(lambda:terminal(second),"resubmitted door crossing after world reload",120)
                require(result["state"]=="SUCCEEDED",f"second mission failed: {result.get('detail')}")
                proof=result["steps"][0]["evidence"]
                require(result["steps"][0]["tool"]=="navigate_place" and proof["verified_count"]==len(proof["waypoints"])>0,
                        "second campus task missed navigation evidence")
                after_reload=await_fact(arrived,"new-clock ground truth and odometry agree at the outside target",15)
                report["guarded_reload"]={"previous_session":old_session,"new_session":new_session,"old_task_id":key,
                    "old_evidence_preserved":True,"task_id":second,"state":result["state"],
                    "verified_count":proof["verified_count"],"truth":after_reload["truth"][-1],"odom":after_reload["odom"][-1]}
            dc("down","--volumes","--remove-orphans","--timeout","5")
            current=identities(project+"-small" if small_owned else args.baseline)
            require(all(current.get(svc)==cid for svc,cid in small.items()),"campus lifecycle replaced baseline services")
            clock_after=small_clock(current)
            require(clock_after>first_clock,"baseline simulation stopped progressing")
            report["baseline"]={"owned_fixture":small_owned,"services_unchanged":sorted(small),"sim_id":small["sim"],
                                "clock_before_ns":first_clock,"clock_after_ns":clock_after}
            report["passed"]=True
        except Exception as error:
            report["error"]=str(error)[:1500]
            try:
                report["services"]=dc("ps","-a","--format","table {{.Service}} {{.Status}}")
                report["diagnostic"]={name:dc("logs","--no-color","--tail","3",name)[-700:] for name in ("campus-build","sim","scene","agent","campus-audit-witness")}
            except Exception:
                pass
            raise
        finally:
            try:
                dc("down","--volumes","--remove-orphans","--timeout","5")
            finally:
                if small_owned:
                    cmd(baseline_dc+["down","--volumes","--timeout","5"],env=small_env)
                args.out.parent.mkdir(parents=True,exist_ok=True)
                args.out.write_text(json.dumps(report,indent=2,ensure_ascii=False)+"\n",encoding="utf-8")


if __name__=="__main__":
    if sys.argv[1:]==["--ros-witness"]:
        ros_witness()
    elif sys.argv[1:]==["--live-geometry"]:
        live_geometry()
    elif sys.argv[1:]==["--direct-render"]:
        direct_render()
    else:
        parser=argparse.ArgumentParser(description=__doc__)
        parser.add_argument("--image",required=True)
        parser.add_argument("--baseline",default="edge-agent-ros")
        parser.add_argument("--verify-reload",action="store_true",help="cross the door again after guarded campus reload")
        parser.add_argument("--assets",type=Path,default=ROOT/"artifacts/sim-assets")
        parser.add_argument("--out",type=Path,required=True)
        run(parser.parse_args())
