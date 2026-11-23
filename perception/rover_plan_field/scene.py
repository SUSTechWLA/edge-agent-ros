"""Derive a local display grid from the immutable observations of one planning window."""
from collections import Counter
import hashlib
import json
import math
import re


REF = re.compile(r"sha256:[0-9a-f]{64}")
CELL = .10
RADIUS = 5.
HEIGHT = 2.5
TRAVEL_BAND = (.055, .52)


def project_point(sensor, point):
    x, y, z = point[:3]
    qx, qy, qz, qw = sensor["quaternion_xyzw"]
    tx, ty, tz = 2*(qy*z-qz*y), 2*(qz*x-qx*z), 2*(qx*y-qy*x)
    bx, by, bz = sensor["translation_m"]
    return x+qw*tx+qy*tz-qz*ty+bx, y+qw*ty+qz*tx-qx*tz+by, z+qw*tz+qx*ty-qy*tx+bz


def checked_blob(raw, ref, limit):
    if (not isinstance(raw, bytes) or len(raw) > limit or not REF.fullmatch(ref)
            or "sha256:"+hashlib.sha256(raw).hexdigest() != ref):
        raise ValueError("unbound sensor artifact")
    return raw


def _ray_cells(origin, hit, observed):
    """Rasterize the part of a measured ray passing through the chassis-height slab."""
    ox, oy, oz = origin
    dx, dy, dz = (hit[i]-origin[i] for i in range(3))
    distance = math.hypot(dx,dy)
    if distance < .001:
        return
    # Limit work for long rays. The extra offset covers a sensor displaced from the origin.
    t0, t1 = 0., min(1., (RADIUS+math.hypot(ox,oy))/distance)
    if abs(dz)>1e-9:
        a, b = sorted((h-oz)/dz for h in TRAVEL_BAND)
        t0, t1 = max(t0,a), min(t1,b)
    elif not TRAVEL_BAND[0]<=oz<=TRAVEL_BAND[1]:
        return
    if t0>=t1:
        return
    steps = max(1,math.ceil((t1-t0)*distance/(CELL*.45)))
    for step in range(steps):
        t = t0+(step+.5)*(t1-t0)/steps
        x,y = ox+dx*t, oy+dy*t
        if math.hypot(x,y)<RADIUS:
            observed.add((math.floor(x/CELL),math.floor(y/CELL)))


def _centers(indices):
    return [round((i+.5)*CELL,3) for i in indices]


def _camera(sample,stamp):
    if not sample or not isinstance(sample.get("ref"),str) or not REF.fullmatch(sample["ref"]):
        return {"mode":"unavailable","reason":"no_bound_camera_frame"}
    skew = abs(stamp-int(sample["stamp_ns"]))/1e6
    meta = {key: sample[key] for key in ("ref","source_id","stamp_ns")}
    url = "/camera/"+sample["ref"][7:]+".png"
    if sample.get("url")!=url or skew>350:
        return {"mode":"unavailable","reason":"camera_binding_or_time_skew","skew_ms":skew,**meta}
    return {"mode":"camera_panel","reason":"no_bound_camera_to_body_extrinsic",
            "url":url,"skew_ms":round(skew,3),**meta}


def build_scene(snapshot, configuration, load_cloud, *, snapshot_ref=None, footprint=None):
    """load_cloud(ref) reads the original provider JSON bytes, never a new live window."""
    evidence = snapshot["source_evidence"]
    calibration = configuration["model_evidence"]["digest"]
    if calibration!=snapshot["calibration_ref"] or not REF.fullmatch(calibration):
        raise ValueError("scene calibration does not match the frozen snapshot")
    sources = evidence["lidars"]
    stamp = int(snapshot["scan_stamp_ns"])
    if not 1<=len(sources)<=8 or len({s["source_id"] for s in sources})!=len(sources):
        raise ValueError("invalid scene sources")
    if max(int(s["stamp_ns"]) for s in sources)!=stamp:
        raise ValueError("scan time differs from bound sources")
    voxels, occupied, observed = Counter(), set(), set()
    for source in sources:
        sensor = configuration["sources"][source["source_id"]]
        ref = source["artifact_ref"]
        if not REF.fullmatch(ref):
            raise ValueError("invalid cloud reference")
        cloud = json.loads(checked_blob(load_cloud(ref), ref, 2_000_000))
        if (sensor["extrinsic_ref"]!=calibration or sensor["frame_id"]!=source["frame_id"]
                or cloud["frame_id"]!=source["frame_id"] or cloud["source_id"]!=source["source_id"]
                or cloud["type"]!="livox.mid360.preview" or int(cloud["last_receive_ns"])!=int(source["stamp_ns"])
                or stamp-int(source["stamp_ns"])>300_000_000
                or len(cloud["points"])!=source["sampled_points"] or len(cloud["points"])>8192):
            raise ValueError("cloud identity, time or point count differs from the snapshot")
        origin = sensor["translation_m"]
        for point in cloud["points"]:
            if len(point)!=4 or not all(type(v) in (float,int) and math.isfinite(v) for v in point):
                continue
            x,y,z = project_point(sensor,point)
            if footprint and (-footprint["rear_m"]<=x<=footprint["front_m"] and
                    -footprint["right_m"]<=y<=footprint["left_m"] and z<=TRAVEL_BAND[1]):
                continue
            _ray_cells(origin,(x,y,z),observed)
            if TRAVEL_BAND[0]<=z<HEIGHT and math.hypot(x,y)<RADIUS:
                key = (math.floor(x/CELL),math.floor(y/CELL),math.floor(z/CELL))
                voxels[key]+=1
                if z<=TRAVEL_BAND[1]:
                    occupied.add(key[:2])
    return {"schema_version":1,"frame_id":snapshot["base_frame"],"stamp_ns":str(stamp),
            "voxel_size_m":CELL,"ground_cell_m":CELL,"bounds_m":{"x":[-RADIUS,RADIUS],"y":[-RADIUS,RADIUS],"z":[0.,HEIGHT]},
            "voxels":[[*_centers(key),voxels[key]] for key in sorted(voxels)],
            "observed_ground":[_centers(key) for key in sorted(observed-occupied)],
            "occupied_ground":[_centers(key) for key in sorted(occupied)],
            "source_evidence":{"snapshot_ref":snapshot_ref or evidence.get("snapshot_ref"),
                               "calibration_ref":calibration,"lidars":sources},
            "texture":_camera(evidence.get("camera"),stamp)}
