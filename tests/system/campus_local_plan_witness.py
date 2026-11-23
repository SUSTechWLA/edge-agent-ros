"""Read-only SDD-056 observer and offline geometry scorer for a single campus trial."""
import argparse
from bisect import bisect_left
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import sys
from threading import Lock
import time
from xml.etree import ElementTree as ET

from campus_route_geometry import dist_point, obstacles, poly_distance
from campus_crossing_fixture import CART, TRUTH

LINKS = ("base_link", "left_wheel", "right_wheel", "rear_caster")
LINGAO_WHEELS = ("front_left_wheel", "front_right_wheel", "rear_left_wheel", "rear_right_wheel")
LINGAO_LINKS = ("base_link", "payload", "front_mid360", "rear_mid360", *LINGAO_WHEELS)


def write(path, value):
    tmp = path.with_suffix(".part")
    tmp.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2), encoding="utf8")
    tmp.replace(path)


def ns(stamp):
    return stamp.sec * 10**9 + (stamp.nanosec if hasattr(stamp, "nanosec") else stamp.nsec)


def angle(value):
    return math.atan2(math.sin(value), math.cos(value))


def rotate(v, yaw):
    c, s = math.cos(yaw), math.sin(yaw)
    return c*v[0]-s*v[1], s*v[0]+c*v[1]


def transform(point, xy, yaw):
    x, y = rotate(point, yaw)
    return (xy[0]+x, xy[1]+y)


def pose6(element):
    return tuple(map(float, element.findtext("pose","0 0 0 0 0 0").split()))


def rotate3(point, rpy):
    """Apply SDF's fixed-axis roll, pitch, yaw to a vector."""
    x,y,z=point
    roll,pitch,yaw=rpy
    y,z=rotate((y,z),roll)
    z,x=rotate((z,x),pitch)
    x,y=rotate((x,y),yaw)
    return x,y,z


def reference_geometry(node, world, out):
    from gz.msgs10.sdf_generator_config_pb2 import SdfGeneratorConfig
    from gz.msgs10.stringmsg_pb2 import StringMsg
    cfg = SdfGeneratorConfig()
    cfg.global_entity_gen_config.expand_include_tags.data = True
    endpoint="/world/"+world+"/generate_world_sdf"
    deadline=time.monotonic()+60.
    attempts=0
    while time.monotonic()<deadline:
        attempts+=1
        remaining=max(1,int((deadline-time.monotonic())*1000))
        ok,result=node.request(endpoint,cfg,SdfGeneratorConfig,StringMsg,min(10000,remaining))
        if ok and result.data:
            break
        time.sleep(min(.5,max(0.,deadline-time.monotonic())))
    else:
        raise RuntimeError(f"Gazebo Transport did not return {endpoint} within 60 s ({attempts} attempts)")
    raw = result.data.encode()
    (out/"world.sdf").write_bytes(raw)
    return geometry_from_sdf(raw, world)


def geometry_from_sdf(raw,world):
    root = ET.fromstring(raw).find("world")
    models = {m.get("name"):m for m in root.findall("model")}
    rover = models["edge_rover"]
    origin = pose6(rover)[:2]
    pieces = []
    topics = {}
    highest = 0.
    rover_z = pose6(rover)[2]
    for link in rover.findall("link"):
        for col in link.findall("collision"):
            lx, ly, lz, _, _, la = pose6(link)
            cx, cy, cz, _, _, ca = pose6(col)
            center = transform((cx,cy), (lx,ly), la)
            box = col.findtext("geometry/box/size")
            rad = col.findtext("geometry/sphere/radius")
            cylinder = col.find("geometry/cylinder")
            if box:
                w,h,height = map(float,box.split())
                highest=max(highest,rover_z+lz+cz+height/2)
                poly = [transform((i*w/2,j*h/2),center,la+ca) for i,j in ((-1,-1),(1,-1),(1,1),(-1,1))]
                pieces.append({"link":link.get("name"),"poly":poly,"radius":0})
            elif rad:
                highest=max(highest,rover_z+lz+cz+float(rad))
                pieces.append({"link":link.get("name"),"poly":[center],"radius":float(rad)})
            elif cylinder is not None:
                # Rotation around the axle leaves the wheel cylinder unchanged.
                # Project its oriented axial extent and circular ends onto XY.
                r=float(cylinder.findtext("radius"))
                half=float(cylinder.findtext("length"))/2
                ax,ay,az=rotate3(rotate3((0,0,1),pose6(col)[3:]),pose6(link)[3:])
                horizontal=math.hypot(ax,ay)
                if horizontal < .999:
                    raise ValueError("expected a nearly horizontal wheel axle")
                ux,uy=ax/horizontal,ay/horizontal
                a=half*horizontal+r*abs(az)
                poly=[(center[0]+i*a*ux-j*r*uy,center[1]+i*a*uy+j*r*ux)
                      for i,j in ((-1,-1),(1,-1),(1,1),(-1,1))]
                highest=max(highest,rover_z+lz+cz+r*horizontal+half*abs(az))
                pieces.append({"link":link.get("name"),"poly":poly,"radius":0})
            else:
                raise ValueError("unhandled rover collision "+col.get("name"))
            sensor=link.find("sensor[@type='contact']/contact")
            if sensor is None or sensor.findtext("collision")!=col.get("name") or not sensor.findtext("topic"):
                raise ValueError("missing contact sensor for "+link.get("name"))
            topics[link.get("name")]=sensor.findtext("topic")
    if set(topics) not in (set(LINKS),set(LINGAO_LINKS)) or len(set(topics.values()))!=len(pieces):
        raise ValueError("incomplete runtime rover collision geometry")
    laser = rover.find(".//sensor[@name='horizontal_lidar']")
    laser_pose=pose6(laser)
    # Campus survey helper clips static collision meshes to the rover-height slab.
    # Include the three parked campus robots as obstacles using their measured initial poses.
    parked = {}
    for name,m in models.items():
        if name not in ("edge_rover",CART) and m.findtext("static","false") not in ("1","true"):
            parked[name]=list(pose6(m)[:2])
            static=m.find("static")
            if static is None:
                static=ET.SubElement(m,"static")
            static.text="true"
    def local_shapes(slab):
        result=[]
        for name,poly,radius,(x0,y0,x1,y1) in obstacles(models,Path("/campus"),slab):
            if x1>=origin[0]-12 and x0<=origin[0]+12 and y1>=origin[1]-12 and y0<=origin[1]+12:
                result.append({"name":name,"poly":poly,"radius":radius})
        if not result:
            raise ValueError("empty local collision geometry")
        return result
    # Wheels can meet obstacles below the old route audit height of 0.065 m.
    # Beams see a different slice of the same world. Determine each from SDF.
    slab=(0.0001,highest+.01)
    shapes=local_shapes(slab)
    laser_height=rover_z+laser_pose[2]
    laser_shapes=local_shapes((laser_height-.001,laser_height+.001))
    boxes={}
    for name,m in models.items():
        candidates=m.findall("./link/collision/geometry/box/size")
        if len(candidates)==1:
            boxes[name] = list(map(float,candidates[0].text.split()))
    moving={}
    if CART in models:
        cart=models[CART].find("link[@name='cart']")
        dims=tuple(map(float,cart.findtext("collision/geometry/box/size").split()))
        if pose6(cart)!=(0,)*6 or pose6(cart.find("collision"))!=(0,)*6:
            raise ValueError("the witness expects the crossing shape at its link origin")
        moving={"model":CART,"size_m":dims,"initial_pose":pose6(models[CART]),
                "poly":[(i*dims[0]/2,j*dims[1]/2) for i,j in ((-1,-1),(1,-1),(1,1),(-1,1))]}
    return {"world":world,"sdf_sha256":hashlib.sha256(raw).hexdigest(),"origin":origin,
            "laser_xy":laser_pose[:2],"laser_yaw":laser_pose[5],"footprint":pieces,"obstacles":shapes,"laser_obstacles":laser_shapes,
            "box_sizes":boxes,"body_z_slab":slab,"parked_models":parked,"geometry_radius_m":12,
            "contact_topics":topics,"moving_obstacle":moving}


