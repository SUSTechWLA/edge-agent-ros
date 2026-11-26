"""Rasterize only the selected paved campus edges and their near-ground collision shapes."""
import json
import math
from xml.etree import ElementTree as ET


def paved_edges(root, site, revision):
    manifest = json.loads((root/"overview/manifest.json").read_bytes())
    if manifest["scene_revision"] != revision or manifest["frame"] != "odom":
        raise ValueError("walk manifest does not match the indoor scene")
    route, = (r for r in manifest["routes"] if r["id"] == site["walk"]["route_id"])
    nodes = {n["id"]: n["xy"] for n in route["nodes"]}
    edges = {(e["from"], e["to"]): e for e in route["edges"]}
    world = ET.parse(root/"world.sdf").getroot().find("world")
    selected = site["walk"]["nodes"]
    corridors = []
    for start, end in zip(selected, selected[1:]):
        edge = edges[start, end]
        model = world.find(f"model[@name='{edge['model']}']")
        if edge["surface"] != "paved" or model.find(f".//visual[@name='{edge['visual']}']") is None:
            raise ValueError("physical walkway missing from world")
        corridors.append((nodes[start], nodes[end], edge["width_m"]/2))
    return nodes, corridors, world


def on_walk(x, y, corridors):
    for (ax,ay), (bx,by), radius in corridors:
        dx, dy = bx-ax, by-ay
        t = min(1., max(0., ((x-ax)*dx+(y-ay)*dy)/(dx*dx+dy*dy)))
        if math.hypot(x-ax-t*dx, y-ay-t*dy) <= radius:
            return True
    return False


def static_obstacles(world, bounds):
    obstacles = []
    for model in world.findall("model"):
        if model.get("name") not in ("teaching", "road"):
            continue
        if any(float(v) for v in model.findtext("pose", "0").split()):
            raise ValueError("campus collision frame changed")
        for link in model.findall("link"):
            if any(float(v) for v in link.findtext("pose", "0").split()):
                raise ValueError("campus link frame changed")
            for part in link.findall("collision"):
                x,y,z,roll,pitch,yaw = map(float,part.findtext("pose","0 0 0 0 0 0").split())
                geometry = part.find("geometry")
                size = geometry.findtext("box/size")
                radius = geometry.findtext("cylinder/radius")
                if size:
                    w,d,h = map(float,size.split())
                elif radius:
                    w=d=2*float(radius)
                    h=float(geometry.findtext("cylinder/length"))
                else:
                    continue
                reach = math.hypot(w,d)/2+.1
                if (x+reach<bounds[0] or y+reach<bounds[1] or x-reach>bounds[2] or y-reach>bounds[3]
                        or z-h/2>.6 or z+h/2<.1):
                    continue
                if roll or pitch:
                    raise ValueError("inclined near-ground campus collision needs rasterizer support")
                obstacles.append((x,y,math.cos(yaw),math.sin(yaw),w/2,d/2,bool(radius)))
    return obstacles


def occupied(x, y, obstacles, cell):
    padding=cell*math.sqrt(.5)
    for ox,oy,c,s,hw,hd,circular in obstacles:
        dx,dy=x-ox,y-oy
        if circular:
            if math.hypot(dx,dy) <= hw+padding:
                return True
        elif abs(c*dx+s*dy)<=hw+padding and abs(-s*dx+c*dy)<=hd+padding:
            return True
    return False
