"""Read-only stationary clearance review for the registered Gazebo rover footprint."""
import math

from edge_contracts.occupancy import finite

MARGIN_M = .55
FOOTPRINT = (.155, .275, .28, .28)
FOOTPRINT_ID = "office.fixture.gazebo.diff_drive"
SENSOR_X_M = .08
MIN_SAMPLES = 5
MIN_SPAN_NS = 650_000_000
MAX_GAP_NS = 200_000_000
MAX_PAIR_NS = 175_000_000


def dot(a, b):
    return a[0]*b[0] + a[1]*b[1]


def minus(a, b):
    return a[0]-b[0], a[1]-b[1]


def point_gap(p, a, b):
    e, delta = minus(b, a), minus(p, a)
    length = dot(e,e)
    t = max(0.,min(1.,dot(e,delta)/length)) if length else 0.
    return math.hypot(delta[0]-t*e[0],delta[1]-t*e[1])


def box_gap(a, b, hx, hy):
    """A scan boundary edge is unsafe even when only the shadow segment touches."""
    dx, dy = minus(b,a)
    if (min(a[0],b[0]) <= hx and max(a[0],b[0]) >= -hx
            and min(a[1],b[1]) <= hy and max(a[1],b[1]) >= -hy
            and abs(a[0]*dy-a[1]*dx) <= hx*abs(dy)+hy*abs(dx)):
        return 0.
    endpoints = (math.hypot(max(0.,abs(p[0])-hx),max(0.,abs(p[1])-hy)) for p in (a,b))
    corners = (point_gap((x,y),a,b) for x in (-hx,hx) for y in (-hy,hy))
    return min(*endpoints,*corners)


def polygon(scan):
    ranges = scan["ranges"]
    angle, step, end = (scan[k] for k in ("angle_min","angle_increment","angle_max"))
    near, far = scan["range_min"], scan["range_max"]
    if (not isinstance(ranges,list) or not 32 <= len(ranges) <= 1440
            or not all(finite(v) for v in (angle,step,end,near,far)) or not 0 < step <= .12
            or abs(angle+(len(ranges)-1)*step-end) > step/2
            or not 2*math.pi-1.6*step <= end-angle <= 2*math.pi+step/2
            or not 0 <= near < far <= 120 or
            not all(finite(v) and near <= v <= far for v in ranges)):
        return []
    return [(SENSOR_X_M+d*math.cos(angle+i*step),d*math.sin(angle+i*step)) for i,d in enumerate(ranges)]


def observed_gap(scan, bearing, length):
    edges = polygon(scan)
    if not edges:
        return None
    pairs = list(zip(edges[-1:]+edges[:-1],edges))
    if not sum((a[1]>0) != (b[1]>0) and a[0]+(b[0]-a[0])*(-a[1])/(b[1]-a[1])>0
               for a,b in pairs) % 2:
        return None
    co, si = math.cos(bearing),math.sin(bearing)
    front, rear, left, right = FOOTPRINT
    center_x, center_y = (front-rear+length)/2,(left-right)/2
    hx, hy = (front+rear+length)/2,(left+right)/2
    # A whole circumscribed disc covers all headings during the stationary pivot.
    radius = math.hypot(max(front,rear),max(left,right))

    def project(p):
        return co*p[0]+si*p[1]-center_x,-si*p[0]+co*p[1]-center_y

    return min(min(point_gap((0.,0.),a,b)-radius,
                   box_gap(project(a),project(b),hx,hy)) for a,b in pairs)