def record(out, world):
    import rclpy
    from rclpy.qos import qos_profile_sensor_data
    from nav_msgs.msg import Odometry
    from sensor_msgs.msg import Imu, LaserScan
    from rosgraph_msgs.msg import Clock
    from std_msgs.msg import String
    from gz.transport13 import Node
    from gz.msgs10.contacts_pb2 import Contacts
    from gz.msgs10.odometry_pb2 import Odometry as GzOdom
    out.mkdir(parents=True,exist_ok=True)
    gz = Node()
    geometry=reference_geometry(gz,world,out)
    write(out/"geometry.json", geometry)
    rclpy.init()
    node = rclpy.create_node("local_plan_truth_witness")
    counts=Counter()
    lock=Lock()
    wall_start=time.monotonic()
    current=[0]
    with (out/"observations.jsonl").open("x",encoding="utf8",buffering=65536) as stream:
        def emit(kind,stamp,**fields):
            row=dict(kind=kind,stamp_ns=stamp,**fields)
            with lock:
                counts[kind]+=1
                stream.write(json.dumps(row,allow_nan=False,separators=(",",":"))+"\n")
        def odom(kind,msg):
            p=msg.pose.pose.position
            q=msg.pose.pose.orientation
            yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
            emit(kind,ns(msg.header.stamp),xy=[p.x,p.y],yaw=yaw,frame=msg.header.frame_id,
                 v=msg.twist.twist.linear.x,w=msg.twist.twist.angular.z)
        def scan(m):
            # JSON has no infinity. Preserve sensor conventions including -Inf and missing rays.
            ranges=[round(v,5) if math.isfinite(v) else ("+" if v>0 else "-" if v<0 else None) for v in m.ranges]
            emit("scan",ns(m.header.stamp),start=m.angle_min,step=m.angle_increment,
                 range_min=m.range_min,range_max=m.range_max,frame=m.header.frame_id,ranges=ranges)
        def clock(m):
            current[0]=ns(m.clock)
            emit("clock",current[0])
        def contacts(link,m):
            pairs=[[c.collision1.name,c.collision2.name,c.collision1.id,c.collision2.id] for c in m.contact]
            emit("contacts",ns(m.header.stamp),link=link,pairs=pairs)
        node.create_subscription(Clock,"/clock",clock,qos_profile_sensor_data)
        for topic,kind in [("/ground_truth","truth"),("/odom","odom")]:
            node.create_subscription(Odometry,topic,lambda m,k=kind:odom(k,m),qos_profile_sensor_data)
        if set(geometry["contact_topics"])==set(LINGAO_LINKS):
            node.create_subscription(Odometry,"/lingao/wheel_odom",lambda m:odom("wheel",m),qos_profile_sensor_data)
            node.create_subscription(Imu,"/imu",lambda m:emit("imu",ns(m.header.stamp),w=m.angular_velocity.z,
                                     frame=m.header.frame_id),qos_profile_sensor_data)
            def corrected(msg):
                data=json.loads(msg.data)
                emit("scan_correction",data.pop("stamp_ns"),**data)
            node.create_subscription(String,"/lingao/scan_correction",corrected,20)
        node.create_subscription(LaserScan,"/scan",scan,qos_profile_sensor_data)
        node.create_subscription(String,"/edge/events",lambda m:emit("event",current[0],event=json.loads(m.data)),100)
        def report(m):
            value=json.loads(m.data)
            emit("report",value["stamp_ns"],report=value)
        node.create_subscription(String,"/edge/perception/front_obstacle",report,50)
        def dynamic_report(m):
            value=json.loads(m.data)
            emit("dynamic_report",value["stamp_ns"],report=value)
        node.create_subscription(String,"/edge/perception/dynamic_obstacles",dynamic_report,50)
        for link,topic in geometry["contact_topics"].items():
            if not gz.subscribe(Contacts,topic,lambda m,k=link:contacts(k,m)):
                raise RuntimeError("contact subscription failed for "+link)
        expected=["clock","truth","scan","odom"]
        if geometry["moving_obstacle"]:
            def actor(m):
                p,q=m.pose.position,m.pose.orientation
                yaw=math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
                emit("actor",ns(m.header.stamp),xy=[p.x,p.y],yaw=yaw,vx=m.twist.linear.x,vy=m.twist.linear.y)
            def actor_contacts(m):
                emit("actor_contacts",ns(m.header.stamp),pairs=[[c.collision1.name,c.collision2.name] for c in m.contact])
            if not gz.subscribe(GzOdom,TRUTH,actor) or not gz.subscribe(Contacts,"/fixture/crossing/cart_contacts",actor_contacts):
                raise RuntimeError("crossing truth or contact subscription failed")
            expected.append("actor")
        try:
            while not (out/"finish").exists():
                rclpy.spin_once(node,timeout_sec=.05)
                if not (out/"ready.json").exists() and all(counts[k]>=3 for k in expected):
                    write(out/"ready.json",dict(counts))
                if time.monotonic()-wall_start>1200:
                    raise TimeoutError("no finish signal")
        finally:
            for topic in geometry["contact_topics"].values():
                gz.unsubscribe(topic)
            if geometry["moving_obstacle"]:
                gz.unsubscribe(TRUTH);gz.unsubscribe("/fixture/crossing/cart_contacts")
            node.destroy_node()
            rclpy.try_shutdown()
            with lock:
                stream.flush()
    write(out/"complete.json",dict(counts))


