"""Generate the metric, coloured playing surface shared by Gazebo and both maps."""
import hashlib
import math
from collections import defaultdict


STRAIGHT = 84.39
KERB_RADIUS = 36.50
LANE_WIDTH = 1.22
LANES = 8
PITCH_LENGTH, PITCH_WIDTH = 105.0, 68.0
PALETTE = {
    "rubber": (.64, .255, .185), "rubber_alt": (.69, .285, .205),
    "grass": (.24, .48, .26), "grass_light": (.295, .535, .285),
    "grass_dark": (.25, .49, .255), "paint": (.97, .97, .91),
    "kerb": (.81, .82, .76),
}


class PlayingSurface:
    """Named horizontal triangles in stadium-local metres; all front faces point up."""

    def __init__(self):
        self.vertices = []
        self.faces = defaultdict(list)
        self.indices = {}

    def polygon(self, name, material, points, z):
        # Polygons here are convex, so a fan suffices. Reverse clockwise input.
        signed = sum(a[0]*b[1] - b[0]*a[1] for a, b in zip(points, points[1:] + points[:1]))
        indices = []
        for x, y in points if signed > 0 else reversed(points):
            vertex = (round(x, 5), round(y, 5), z)
            if vertex not in self.indices:
                self.vertices.append(vertex)
                self.indices[vertex] = len(self.vertices)
            indices.append(self.indices[vertex])
        self.faces[name, material].extend((indices[0], a, b) for a, b in zip(indices[1:], indices[2:]))

    def stripe(self, name, a, b, width=.12, z=.055):
        dx, dy = b[0]-a[0], b[1]-a[1]
        scale = width/(2*math.hypot(dx, dy))
        nx, ny = -dy*scale, dx*scale
        self.polygon(name, "paint", [(a[0]+nx,a[1]+ny), (a[0]-nx,a[1]-ny),
                                     (b[0]-nx,b[1]-ny), (b[0]+nx,b[1]+ny)], z)

    def arc(self, name, center, inner, outer, a, b, material, z):
        # At most 2.5 degrees per station: <12 mm chord error at the outer curve.
        count = max(2, math.ceil(abs(b-a)/math.radians(2.5)))
        for i in range(count):
            angles = (a+(b-a)*i/count, a+(b-a)*(i+1)/count)
            points = [(center[0]+r*math.cos(t), center[1]+r*math.sin(t))
                      for r,t in ((outer,angles[0]),(outer,angles[1]),(inner,angles[1]),(inner,angles[0]))]
            self.polygon(name, material, points[:3] if inner == 0 else points, z)

    def oval_ring(self, name, inner, outer, material, z):
        half = STRAIGHT/2
        for side in (-1, 1):
            start = -math.pi/2 if side == 1 else math.pi/2
            self.arc(name, (side*half,0), inner, outer, start, start+math.pi, material, z)
            self.polygon(name, material, [(-half,side*inner), (half,side*inner),
                                          (half,side*outer), (-half,side*outer)], z)

    def write(self, directory):
        directory.mkdir(parents=True, exist_ok=True)
        mtl = "".join(f"newmtl {name}\nKa {r} {g} {b}\nKd {r} {g} {b}\nKs 0 0 0\nillum 1\n"
                      for name,(r,g,b) in PALETTE.items())
        materials = "athletics-" + hashlib.sha256(mtl.encode()).hexdigest()[:12] + ".mtl"
        rows = [f"mtllib {materials}", "vn 0 0 1"]
        rows.extend(f"v {x} {y} {z}" for x,y,z in self.vertices)
        for (name, material), faces in self.faces.items():
            rows.extend((f"g {name}", f"usemtl {material}"))
            rows.extend(f"f {a}//1 {b}//1 {c}//1" for a,b,c in faces)
        obj = "\n".join(rows)+"\n"
        target = directory/("athletics-" + hashlib.sha256(obj.encode()).hexdigest()[:12] + ".obj")
        (directory/materials).write_bytes(mtl.encode())
        target.write_bytes(obj.encode())
        return target


def playing_surface():
    s = PlayingSurface()
    half_l, half_w = PITCH_LENGTH/2, PITCH_WIDTH/2
    s.oval_ring("infield", 0, KERB_RADIUS, "grass", .024)
    for lane in range(LANES):
        radius = KERB_RADIUS + lane*LANE_WIDTH
        s.oval_ring(f"lane_{lane+1}", radius, radius+LANE_WIDTH, ("rubber","rubber_alt")[lane%2], .034)
    for line in range(LANES+1):
        radius = KERB_RADIUS + line*LANE_WIDTH
        s.oval_ring(f"boundary_{line}", radius-.05, radius, "kerb" if line == 0 else "paint", .05)
    # Running anticlockwise, the staggered 400 m starts follow the first bend.
    for lane in range(LANES):
        inside = KERB_RADIUS + lane*LANE_WIDTH
        measurement = inside + (.3 if lane == 0 else .2)
        advance = math.tau*(measurement - KERB_RADIUS - .3)/measurement
        angle = -math.pi/2 + advance
        ends = [(STRAIGHT/2 + r*math.cos(angle), r*math.sin(angle)) for r in (inside,inside+LANE_WIDTH)]
        s.stripe(f"start_{lane+1}", *ends, width=.05)
    s.stripe("finish", (STRAIGHT/2,-KERB_RADIUS), (STRAIGHT/2,-KERB_RADIUS-LANES*LANE_WIDTH), .05)
    for i in range(12):
        x = -half_l + i*PITCH_LENGTH/12
        nxt = x + PITCH_LENGTH/12
        s.polygon(f"mown_{i}", ("grass_light","grass_dark")[i%2],
                  [(x,-half_w),(nxt,-half_w),(nxt,half_w),(x,half_w)], .039)
    s.stripe("halfway", (0,-half_w),(0,half_w))
    s.arc("center_circle",(0,0),9.09,9.21,0,math.tau,"paint",.055)
    s.arc("center_mark",(0,0),0,.13,0,math.tau,"paint",.055)
    for side in (-1,1):
        gx, spot = side*half_l, side*(half_l-11)
        s.stripe(f"touchline_{side}",(-half_l,side*half_w),(half_l,side*half_w))
        s.stripe(f"goal_line_{side}",(gx,-half_w),(gx,half_w))
        for name,depth,width in (("penalty_area",16.5,20.16),("goal_area",5.5,9.16)):
            front = gx-side*depth
            for sign in (-1,1):
                s.stripe(f"{name}_{side}", (gx,sign*width), (front,sign*width))
            s.stripe(f"{name}_{side}", (front,-width), (front,width))
        direction = math.pi if side == 1 else 0
        angle = math.acos((16.5-11)/9.15)
        s.arc(f"penalty_arc_{side}",(spot,0),9.09,9.21,direction-angle,direction+angle,"paint",.055)
        s.arc(f"penalty_mark_{side}",(spot,0),0,.13,0,math.tau,"paint",.055)
        for sign in (-1,1):
            angle = math.atan2(-sign,-side)
            s.arc(f"corner_{side}_{sign}", (gx,sign*half_w), .94,1.06,angle-math.pi/4,angle+math.pi/4,"paint",.055)
    return s