def predicted_conflict(track, position, ahead, left, length):
    center = [track.get("x_m"),track.get("y_m")]
    radius = track.get("visible_radius_m")
    if not all(finite(v) for v in (*center,radius)) or radius < 0:
        return True
    rel = minus(center,position)
    x,y = dot(rel,ahead),dot(rel,left)
    vx,vy = track.get("vx_mps"),track.get("vy_mps")
    stationary_surface = track.get("matches_stationary_surface") is True
    if stationary_surface or track.get("velocity_valid") is not True or not all(finite(v) for v in (vx,vy)):
        advance = lateral = 0.
    else:
        advance,lateral = dot((vx,vy),ahead)*2.5,dot((vx,vy),left)*2.5
    # The full predicted disc sweeps a capsule. Compute its distance to
    # the rover's swept rectangle using the same Euclidean margin as the scan.
    front, rear, left_width, right_width = FOOTPRINT
    cx = (length + front - rear) / 2
    cy = (left_width - right_width) / 2
    start = x - cx, y - cy
    end = start[0] + advance, start[1] + lateral
    hx = (length + front + rear) / 2
    hy = (left_width + right_width) / 2
    if box_gap(start, end, hx, hy) > radius + MARGIN_M:
        return False
    # Older reports and changing silhouettes keep the conservative disc.
    if track.get("velocity_valid") is not True or not all(finite(v) for v in (vx,vy)):
        return True
    extent = stable_extent(track, radius, math.hypot(vx,vy))
    if extent is None:
        return True
    ex, ey = extent
    sweep_centre = [position[i]+ahead[i]*cx+left[i]*cy for i in (0,1)]
    if all(abs(v-c) <= e for v,c,e in zip(sweep_centre,center,extent)):
        return True
    vertices = []
    for dx,dy in ((-ex,-ey),(ex,-ey),(ex,ey),(-ex,ey)):
        offset = [rel[0]+dx,rel[1]+dy]
        vertices.append((dot(offset,ahead)-cx,dot(offset,left)-cy))
    margin = MARGIN_M + .08 + 2.5*math.hypot(vx,vy)
    return any(box_gap(a,b,hx,hy) <= margin for a,b in zip(vertices[-1:]+vertices[:-1],vertices))


def stable_extent(track, radius, speed):
    """Accept measured odom-aligned support only with a stable, multi-scan fit."""
    extent = [track.get("visible_half_extent_"+axis+"_m") for axis in "xy"]
    span = track.get("span_s")
    if (speed > .035 or not all(finite(v) and v >= 0 for v in extent)
            or abs(math.hypot(*extent)+.08-radius)>1e-6 or not finite(span) or span < .65
            or type(track.get("samples")) is not int or track["samples"] < 5
            or type(track.get("support_rays")) is not int or track["support_rays"] < 3):
        return None
    for keys, limit in ((("early_dx_m","early_dy_m"),.018),(("late_dx_m","late_dy_m"),.018),
                        (("half_extent_rate_x_mps","half_extent_rate_y_mps"),.035)):
        pair = [track.get(k) for k in keys]
        if not all(finite(v) for v in pair) or math.hypot(*pair) > limit:
            return None
    return extent


def sample_review(sample, goal, previous_goal=None):
    scan,pose,report = (sample[k] for k in ("scan","pose","dynamic"))
    stamp,odom = scan["stamp_ns"],pose["stamp_ns"]
    measured = [pose.get(k) for k in ("x","y","yaw","speed","yaw_rate")]
    if (type(stamp) is not int or type(odom) is not int or not all(finite(v) for v in measured)
            or abs(stamp-odom)>MAX_PAIR_NS or pose.get("frame")!="odom" or scan.get("frame")!="laser_link"
            or report.get("frame_id")!="odom" or report.get("valid") is not True
            or report.get("stamp_ns")!=stamp or report.get("odom_stamp_ns")!=odom
            or type(report.get("scan_seq")) is not int or not report.get("scan_ref")):
        return None,"observation_unpaired"
    x,y,yaw,speed,angular = measured
    if abs(speed)>.02 or abs(angular)>.04:
        return None,"body_not_stationary"
    dx,dy = minus(goal,(x,y))
    heading = math.atan2(dy,dx)
    length = math.hypot(dx,dy)
    if not 0 < length <= .8:
        return None,"recheck_segment_invalid"
    tracks = report.get("tracks")
    if not isinstance(tracks,list) or len(tracks)>256 or any(not isinstance(t,dict) for t in tracks):
        return None,"predicted_track_conflict"
    if previous_goal is not None:
        ox, oy = minus(previous_goal,(x,y))
        previous_length = math.hypot(ox,oy)
        if not 0 < previous_length <= .8:
            return None,"recheck_segment_invalid"
        ahead = ox/previous_length,oy/previous_length
        if any(predicted_conflict(t,(x,y),ahead,(-ahead[1],ahead[0]),previous_length) for t in tracks):
            return None,"original_track_conflict"
        # A shorter first chord on the new map route must still prove the full lookahead.
        length = max(length,previous_length)
    gap = observed_gap(scan,heading-yaw,length)
    if gap is None:
        return gap,"swept_corridor_unobserved"
    if gap < MARGIN_M:
        return gap,"insufficient_clearance"
    forward = math.cos(heading),math.sin(heading)
    left = -forward[1],forward[0]
    if any(predicted_conflict(t,(x,y),forward,left,length) for t in tracks):
        return gap,"predicted_track_conflict"
    return gap,"clear_observation"