def ray_polygon(origin,heading,poly):
    """Travel along a ray to a polygon's first visible face."""
    dx,dy=math.cos(heading),math.sin(heading)
    best=None
    for a,b in zip(poly,poly[1:]+poly[:1]):
        ex,ey=b[0]-a[0],b[1]-a[1]
        det=dx*ey-dy*ex
        if abs(det)<1e-10:
            continue
        ax,ay=a[0]-origin[0],a[1]-origin[1]
        r=(ax*ey-ay*ex)/det
        u=(ax*dy-ay*dx)/det
        if r>0 and 0<=u<=1 and (best is None or r<best[0]):
            best=(r,(origin[0]+dx*r,origin[1]+dy*r))
    return best


def nearest_segment(a,b):
    dx,dy=b[0]-a[0],b[1]-a[1]
    den=dx*dx+dy*dy
    t=max(0.,min(1.,-(a[0]*dx+a[1]*dy)/den)) if den else 0.
    return a[0]+t*dx,a[1]+t*dy


def circle_hit(origin, heading, center, radius):
    dx,dy=math.cos(heading),math.sin(heading)
    x,y=center[0]-origin[0],center[1]-origin[1]
    projection=x*dx+y*dy
    delta=radius*radius-(x*dy-y*dx)**2
    if delta<0 or projection<=0:
        return None
    return max(0.,projection-math.sqrt(delta))


def box_reference(scan, xy, yaw, geometry, world_box, resolution=None):
    """Raycast the known world at every scan angle; measurements never select their own test frames."""
    def local(p):
        return rotate((p[0]-xy[0],p[1]-xy[1]),-yaw)
    box=[local(p) for p in world_box]
    laser=geometry["laser_xy"]
    upper=min(scan["range_max"],max(math.dist(laser,p) for p in box))
    others=[]
    for shape in geometry["laser_obstacles"]:
        if shape["poly"]==world_box:
            continue
        poly=[local(p) for p in shape["poly"]]
        nearest=min(dist_point(laser,a,b) for a,b in zip(poly,poly[1:]+poly[:1]))-shape["radius"]
        if nearest<=upper+.2:
            others.append((poly,shape["radius"],shape["name"]))
    groups=[]
    ambient=math.inf
    ambient_returns={}
    hits=0
    for i in range(len(scan["ranges"])):
        a=scan["start"]+i*scan["step"]+geometry["laser_yaw"]
        hit=ray_polygon(laser,a,box)
        competing=math.inf
        other_name=None
        for shape,radius,name in others:
            if len(shape)==1:
                d=circle_hit(laser,a,shape[0],radius)
            else:
                surface=ray_polygon(laser,a,shape)
                d=surface[0] if surface else None
            if d is not None and d<competing:
                competing,other_name=d,name
        if math.isfinite(competing):
            p=(laser[0]+competing*math.cos(a),laser[1]+competing*math.sin(a))
            if (scan["range_min"]<=competing<=scan["range_max"] and abs(math.atan2(p[1],p[0]))<=math.pi/4
                    and (hit is None or competing<hit[0])):
                ambient=min(ambient,math.hypot(*p))
                if resolution is not None:
                    ambient_returns.setdefault(other_name,[]).append((i,p))
        if (not hit or hit[0]>min(scan["range_max"],competing) or hit[0]<scan["range_min"]
                or abs(math.atan2(hit[1][1],hit[1][0]))>math.pi/4):
            continue
        hits+=1
        if not groups or i!=groups[-1][-1][0]+1:
            groups.append([])
        groups[-1].append((i,hit[1]))
    points=[]
    for group in groups:
        points.extend(p for _,p in group)
        points.extend(nearest_segment(a[1],b[1]) for a,b in zip(group,group[1:]))
    if not points:
        return None
    p=min(points,key=lambda p:(math.hypot(*p),abs(math.atan2(p[1],p[0]))))
    distance=math.hypot(*p)
    if resolution is None and ambient<distance:
        return None
    bearing=math.atan2(p[1],p[0])
    if resolution is None:
        return distance,bearing,hits
    # Candidate membership is decided entirely from raycast geometry before
    # viewing a measurement. Both faces must physically return visible beams.
    candidates=[{"shape":CART,"range_m":distance,"bearing_rad":bearing,"rays":hits}]
    for name,returns in ambient_returns.items():
        points=[p for _,p in returns]
        points.extend(nearest_segment(a,b) for (i,a),(j,b) in zip(returns,returns[1:]) if i+1==j)
        closest=min(points,key=lambda q:(math.hypot(*q),abs(math.atan2(q[1],q[0]))))
        candidates.append({"shape":name,"range_m":math.hypot(*closest),
                           "bearing_rad":math.atan2(closest[1],closest[0]),"rays":len(returns)})
    minimum=min(c["range_m"] for c in candidates)
    if distance-minimum>resolution:
        return None
    return [c for c in candidates if c["range_m"]-minimum<=resolution]


def matching_face(candidates,measured_range,measured_bearing):
    if not isinstance(measured_range,(int,float)) or not isinstance(measured_bearing,(int,float)):
        return None
    def error(c):
        return abs(angle(measured_bearing-c["bearing_rad"]))
    possible=[c for c in candidates if abs(measured_range-c["range_m"])<=max(.05,.05*c["range_m"])
              and math.degrees(error(c))<=2.]
    return min(possible,key=error,default=None)

def clearance(pose,geometry):
    best=(math.inf,None)
    for part in geometry["footprint"]:
        poly=[transform(p,pose["xy"],pose["yaw"]) for p in part["poly"]]
        for obs in geometry["obstacles"]:
            shape=obs["poly"]
            if len(shape)==1:
                d=poly_distance(shape[0],shape[0],poly)
            else:
                d=min(poly_distance(p,q,shape) for p,q in zip(poly,poly[1:]+poly[:1]))
            d-=part["radius"]+obs["radius"]
            if d<best[0]:
                best=(d,obs["name"])
    return best


