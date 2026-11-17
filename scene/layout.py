"""A versioned, local site survey in the localization frame (metres)."""
import hashlib
import heapq
import json
import math
from pathlib import Path


class RouteError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class Layout:
    def __init__(self, path):
        raw = Path(path).read_bytes()
        self.data = json.loads(raw)
        self.digest = hashlib.sha256(raw).hexdigest()
        if self.data["schema_version"] != 1:
            raise ValueError("unsupported scene schema")
        self.nodes = {p["id"]:p for p in self.data["nodes"]}
        self.links = {key:[] for key in self.nodes}
        for a,b in self.data["edges"]:
            if not self.clear(self.nodes[a]["xy"], self.nodes[b]["xy"]):
                raise ValueError(f"scene edge intersects footprint: {a}, {b}")
            self.links[a].append(b)
            self.links[b].append(a)

    def zone(self, xy):
        for zone in self.data["zones"]:
            x0,y0,x1,y1 = zone["bounds"]
            if x0<=xy[0]<=x1 and y0<=xy[1]<=y1:
                return zone["id"]
        return None

    def clear(self, start, end):
        """Intersect the segment with each robot-inflated axis-aligned footprint."""
        margin = self.data["clearance_m"]
        for f in self.data["features"]:
            if f.get("base_z",0) > 0.5:
                continue
            lo,hi = 0.,1.
            for axis in (0,1):
                center = f["xy"][axis]
                radius = f["size"][axis]/2 + margin
                delta = end[axis]-start[axis]
                if abs(delta)<1e-9:
                    if abs(start[axis]-center)>radius:
                        lo,hi=1.,0.
                        break
                    continue
                a,b=sorted(((center-radius-start[axis])/delta,(center+radius-start[axis])/delta))
                lo,hi=max(lo,a),min(hi,b)
            if lo<=hi:
                return False
        return True

    def route(self, start, label):
        matches = [n for n in self.nodes.values() if label in
                   [n["id"],n["label"],*n.get("aliases",[])]]
        if not matches:
            raise RouteError("scene.place_unknown","场景中没有这个地点")
        if len(matches) > 1:
            names = "、".join(n["id"] for n in matches)
            raise RouteError("scene.place_ambiguous",f"地点名称有多个匹配，请明确目标地点 ID：{names}")
        target = matches[0]
        if not target.get("accessible",True):
            raise RouteError("scene.place_inaccessible","目的地处于限制通行区")
        zone = self.zone(start)
        entries = [n for n in self.nodes.values() if n["zone"]==zone and n.get("accessible",True)
                   and self.clear(start,n["xy"])]
        if any(z["id"] == zone and z["kind"] == "transition" for z in self.data["zones"]):
            entries = [n for n in self.nodes.values() if self.clear(start,n["xy"])
                       and n.get("accessible",True)]
        queue = [(math.dist(start,n["xy"]),[n["id"]]) for n in entries]
        heapq.heapify(queue)
        seen = set()
        while queue:
            cost,path = heapq.heappop(queue)
            tail = path[-1]
            if tail in seen:
                continue
            seen.add(tail)
            if tail==target["id"]:
                return path
            for neighbor in self.links[tail]:
                if neighbor not in seen and self.nodes[neighbor].get("accessible",True):
                    distance=math.dist(self.nodes[tail]["xy"],self.nodes[neighbor]["xy"])
                    heapq.heappush(queue,(cost+distance,[*path,neighbor]))
        raise RouteError("scene.no_route","当前位置和目标之间没有经过验证的连接")

    def plan(self, start, label):
        path = self.route(start, label)
        waypoints = [self.nodes[key]["xy"] for key in path]
        distance = sum(math.dist(a, b) for a, b in zip([start, *waypoints], waypoints))
        return {"scene":self.data["id"], "revision":self.data["revision"], "digest":self.digest,
                "frame":self.data["frame"], "path":path, "waypoints":waypoints,
                "start_xy":start, "start_zone":self.zone(start), "destination_id":path[-1],
                "distance_m":distance}