def review_corridor(body):
    if "native" in body:
        return review_receipt(body["native"])
    if "candidate_goals" in body:
        candidates = body["candidate_goals"]
        if (not isinstance(candidates,list) or not 1 <= len(candidates) <= 5 or "previous_goal" not in body
                or not all(isinstance(p,list) and len(p)==2 and all(finite(v) for v in p) for p in candidates)):
            raise ValueError("invalid_alternative_probes")
        single = {k:v for k,v in body.items() if k!="candidate_goals"}
        first = _review_segment({**single,"goal":candidates[0]})
        if first["result"]=="clear" or not single["samples"]:
            return first
        # A proof must include the newest sample. Skip candidates which already fail there.
        latest = single["samples"][-1]
        for goal in candidates[1:]:
            if sample_review(latest,goal,single["previous_goal"])[1] == "clear_observation":
                proof = _review_segment({**single,"goal":goal})
                if proof["result"]=="clear":
                    return proof
        return first
    return _review_segment(body)


def _review_segment(body):
    goal,samples,not_before,current = (body[k] for k in ("goal","samples","after_ns","clock_ns"))
    previous_goal = body.get("previous_goal")
    if (not isinstance(goal,list) or len(goal)!=2 or not all(finite(v) for v in goal)
            or previous_goal is not None and (not isinstance(previous_goal,list) or len(previous_goal)!=2
                                               or not all(finite(v) for v in previous_goal))
            or not isinstance(samples,list) or len(samples)>12 or type(not_before) is not int or type(current) is not int):
        raise ValueError("corridor_review_request_invalid")
    result = dict(result="waiting",reason="waiting_for_fresh_samples",clearance_m=None,
                  required_clearance_m=MARGIN_M,clear_samples=0,required_samples=MIN_SAMPLES,
                  clear_span_ms=0,last_stamp_ns=None)
    first = previous = previous_seq = previous_odom = None
    closest = math.inf
    for sample in samples:
        stamp = sample["scan"]["stamp_ns"]
        odom = sample["pose"]["stamp_ns"]
        seq = sample["dynamic"].get("scan_seq")
        if type(stamp) is not int or stamp <= not_before or not 0 <= current-stamp <= 1_400_000_000:
            continue
        gap,reason = sample_review(sample,goal,previous_goal)
        if reason != "clear_observation" or (previous is not None and
            (not 0 < stamp-previous <= MAX_GAP_NS or seq!=previous_seq+1 or odom<=previous_odom)):
            first=previous=previous_seq=previous_odom=None
            closest = math.inf
            result.update(clear_samples=0,clear_span_ms=0)
        if reason == "clear_observation":
            if first is None:
                first=stamp
            previous,previous_seq,previous_odom = stamp,seq,odom
            closest=min(closest,gap)
            result.update(clear_samples=result["clear_samples"]+1,clear_span_ms=(stamp-first)//1_000_000)
        result.update(reason=reason,clearance_m=min(gap,closest) if gap is not None else None,last_stamp_ns=stamp)
    if result["clear_samples"]>=MIN_SAMPLES and result["clear_span_ms"]>=MIN_SPAN_NS//1_000_000 and (
            result["last_stamp_ns"] is not None and current-result["last_stamp_ns"] <= 300_000_000):
        result["result"]="clear"
        if previous_goal is not None:
            result.update(previous_goal=previous_goal,goal=goal,original_tracks_clear=True)
    return result


def review_receipt(plan):
    proof = plan.get("dynamic_recheck")
    proof = proof if isinstance(proof,dict) else {}
    count = plan.get("dynamic_clear_samples")
    mode = proof.get("mode")
    required, dwell = (3,240_000_000) if mode in {"tracked_exit","stationary_exterior"} else (8,875_000_000)
    scan,odom = proof.get("scan_stamp_ns"),proof.get("odom_stamp_ns")
    gap = plan.get("clearance_m")
    clear = (plan.get("footprint_ref")==FOOTPRINT_ID and mode in {"tracked_exit","stationary_exterior","observed_corridor"}
             and all(proof.get(field) is True for field in ("corridor_clear","stationary","tracks_clear"))
             and type(scan) is int and type(odom) is int and min(scan,odom)>0 and abs(scan-odom)<=MAX_PAIR_NS
             and type(count) is int and count>=required and finite(gap) and gap>=MARGIN_M
             and type(proof.get("dwell_ns")) is int and proof["dwell_ns"]>=dwell)
    return dict(result="clear" if clear else "waiting",reason=mode or "waiting_for_native_proof",
                clearance_m=gap if finite(gap) else None,required_clearance_m=MARGIN_M,
                clear_samples=count if type(count) is int else 0,required_samples=required,
                last_stamp_ns=scan if type(scan) is int else None)