def box_clearances(row,geometry,box):
    distances={}
    for part in geometry["footprint"]:
        vertices=[transform(p,row["xy"],row["yaw"]) for p in part["poly"]]
        edges=list(zip(vertices,vertices[1:]+vertices[:1]))
        values=[poly_distance(a,b,box) for a,b in edges]
        distances[part["link"]]=min(values)-part["radius"]
        if part["link"]=="base_link" and len(edges)==4:
            # Local box vertices are ordered rear-right, front-right,
            # front-left, rear-left. Corners belong to both adjacent edges.
            distances.update(zip(("body_right","body_front","body_left","body_rear"),values))
    return distances


def interpolate(rows,stamps,stamp):
    n=bisect_left(stamps,stamp)
    if n<len(rows) and stamps[n]==stamp:return rows[n]
    if not 0<n<len(rows):return None
    a,b=rows[n-1],rows[n];u=(stamp-stamps[n-1])/(stamps[n]-stamps[n-1])
    return {"xy":[x+(y-x)*u for x,y in zip(a["xy"],b["xy"])],"yaw":a["yaw"]+u*angle(b["yaw"]-a["yaw"])}


def max_gap(rows):
    return max(((b["stamp_ns"]-a["stamp_ns"])*1e-9 for a,b in zip(rows,rows[1:])),default=None)


def track_errors(track,actor_pose,origin_xy,origin_yaw,dimensions):
    # Tracker reports the centroid of visible beam endpoints. That point can
    # be anywhere on the measured cart, including its nearest visible face.
    position=transform((track["x_m"],track["y_m"]),origin_xy,origin_yaw)
    local=rotate((position[0]-actor_pose["xy"][0],position[1]-actor_pose["xy"][1]),-actor_pose["yaw"])
    distance=math.hypot(max(0.,abs(local[0])-dimensions[0]/2),max(0.,abs(local[1])-dimensions[1]/2))
    vx,vy=rotate((track["vx_mps"],track["vy_mps"]),origin_yaw)
    physical_speed=math.hypot(actor_pose["vx"],actor_pose["vy"])
    projected_speed=(vx*actor_pose["vx"]+vy*actor_pose["vy"])/physical_speed
    return distance,projected_speed-physical_speed,math.hypot(vx-actor_pose["vx"],vy-actor_pose["vy"])


def dynamic_evidence(events,dynamic,front_refs,check):
    captures={r["report"]["report_ref"]:r["report"] for r in dynamic}
    observations={e["data"]["dynamic"]["report_ref"]:e["data"]["dynamic"] for e in events
                  if e["code"]=="perception.dynamic_obstacles"}
    check("dynamic_topic_matches_events",bool(observations) and all(captures.get(ref)==r for ref,r in observations.items()))
    check("dynamic_scan_binding",bool(observations) and all(r["frame_id"]=="odom" and r["scan_ref"] in front_refs
          and r["stamp_ns"]==front_refs[r["scan_ref"]]["stamp_ns"] and r["scan_seq"]==front_refs[r["scan_ref"]]["scan_seq"]
          for r in observations.values()))
    return observations


def static_dynamic_behavior(events,plans,reports):
    phases=Counter(p.get("dynamic_phase") or "inactive" for p in plans)
    first=min((reports[p["report_ref"]]["stamp_ns"] for p in plans if p.get("report_ref") in reports),default=None)
    questionable=[]
    verified=Counter()
    for i,p in enumerate(plans):
        if p.get("decision")!="wait" and p.get("dynamic_phase") not in ("observing","unobserved","yielding"):
            continue
        r=reports.get(p.get("report_ref"))
        reason=p.get("reason_code")
        stopped=p.get("linear_mps")==0 and p.get("angular_radps")==0
        justified=False
        if stopped and r and reason=="navigation.dynamic_observing":
            justified=p.get("dynamic_phase")=="observing" and first is not None and r["stamp_ns"]-first<=900_000_000
        elif stopped and r and not p.get("dynamic_phase"):
            if reason=="navigation.ttc_unobserved":
                justified=r.get("ttc_status")=="unobserved" and r.get("ttc_s") is None and p.get("ttc_s") is None
            elif reason=="navigation.reassessing_corridor" and r.get("valid") and r.get("swept_coverage"):
                # At a restricted passage an active bypass may pause and
                # reassess, with both neighbouring decisions on fresh scans.
                previous=next((v for v in reversed(plans[:i]) if v.get("decision")!="wait"),{})
                following=next((v for v in plans[i+1:] if v.get("decision")!="wait"),{})
                before=reports.get(previous.get("report_ref"),{}).get("stamp_ns")
                after=reports.get(following.get("report_ref"),{}).get("stamp_ns")
                justified=(previous.get("decision") in ("avoid_left","avoid_right") and
                           following.get("decision") in ("avoid_left","avoid_right","proceed") and
                           before is not None and after is not None and before<=r["stamp_ns"]<=after and 0<after-before<=500_000_000)
        if justified:
            verified[reason]+=1
        else:
            questionable.append(p.get("report_ref"))
    false_claims=[e["code"] for e in events if e["code"] in ("navigation.dynamic_yield","navigation.dynamic_clear")]
    return {"phases":dict(phases),"verified_wait_reasons":dict(verified),"unexplained_wait_refs":questionable,"dynamic_transition_events":false_claims,
            "passed":bool(plans) and not questionable and not false_claims and
                     not any(p.get("dynamic_phase") in ("yielding","cleared") for p in plans)}


