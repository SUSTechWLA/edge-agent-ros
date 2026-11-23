"""Bounded, body-local geometric previews over positive surface observations."""
import math
from collections import defaultdict

if __package__:
    from . import trajectory
else:
    import trajectory


def finite(v):
    return type(v) in (float, int) and math.isfinite(v)


def stamp(v):
    if (type(v) is int or isinstance(v,str) and v.isascii() and v.isdigit()) and 0 < int(v) < 2**63:
        return int(v)
    raise ValueError("invalid acquisition timestamp")


def wrap(a):
    return math.atan2(math.sin(a), math.cos(a))


def bin_at(x,y,count):
    return round(math.atan2(y,x)*count/math.tau)%count


def point_cloud(snapshot):
    """Retain the nearest range in each angular cell; gaps stay unknown."""
    bins=180
    free=[None]*bins
    hits=[]
    rays=snapshot["rays"]
    if not isinstance(rays,list) or len(rays)>20000:
        raise ValueError("invalid ray count")
    for ray in rays:
        angle=ray["angle_rad"]
        if not finite(angle):
            raise ValueError("non-finite bearing")
        c,s=math.cos(angle),math.sin(angle)
        i=bin_at(c,s,bins)
        known=ray.get("free_to_m")
        near=ray.get("hit_m")
        if near is not None:
            if not finite(near) or not .015<=near<=100:
                raise ValueError("invalid observed range")
            hits.append((c*near,s*near))
        if known is not None:
            if not finite(known) or not 0<=known<=100:
                raise ValueError("invalid known range")
            bound=min(known,near) if near is not None else known
            free[i]=bound if free[i] is None else min(free[i],bound)
    raw=snapshot.get("hits_body_m",[])
    if not isinstance(raw,list) or len(raw)>30000:
        raise ValueError("invalid point count")
    for p in raw:
        if not isinstance(p,(list,tuple)) or len(p)!=2 or not all(finite(x) and abs(x)<=100 for x in p):
            raise ValueError("invalid return")
        hits.append(tuple(p))
    return hits,free


def surface_groups(points,cell):
    cells=defaultdict(list)
    for x,y in points:
        cells[(math.floor(x/cell),math.floor(y/cell))].append((x,y))
    groups=[]
    while cells:
        key=next(iter(cells))
        queue=[key]
        pts=cells.pop(key)
        while queue:
            ix,iy=queue.pop()
            for dx in (-1,0,1):
                for dy in (-1,0,1):
                    other=(ix+dx,iy+dy)
                    if other in cells:
                        queue.append(other)
                        pts.extend(cells.pop(other))
        xs,ys=zip(*pts)
        groups.append({"center_body_m":[sum(xs)/len(xs),sum(ys)/len(ys)],
                       "bounds_body_m":[min(xs),min(ys),max(xs),max(ys)],
                       "nearest_range_m":min(math.hypot(x,y) for x,y in pts),"support":len(pts)})
    return sorted(groups,key=lambda g:g["nearest_range_m"])


def footprint_clearance(x,y,c,s,nearby,f):
    best=math.inf
    for px,py in nearby:
        dx,dy=px-x,py-y
        bx,by=c*dx+s*dy,-s*dx+c*dy
        gap_x=max(0.,-f["rear_m"]-bx,bx-f["front_m"])
        gap_y=max(0.,-f["right_m"]-by,by-f["left_m"])
        best=min(best,math.hypot(gap_x,gap_y))
    return best


def evaluate(curve,index,free,f,margin,radius):
    # A dropped hit in the same 15 mm cell can be up to its diagonal from its representative.
    padding=curve.sweep_padding+math.sqrt(2)*.015
    target=margin+padding
    minimum=max(1.,target)
    offsets=[(bx,by) for bx in (-f["rear_m"]-margin,0.,f["front_m"]+margin)
             for by in (-f["right_m"]-margin,0.,f["left_m"]+margin)]
    limits=[]
    for i in range(len(free)):
        adjacent=(free[i-1],free[i],free[(i+1)%len(free)])
        limits.append(min(adjacent) if None not in adjacent else 0.)
    known=total=0
    for x,y,yaw,_ in curve.samples:
        c,s=math.cos(yaw),math.sin(yaw)
        rad=radius+minimum
        for ix in range(math.floor(x-rad),math.floor(x+rad)+1):
            for iy in range(math.floor(y-rad),math.floor(y+rad)+1):
                nearby=index.get((ix,iy))
                if nearby:
                    minimum=min(minimum,footprint_clearance(x,y,c,s,nearby,f))
                    if minimum<target:
                        return None
        for bx,by in offsets:
            px,py=x+c*bx-s*by,y+s*bx+c*by
            if -f["rear_m"]<=px<=f["front_m"] and -f["right_m"]<=py<=f["left_m"]:
                continue
            i=bin_at(px,py,len(free))
            total+=1
            known+=limits[i]>=math.hypot(px,py)+padding
    return {"minimum_clearance_m":max(0.,minimum-padding),"coverage_fraction":known/max(total,1)}