def score_crossing(geometry,actor,truth,first,last,args,events,contacts,out,check,odom,dynamic,front_refs):
    """All crossing checks use independent physical poses and the linked operation."""
    in_task=[p for p in truth if first<=p["stamp_ns"]<=last]
    actor_ts=[p["stamp_ns"] for p in actor]
    check("moving_truth_coverage",len(actor)>5 and actor_ts[0]<=first and actor_ts[-1]>=last and
          all(a<b for a,b in zip(actor_ts,actor_ts[1:])) and max_gap(actor)<.10)
    check("cart_contact_free",not any(r["pairs"] for r in contacts))
    motion=out/"cart-motion.json"
    proof=json.loads(motion.read_bytes()) if motion.exists() else {}
    check("cart_command_bound",proof.get("operation")==args.operation and proof.get("actuator_topic")=="/model/crossing_cart/joint/guide/cmd_vel")
    if not in_task or not geometry["moving_obstacle"] or len(actor)<2:
        check("moving_obstacle_measured",False)
        return {}
    box_x,box_y,_=geometry["moving_obstacle"]["size_m"]
    footprint=geometry["footprint"]
    rover_front=max(x+p["radius"] for p in footprint for x,y in p["poly"])
    rover_side=max(abs(y)+p["radius"] for p in footprint for x,y in p["poly"])
    center_x=actor[0]["xy"][0]
    speeds=[math.dist(a["xy"],b["xy"])/((b["stamp_ns"]-a["stamp_ns"])*1e-9)
            for a,b in zip(actor,actor[1:]) if b["stamp_ns"]>a["stamp_ns"]]
    check("cart_motion_physical",actor[0]["xy"][1]<-.30 and max(r["xy"][1] for r in actor)>1.1 and
          max(abs(r["xy"][0]-center_x) for r in actor)<.03 and max(speeds,default=0.)<=.18 and max(speeds,default=0.)>.09)
    # Compare arrival time against the same straight 0.28 m/s point task
    # without yielding. The cart really occupied the shared swept corridor at
    # that time, independent of whether it was reported as a moving obstacle.
    predicted_ns=first+int((center_x-args.start[0])/.28*1e9)
    predicted=interpolate(actor,actor_ts,predicted_ns)
    check("unyielded_route_conflict",predicted is not None and
          abs(predicted["xy"][1]-args.start[1])<=box_y/2+rover_side)
    cleared_ns=next((p["stamp_ns"] for p in actor if first<=p["stamp_ns"]<=last and
                    p["xy"][1]-box_y/2-args.start[1]-rover_side>=.45),None)
    cleared_x=center_x-box_x/2-rover_front
    passed_ns=next((p["stamp_ns"] for p in in_task if p["xy"][0]>=cleared_x),None)
    check("cart_clears_before_rover_enters",cleared_ns is not None and passed_ns is not None and cleared_ns<passed_ns)
    longest=stopped=0.
    for a,b in zip(in_task,in_task[1:]):
        dt=(b["stamp_ns"]-a["stamp_ns"])*1e-9
        if dt<=0:continue
        pre_entry=max(a["xy"][0],b["xy"][0])<cleared_x
        waiting=cleared_ns is not None and proof.get("start_ns",first)<=a["stamp_ns"]<=cleared_ns
        if pre_entry and waiting and math.dist(a["xy"],b["xy"])/dt<.035 and abs(angle(b["yaw"]-a["yaw"]))/dt<.10:
            stopped+=dt;longest=max(longest,stopped)
        else:stopped=0.
    check("physical_yield",longest>=.5)
    codes=[e["code"] for e in events]
    yields=[i for i,c in enumerate(codes) if c=="navigation.dynamic_yield"]
    clears=[i for i,c in enumerate(codes) if c=="navigation.dynamic_clear"]
    check("same_operation_yield_then_clear",bool(yields and clears) and min(yields)<max(clears) and
          "perception.dynamic_obstacles" in codes)
    observations=dynamic_evidence(events,dynamic,front_refs,check)
    plans=[events[i]["data"]["plan"] for i in yields+clears]
    ids={p["dynamic_track_id"] for p in plans}
    check("same_tracked_object",bool(yields and clears) and len(ids)==1 and 0 not in ids and
          all(p["dynamic_report_ref"] in observations for p in plans) and
          all(events[i]["data"]["plan"]["dynamic_clear_samples"]>=3 for i in clears))
    active=[observations[p["dynamic_report_ref"]]["stamp_ns"] for p in plans if p["dynamic_report_ref"] in observations]
    release=[observations[events[i]["data"]["plan"]["dynamic_report_ref"]]["stamp_ns"] for i in clears
             if events[i]["data"]["plan"]["dynamic_report_ref"] in observations]
    check("physical_corridor_clear_at_release",cleared_ns is not None and bool(release) and all(t>=cleared_ns for t in release))
    vel_errors=[]; transverse_errors=[]; spatial_errors=[]; valid_matches=eligible_scans=0
    odom_origin=next((o for o in odom if truth[0]["stamp_ns"]<=o["stamp_ns"]<=first),None)
    world_origin=interpolate(truth,[r["stamp_ns"] for r in truth],odom_origin["stamp_ns"]) if odom_origin else None
    if world_origin and odom_origin and len(ids)==1 and active:
        yaw=world_origin["yaw"]-odom_origin["yaw"]
        offset=rotate(odom_origin["xy"],yaw)
        origin=[world_origin["xy"][i]-offset[i] for i in (0,1)]
        for r in observations.values():
            stamp=r["stamp_ns"]
            # The native cart pose/twist nearest to a scan is at most 50 ms
            # apart. Grade only genuinely moving observations, away from the
            # initial acceleration and final braking intervals of the cart.
            if not max(min(active),proof.get("start_ns",first)+1e9)<=stamp<=min(max(active),proof.get("stop_ns",last)-.5e9):
                continue
            n=bisect_left(actor_ts,stamp)
            if n>=len(actor) or actor_ts[n]-stamp>50e6:
                continue
            physical=actor[n]
            if math.hypot(physical["vx"],physical["vy"])<.10:
                continue
            eligible_scans+=1
            for track in r["tracks"]:
                if track["track_id"] in ids and track["velocity_valid"]:
                    spatial,transverse,vector=track_errors(track,physical,origin,yaw,geometry["moving_obstacle"]["size_m"])
                    spatial_errors.append(spatial);transverse_errors.append(transverse);vel_errors.append(vector)
                    if spatial<=.10 and abs(transverse)<=.075:valid_matches+=1
    check("tracked_cart_geometry_and_velocity",len(vel_errors)>=max(5,.5*eligible_scans) and max(spatial_errors,default=math.inf)<=.10
          and valid_matches>=.90*len(vel_errors))
    return {"conflict_prediction_ns":predicted_ns,"cart_at_unyielded_arrival_xy":predicted["xy"] if predicted else None,
            "cart_cleared_ns":cleared_ns,"rover_entered_ns":passed_ns,"longest_stopped_before_entry_s":longest,
            "actor_truth_samples":len(actor),"actor_start_xy":actor[0]["xy"],"actor_end_xy":actor[-1]["xy"],
            "peak_cart_speed_mps":max(speeds,default=None),"yield_events":len(yields),"clear_events":len(clears),
            "tracked_ids":sorted(ids),"eligible_velocity_scans":eligible_scans,
            "compared_velocity_samples":len(vel_errors),"matched_track_samples":valid_matches,
            "max_track_outside_cart_m":max(spatial_errors,default=None),
            "max_transverse_speed_error_mps":max((abs(v) for v in transverse_errors),default=None),
            "max_vector_velocity_error_mps":max(vel_errors,default=None),
            "vector_velocity_error_over_0p075_samples":sum(v>.075 for v in vel_errors)}


def score(args):
    out=args.out
    destination=getattr(args,"score_out",None) or out
    destination.mkdir(parents=True,exist_ok=True)
    rows=[json.loads(line) for line in (out/"observations.jsonl").open(encoding="utf8")]
    geometry=json.loads((out/"geometry.json").read_bytes())
    def of(kind):return [r for r in rows if r["kind"]==kind]
    truth,scans,all_events=of("truth"),of("scan"),of("event")
    events=[r["event"] for r in all_events if r["event"].get("operation_id")==args.operation]
    checks={}
    def check(name,condition):checks[name]=bool(condition)
    check("truth_scan_odom_clock_present",all(of(k) for k in ("truth","scan","odom","clock")))
    stamps=[r["stamp_ns"] for r in truth]
    check("world_truth_frame",all(r["frame"]=="world" for r in truth))
    check("monotonic_truth",all(a<b for a,b in zip(stamps,stamps[1:])))
    check("truth_coverage",len(truth)>=10 and max_gap(truth)<=.20)
    check("scan_coverage",len(scans)>=4 and max_gap(scans)<=.40)
    expected_links=set(LINGAO_LINKS if args.vehicle=="lingao" else LINKS)
    check("vehicle_collision_links",{p["link"] for p in geometry["footprint"]}==expected_links)
    check("start_pose",bool(truth) and math.dist(truth[0]["xy"],args.start)<=.12)
    arrival=math.dist(truth[-1]["xy"],args.goal) if truth else None
    check("goal_reached",arrival is not None and arrival<=.12)
    boxes=[s for s in geometry["obstacles"] if s["name"].split("/")[0]==args.obstacle]
    crossing=args.scenario=="crossing"
    moving=geometry.get("moving_obstacle",{})
    actor=sorted(of("actor"),key=lambda r:r["stamp_ns"])
    actor_ts=[r["stamp_ns"] for r in actor]
    def obstacle_at(stamp):
        if not crossing:
            return boxes[0]["poly"] if len(boxes)==1 else None
        q=interpolate(actor,actor_ts,stamp)
        if not q or not moving:return None
        return [transform(v,q["xy"],q["yaw"]) for v in moving["poly"]]
    sampled_by_part={}; swept_by_part={}; last_parts={}
    trajectory=[]; previous=None; minimum=math.inf
    sweep_radius=max(.35,max(math.hypot(*v)+p["radius"] for p in geometry["footprint"] for v in p["poly"]))
    for row in truth:
        d,label=clearance(row,geometry)
        box=obstacle_at(row["stamp_ns"])
        parts=box_clearances(row,geometry,box) if box else {}
        if crossing and parts and min(parts.values())<d:
            d,label=min(parts.values()),CART
        for name,value in parts.items():
            sampled_by_part[name]=min(sampled_by_part.get(name,math.inf),value)
        # A lower bound over the entire interval, accounting for translation and
        # rotation between consecutive samples at the gateway's known speed limits.
        if previous:
            dt=(row["stamp_ns"]-previous["stamp_ns"])*1e-9
            reserve=(.32+sweep_radius*1.1+(.18 if crossing else 0.))*dt/2
            swept=min(d,trajectory[-1]["sample_clearance_m"])-reserve
            minimum=min(minimum,swept)
            for name,value in parts.items():
                if name in last_parts:
                    swept_by_part[name]=min(swept_by_part.get(name,math.inf),min(value,last_parts[name])-reserve)
        trajectory.append({"stamp_ns":row["stamp_ns"],"sample_clearance_m":d,"closest":label})
        previous=row; last_parts=parts
    check("footprint_swept_clearance",math.isfinite(minimum) and minimum>=.45)
    check("sweep_within_geometry_extent",all(math.dist(r["xy"],geometry["origin"])<10 for r in truth))
    centered=args.scenario=="centered"
    check("scenario_geometry",len(boxes)==int(centered) and (not centered or
          geometry["box_sizes"].get(args.obstacle)==[.5,.5,1.]) and bool(moving)==crossing)
    # Every perception event from the operation must refer to a published sampled report.
    reports={r["stamp_ns"]:r["report"] for r in of("report")}
    scan_by_stamp={s["stamp_ns"]:s for s in scans}
    refs={r.get("report_ref"):r for r in reports.values()}
    front=[e for e in events if e.get("code")=="perception.front_obstacle"]
    plans=[e for e in events if e.get("code")=="navigation.local_plan"]
    avoid=[e for e in events if e.get("code")=="navigation.local_avoid"]
    expected_task=args.operation.split("/waypoint/")[0].split("/step/")[0]
    check("event_identity",bool(events) and len({e.get("event_id") for e in events})==len(events) and
          all(e.get("module")=="edge.gateway" and e.get("task_id")==expected_task and
              e.get("trace_id")==expected_task for e in events))
    check("arrived_event",any(e.get("code")=="navigation.arrived" for e in events))
    check("no_hard_stop",not any(e.get("code")=="navigation.path_blocked" for e in events))
    check("plan_events",bool(front and plans) and (crossing or bool(avoid)==centered))
    check("report_identity",bool(front) and all((ev.get("data",{}).get("report") or {}).get("report_ref") in refs for ev in front))
    check("plan_binding",bool(plans) and all((ev.get("data",{}).get("plan") or {}).get("report_ref") in refs for ev in plans))
    footprint_ids=Counter((e.get("data",{}).get("plan") or {}).get("footprint_ref","") for e in plans)
    if args.planner=="footprint_follow":
        profile=json.loads((out.parent/"vehicle-profile.json").read_bytes())
        profile_id=profile["profile_id"]
        check("footprint_binding",bool(plans) and set(footprint_ids)=={profile_id})
        starts=[e["data"].get("vehicle_footprint",{}) for e in events if e["kind"]=="action.started"]
        check("installed_profile",len(starts)==1 and starts[0].get("profile_id")==profile_id and
              all(starts[0].get(k)==profile[k] for k in ("front_m","rear_m","left_m","right_m","track_m")))
        check("ttc_uses_vehicle_footprint",bool(reports) and
              all(r.get("ttc_footprint_ref")==profile_id for r in reports.values()))
        # The independently exported SDF must fit inside the loaded profile.
        vertices=[v for part in geometry["footprint"] for v in part["poly"]]
        extents=(max(x for x,y in vertices),max(-x for x,y in vertices),
                 max(y for x,y in vertices),max(-y for x,y in vertices))
        check("profile_encloses_physics",all(-1e-5<=profile[k]-d<=.002
              for k,d in zip(("front_m","rear_m","left_m","right_m"),extents)))
    check("scan_and_report_source",bool(reports) and all(r.get("frame_id")=="base_link" and
          r.get("source_frame_id")=="laser_link" and r.get("calibration_ref") and
          r.get("schema_version")==1 and r.get("scan_seq") is not None for r in reports.values()))
    check("published_report_matches_event",bool(front) and all(
          refs.get(e.get("data",{}).get("report",{}).get("report_ref"))==e["data"]["report"] for e in front))
    task_stamps=[e["data"]["report"]["stamp_ns"] for e in front if e.get("data",{}).get("report")]
    limits=[e.get("data",{}).get("scan_stamp_ns") for e in events if e["kind"] in ("action.started","action.result")]
    bounded=len(limits)==2 and all(isinstance(t,int) for t in limits) and limits[0]<limits[1]
    first,last=tuple(limits) if bounded else (0,0)
    check("full_operation_observed",bounded and bool(task_stamps) and min(task_stamps)-first<=.4e9
          and last-max(task_stamps)<=.4e9 and bool(truth) and stamps[0]<=first and stamps[-1]>=last)
    if args.localization!="wheel":
        for kind,limit in (("imu",.10),("wheel",.14),("odom",.14)):
            stream=of(kind)
            check(kind+"_covers_operation",len(stream)>=10 and stream[0]["stamp_ns"]<=first and
                  stream[-1]["stamp_ns"]>=last and max_gap(stream)<=limit)
        if args.localization=="wheel_gyro_scan":
            scan_updates=[r for r in of("scan_correction") if first<=r["stamp_ns"]<=last]
            check("laser_translation_updates",len(scan_updates)>=3 and
                  any(r["method"] in ("matched","one_axis") for r in scan_updates))
    samples=[]; active_scans=[s for s in scans if first<=s["stamp_ns"]<=last]
    missed=[s["stamp_ns"] for s in active_scans if s["stamp_ns"] not in reports]
    check("all_active_scans_have_reports",bool(active_scans) and not missed)
    if (centered and len(boxes)==1) or crossing:
        resolution=None
        if crossing:
            sensor=ET.parse(out/"world.sdf").find(".//model[@name='edge_rover']//sensor[@name='horizontal_lidar']")
            resolution=float(sensor.findtext("lidar/range/resolution"))
            if not 0<resolution<=.05:raise ValueError("unexpected scanner range resolution")
        for s in active_scans:
            stamp=s["stamp_ns"]
            p=interpolate(truth,stamps,stamp)
            if not p:
                samples.append({"stamp_ns":stamp,"error":"missing_truth","pass":False});continue
            box=obstacle_at(stamp)
            if not box:
                samples.append({"stamp_ns":stamp,"error":"missing_obstacle_truth","pass":False});continue
            expected=box_reference(s,p["xy"],p["yaw"],geometry,box,resolution)
            if expected is None:continue
            r=reports.get(stamp,{})
            rd,rb=r.get("nearest_range_m"),r.get("nearest_bearing_rad")
            if crossing:
                face=matching_face(expected,rd,rb)
                graded=face or min(expected,key=lambda c:c["range_m"])
                d,b,n=graded["range_m"],graded["bearing_rad"],graded["rays"]
            else:
                d,b,n=expected
            distance_error=abs(d-rd) if isinstance(rd,(int,float)) else None
            bearing_error=math.degrees(abs(angle(b-rb))) if isinstance(rb,(int,float)) else None
            ok=bool(r.get("valid") and distance_error is not None and distance_error<=max(.05,.05*d)
                    and bearing_error is not None and bearing_error<=2.)
            samples.append({"stamp_ns":stamp,"expected_range_m":d,"expected_bearing_rad":b,"rays":n,
                            "measured_range_m":rd,"measured_bearing_rad":rb,"range_error_m":distance_error,
                            "bearing_error_deg":bearing_error,"pass":ok})
            if crossing:
                samples[-1].update(visible_candidates=expected,matched_shape=face["shape"] if face else None)
    if centered or crossing:
        check("all_visible_box_measurements",len(samples)>=3 and all(s["pass"] for s in samples))
    if centered:
        check("straight_path_would_hit",bool(boxes) and poly_distance(args.start,args.goal,boxes[0]["poly"])==0)
        check("observed_lateral_bypass",any(dist_point(r["xy"],args.start,args.goal)>.45 for r in truth))
    contacts=of("contacts"); ground_pairs=set();bad_pairs=set(); ground_stamps={k:[] for k in expected_links}
    for row in contacts:
        for name1,name2,id1,id2 in row["pairs"]:
            pair=(name1,name2)
            if not all(pair) or "floor::floor_link::floor" not in pair or not any(n.startswith("edge_rover::"+row["link"]+"::") for n in pair):
                bad_pairs.add(pair)
            else:
                ground_pairs.add(pair)
                ground_stamps[row["link"]].append(row["stamp_ns"])
    check("contact_stream_ground_witness",bool(ground_pairs))
    check("no_non_ground_contact",not bad_pairs)
    contact_gaps={}
    wheels=LINGAO_WHEELS if args.vehicle=="lingao" else ("left_wheel","right_wheel","rear_caster")
    for link in wheels:
        t=sorted(set(v for v in ground_stamps[link] if first<=v<=last))
        contact_gaps[link]=max((b-a for a,b in zip([first]+t,t+[last])),default=last-first)*1e-9
    check("contact_coverage",bool(task_stamps) and all(ground_stamps[k] and gap<=.4 for k,gap in contact_gaps.items()))
    crossing_evidence={}
    static_policy_evidence=None
    if getattr(args,"dynamic_policy",None) and not crossing:
        observed=dynamic_evidence(events,of("dynamic_report"),refs,check)
        raw_plans=[e["data"]["plan"] for e in plans]
        static_policy_evidence=static_dynamic_behavior(events,raw_plans,refs)
        check("static_world_no_dynamic_hold",static_policy_evidence["passed"])
        check("static_run_used_dynamic_observer",bool(observed) and any(p.get("dynamic_report_ref") in observed for p in raw_plans))
    if crossing:
        cart=ET.parse(out/"world.sdf").find(".//model[@name='crossing_cart']")
        guide=cart.find("joint[@type='prismatic']")
        controller=cart.find("plugin[@filename='gz-sim-joint-controller-system']")
        publisher=cart.find("plugin[@filename='gz-sim-odometry-publisher-system']")
        contact=cart.find("link[@name='cart']/sensor[@type='contact']/contact")
        check("runtime_cart_physics_instrumented",guide is not None and guide.findtext("parent")=="world"
              and guide.findtext("child")=="cart" and [float(v) for v in guide.findtext("axis/xyz").split()]==[0.,1.,0.]
              and controller is not None and controller.findtext("joint_name")==guide.get("name")
              and publisher is not None and publisher.findtext("odom_topic")==TRUTH and publisher.findtext("odom_frame")=="world"
              and contact is not None and contact.findtext("topic")=="/fixture/crossing/cart_contacts"
              and cart.find("link[@name='cart']/collision[@name='"+contact.findtext("collision")+"']") is not None)
        crossing_evidence=score_crossing(geometry,actor,truth,first,last,args,events,of("actor_contacts"),out,check,
                                         of("odom"),of("dynamic_report"),refs)
        crossing_evidence["angular_ambiguity_samples"]=sum(len(s.get("visible_candidates",[]))>1 for s in samples)
        crossing_evidence["laser_range_resolution_m"]=resolution
    write(destination/"clearance.json",trajectory)
    write(destination/"measurements.json",samples)
    result={"passed":all(checks.values()),"checks":checks,"scenario":args.scenario,"vehicle":args.vehicle,"localization":args.localization,"operation":args.operation,
            "start":args.start,"goal":args.goal,"arrival_error_m":arrival,"min_swept_clearance_m":minimum if math.isfinite(minimum) else None,
            "max_truth_gap_s":max_gap(truth),"max_scan_gap_s":max_gap(scans),"samples":len(truth),
            "active_scans":len(active_scans),"unmatched_scan_stamps":missed,"visible_box_samples":len(samples),
            "max_bearing_error_deg":max((s["bearing_error_deg"] for s in samples if s.get("bearing_error_deg") is not None),default=None),
            "max_range_error_m":max((s["range_error_m"] for s in samples if s.get("range_error_m") is not None),default=None),
            "failure_samples":sum(not s["pass"] for s in samples),"contact_packets":len(contacts),
            "max_contact_gap_s":contact_gaps,"ground_contact_pairs":sorted(ground_pairs),"unexpected_contact_pairs":sorted(bad_pairs),
            "event_codes":dict(Counter(e.get("code") for e in events)),"source_sdf_sha256":geometry["sdf_sha256"]}
    result["peak_lateral_excursion_m"]=max((dist_point(r["xy"],args.start,args.goal) for r in truth),default=None)
    result["path_length_m"]=sum(math.dist(a["xy"],b["xy"]) for a,b in zip(truth,truth[1:]))
    pivot_s=rolling_turn_s=yaw_travel=0.
    max_truth_speed=max_truth_yaw_rate=0.
    for a,b in zip(truth,truth[1:]):
        dt=(b["stamp_ns"]-a["stamp_ns"])*1e-9
        if dt<=0 or not first<=a["stamp_ns"]<=b["stamp_ns"]<=last:
            continue
        change=abs(angle(b["yaw"]-a["yaw"]))
        max_truth_speed=max(max_truth_speed,math.dist(a["xy"],b["xy"])/dt)
        max_truth_yaw_rate=max(max_truth_yaw_rate,change/dt)
        yaw_travel+=change
        if change/dt>.18:
            if math.dist(a["xy"],b["xy"])/dt<.03:
                pivot_s+=dt
            else:
                rolling_turn_s+=dt
    result["world_turning"]={"total_abs_yaw_rad":yaw_travel,"pivot_seconds":pivot_s,
                            "rolling_turn_seconds":rolling_turn_s,"turn_rate_threshold_radps":.18,
                            "pivot_translation_threshold_mps":.03}
    if crossing:
        check("sweep_motion_bounds_observed",max_truth_speed<=.32 and max_truth_yaw_rate<=1.1)
        crossing_evidence.update(max_observed_rover_speed_mps=max_truth_speed,max_observed_rover_yaw_radps=max_truth_yaw_rate)
        result["passed"]=all(checks.values())
    result["box_clearances_by_part"]={"sample_min_m":sampled_by_part,"swept_lower_bound_m":swept_by_part}
    result["footprint_plan_counts"]={ref:n for ref,n in footprint_ids.items() if ref}
    result["scan_correction_modes"]=dict(Counter(r["method"] for r in of("scan_correction")))
    if crossing:result["crossing"]=crossing_evidence
    if static_policy_evidence is not None:result["static_dynamic_policy"]=static_policy_evidence
    write(destination/"score.json",result)
    return result


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    subs=cli.add_subparsers(dest="action",required=True)
    rec=subs.add_parser("record")
    rec.add_argument("--out",type=Path,required=True);rec.add_argument("--world",default="edge_lab")
    scoring=subs.add_parser("score")
    scoring.add_argument("--out",type=Path,required=True)
    scoring.add_argument("--score-out",type=Path,help="separate directory for grading an immutable capture")
    scoring.add_argument("--scenario",choices=("clear","centered","crossing"),required=True)
    scoring.add_argument("--vehicle",choices=("campus","lingao"),default="campus")
    scoring.add_argument("--localization",choices=("wheel","wheel_gyro","wheel_gyro_scan"),default="wheel")
    scoring.add_argument("--planner",choices=("gap_follow","footprint_follow"),default="gap_follow")
    scoring.add_argument("--dynamic-policy",choices=("yield_crossing",))
    scoring.add_argument("--operation",required=True);scoring.add_argument("--obstacle",required=True)
    scoring.add_argument("--start",type=float,nargs=2,required=True);scoring.add_argument("--goal",type=float,nargs=2,required=True)
    args=cli.parse_args()
    if args.action=="record":
        record(args.out,args.world)
    else:
        result=score(args)
        print(json.dumps(result,ensure_ascii=False))
        sys.exit(0 if result["passed"] else 1)