def pose(record,base,now):
    if not isinstance(record,dict):
        return None
    try:
        t=stamp(record.get("stamp_ns"))
    except ValueError:
        return None
    if (not isinstance(record.get("frame_id"),str) or not record["frame_id"] or record.get("child_frame_id")!=base or
            abs(now-t)>350_000_000 or not all(finite(record.get(k)) for k in ("x_m","y_m","yaw_rad"))):
        return None
    return {k:record[k] for k in ("frame_id","child_frame_id","x_m","y_m","yaw_rad")} | {"stamp_ns":str(t)}


def project(points,p):
    c,s=math.cos(p["yaw_rad"]),math.sin(p["yaw_rad"])
    return [[p["x_m"]+c*x-s*y,p["y_m"]+s*x+c*y] for x,y in points]


def plan(snapshot,profile,goal_body_m=None,preferred_side=None):
    f={k:profile[k] for k in ("front_m","rear_m","left_m","right_m")}
    margin=profile["margin_m"]
    if any(not finite(x) or not .02<=x<=2 for x in [*f.values(),margin]):
        raise ValueError("invalid site footprint or clearance")
    hits,free=point_cloud(snapshot)
    hits=[(x,y) for x,y in hits if not
          (-f["rear_m"]<=x<=f["front_m"] and -f["right_m"]<=y<=f["left_m"])]
    # Preserve one real return per 15 mm cell. The maximum displacement is charged to the bound above.
    sparse={}
    for x,y in hits:
        sparse.setdefault((math.floor(x/.015),math.floor(y/.015)),(x,y))
    points=list(sparse.values())
    groups=surface_groups(points,.14) if len(points)<=6000 else []
    limit=profile["horizon_m"]
    if not finite(limit) or not .6<=limit<=5:
        raise ValueError("invalid local horizon")
    reach=max(f["left_m"],f["right_m"])+margin
    blocking=[g for g in groups if g["bounds_body_m"][2]>f["front_m"] and g["bounds_body_m"][0]<limit and
              g["bounds_body_m"][1]<=reach and g["bounds_body_m"][3]>=-reach]
    goal=(limit,0.) if goal_body_m is None else goal_body_m
    if (not isinstance(goal,(list,tuple)) or len(goal)!=2 or not all(finite(v) for v in goal)
            or not .6<=goal[0]<=limit or abs(goal[1])>1.5):
        raise ValueError("local goal requires a forward body-frame x in [0.6,horizon] and |y| <= 1.5 m")
    curvature_limit=profile.get("curvature_limit_inv_m",trajectory.DEFAULT_CURVATURE_LIMIT)
    if not finite(curvature_limit) or not .05<=curvature_limit<=10:
        raise ValueError("invalid site curvature limit")
    if preferred_side not in (None,"left","right"):
        raise ValueError("invalid preferred side")
    base=snapshot["base_frame"]
    # A sector which only reaches the currently occupied body does not observe a corridor.
    known_sectors=0
    for i,extent in enumerate(free):
        angle=i*math.tau/len(free)
        cx,cy=math.cos(angle),math.sin(angle)
        boundary=min((f["front_m"] if cx>=0 else f["rear_m"])/max(1e-12,abs(cx)),
                     (f["left_m"] if cy>=0 else f["right_m"])/max(1e-12,abs(cy)))
        known_sectors+=extent is not None and extent>=boundary+.10
    front=[(x,y) for x,y in hits if x>=f["front_m"] and -f["right_m"]<=y<=f["left_m"]]
    nearest_front=min(front,default=None)
    result={"schema_version":1,"status":"blocked","reason":"no_observed_bypass","motion_authorized":False,
            "base_frame":base,"source_id":snapshot["source_id"],"scan_stamp_ns":str(stamp(snapshot["scan_stamp_ns"])),
            "calibration_ref":snapshot["calibration_ref"],"source_evidence":snapshot.get("source_evidence",{}),
            "footprint":f,"profile_id":profile["profile_id"],"margin_m":margin,"horizon_m":limit,
            "local_goal_body_m":list(goal),"trajectory":None,"curvature_limit_inv_m":curvature_limit,
            "search":{"tested":0,"curvature_rejected":0,"clearance_rejected":0},
            "coverage":{"known_sectors":known_sectors,"total_sectors":len(free),"fraction":known_sectors/len(free),
                        "min_observed_beyond_footprint_m":.10},
            "scan_hits_body_m":points[::max(1,math.ceil(len(points)/600))],
            "obstacles":groups[:48],"forward_obstacle":blocking[0] if blocking else None,
            "front_surface_body_m":nearest_front,
            "front_clearance_to_surface_m":nearest_front[0]-f["front_m"] if nearest_front else None,
            "present_min_clearance_m":footprint_clearance(0.,0.,1.,0.,hits,f) if hits else None,
            "recommendation":"hold_position",
            "path_body_m":[],"path_odom_m":[],"planned_endpoint":None,"minimum_clearance_m":None,
            "clearance_basis":"lower_bound_to_sampled_surfaces_capped_at_search_radius","coverage_fraction_on_path":None,
            "coverage_basis":"fraction_of_swept_footprint_probes_with_three_adjacent_observed_bearings",
            "clearance_certified":False,"map_anchor":None}
    if not points and not known_sectors:
        result["reason"]="no_surface_or_ray_evidence"
        return result
    if len(points)>6000:
        result["reason"]="point_budget_exceeded"
        return result
    if result["present_min_clearance_m"] is not None and result["present_min_clearance_m"]<margin:
        result["reason"]="current_footprint_below_margin"
        return result
    radius=math.hypot(max(f["front_m"],f["rear_m"]),max(f["left_m"],f["right_m"]))
    index=defaultdict(list)
    for pt in points:
        index[(math.floor(pt[0]),math.floor(pt[1]))].append(pt)

    def assess(segments,side):
        result["search"]["tested"]+=1
        curve=trajectory.sample(segments,side,curvature_limit,radius)
        if curve is None:
            result["search"]["curvature_rejected"]+=1
            return None
        score=evaluate(curve,index,free,f,margin,radius)
        if not score:
            result["search"]["clearance_rejected"]+=1
        return (curve,score) if score else None

    direct_side="straight" if goal[1]==0 else ("left" if goal[1]>0 else "right")
    first=assess(trajectory.direct(goal),direct_side)
    accepted=[first] if first else []
    if points and not (first and first[1]["coverage_fraction"]==1.):
        near=min((x for x,y in points if 0<x<goal[0] and abs(y)<=reach+abs(goal[1])),default=goal[0]/2)
        for side,segments in trajectory.detours(goal,near):
            candidate=assess(segments,side)
            if candidate:
                accepted.append(candidate)
    if not accepted:
        return result

    # Prefer complete observation. Retain a previously chosen side only within
    # 2 percentage points of coverage and 12 cm of the shortest comparable curve.
    coverage=max(score["coverage_fraction"] for _,score in accepted)
    floor=1. if coverage==1. else coverage-.02
    comparable=[a for a in accepted if a[1]["coverage_fraction"]>=floor]
    shortest=min(c.length for c,_ in comparable)
    fitting=[a for a in comparable if a[0].length<=shortest+.12]
    curve,score=min(fitting,key=lambda a:(preferred_side is not None and a[0].side!=preferred_side,
                                      a[0].length,-a[1]["coverage_fraction"],a[0].side))
    poly=[[p[0],p[1]] for p in curve.samples]
    if len(curve.segments)==1 and curve.segments[0].y0==curve.segments[0].y1:
        poly=[poly[0],poly[-1]]
    complete=score["coverage_fraction"]==1.
    result.update(status="planned" if complete else "partial",reason="observed_local_preview" if complete
                  else "candidate_with_unknown_coverage",path_body_m=poly,minimum_clearance_m=score["minimum_clearance_m"],
                  trajectory=curve.record(curvature_limit),coverage_fraction_on_path=score["coverage_fraction"],
                  recommendation="review_geometric_preview",
                  planned_endpoint={"frame_id":base,"x_m":goal[0],"y_m":goal[1],"yaw_rad":0.})
    return result
