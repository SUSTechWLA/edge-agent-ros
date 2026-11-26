"""Propose stationary capture viewpoints from a measured metric target in a stamped optical frame."""
import hashlib
import json
import math
from pathlib import Path

import cv2
import numpy as np

from calibration import board_solver, checks, intrinsic_collection, intrinsic_information, view_coverage
from calibration.targets import rotation_xyzw
from edge_contracts import capture_navigation as nav
from edge_contracts.spatial_facts import rotation


VERSION = "5"
SOURCE_SHA = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def camera_mount(profile, optical_frame):
    camera = profile.get("camera", {})
    if not (checks.ident(profile.get("robot_id")) and checks.ident(profile.get("base_frame"))
            and checks.reference(camera.get("serial")) and camera.get("frame") == optical_frame):
        return None
    try:
        checks.extrinsic(profile, "rgbd", camera)
    except checks.Invalid:
        return None
    pose = profile["extrinsics"]["rgbd"]
    transform = np.asarray(rotation(pose["translation_m"], pose["rotation_xyzw"]))
    if np.linalg.norm(transform[:2, 2]) < .2:
        return None  # A near-vertical optical axis has no reliable look-at heading on the ground.
    return transform[:, :3], transform[:, 3], pose["reference"]


def measured_footprint(plane, k, distortion, width, height):
    camera = {"width":width, "height":height, "k":np.asarray(k).flatten().tolist()}
    if not checks.pinhole(camera):
        raise ValueError("invalid_intrinsics")
    k = np.array(camera["k"]).reshape(3, 3)
    center = np.asarray(plane["center_m"], dtype=float)
    normal = board_solver._normal(plane)
    px = np.asarray(plane["corners_px"], dtype=float)
    if (px.ndim != 2 or px.shape[1] != 2 or not 4 <= len(px) <= 1024 or not np.isfinite(px).all()
            or not checks.scalar(plane.get("reprojection_rms_px")) or not 0 <= plane["reprojection_rms_px"] <= 2.5
            or center[2] <= 0 or np.any(px < 0) or np.any(px >= [width, height])):
        raise ValueError("unusable_target_footprint")
    rays = cv2.undistortPoints(px[:, None], k, distortion).reshape(-1, 2)
    rays = np.column_stack((rays, np.ones(len(rays))))
    denom = rays @ normal
    if np.min(denom) <= .05:
        raise ValueError("grazing_target_observation")
    return k, center, normal, rays * (plane["offset_m"] / denom)[:, None]


def candidate_poses(center, k, width, points, mount, profile, *, off_axis=False):
    if mount is not None:
        r_mount, t_mount, reference = mount
        target_base = r_mount @ center + t_mount
        heading = math.atan2(r_mount[1, 2], r_mount[0, 2])
        current_range = np.linalg.norm((r_mount @ center)[:2])
    else:
        current_range = math.hypot(center[0], center[2])
    half_fov = math.atan2(width/2, k[0, 0])
    # Search a patch of viewing angles and ranges. No execution order is implied.
    angle_span = min(math.radians(35), .85 * half_fov)
    feature_span = np.linalg.norm(np.ptp(points, axis=0))
    low = max(.65 * current_range, .5 * feature_span / math.tan(half_fov))
    high = max(1.55 * current_range, 1.3 * low)
    for distance in np.linspace(low, high, 5):
        for angle in np.linspace(-angle_span, angle_span, 7):
            # Decouple camera bearing from travel so accepted target centres can span the image.
            offsets = (-.23*half_fov, 0., .23*half_fov) if off_axis else (0.,)
            for offset in offsets:
                yaw_goal = angle + offset
                c,s = math.cos(yaw_goal),math.sin(yaw_goal)
                if mount is None:
                    r = np.array([[c,0,s], [0,1,0], [-s,0,c]])
                    position = np.array([center[0]-distance*math.sin(angle), 0., center[2]-distance*math.cos(angle)])
                    yield position, r, None
                else:
                    yaw = np.array([[c,-s,0], [s,c,0], [0,0,1]])
                    optical_xy = target_base[:2] - distance*np.array([math.cos(heading+angle), math.sin(heading+angle)])
                    base_xy = optical_xy - (yaw @ t_mount)[:2]
                    position = r_mount.T @ (np.r_[base_xy, 0.] + yaw @ t_mount - t_mount)
                    goal = {"reference_frame":profile["base_frame"], "x_m":float(base_xy[0]), "y_m":float(base_xy[1]),
                            "yaw_rad":float(yaw_goal), "mount_reference":reference}
                    yield position, r_mount.T @ yaw @ r_mount, goal


def target_outline(plane, profile):
    """Convert the observed marker axes to the full physical panel outline."""
    corners = np.asarray(plane["all_marker_corners_m"], dtype=float).reshape(4, 4, 3)
    horizontal = corners[:, 1] - corners[:, 0]
    vertical = corners[:, 3] - corners[:, 0]
    axes = np.stack((horizontal.mean(axis=0), vertical.mean(axis=0)))
    scale = np.linalg.norm(axes, axis=1)
    if not np.isfinite(axes).all() or np.any(scale < .01):
        raise ValueError("invalid_target_outline")
    width, height = profile["board_asset"]["outline_m"]
    offsets = np.array([[-width,-height], [width,-height], [width,height], [-width,height]])/2
    return np.asarray(plane["center_m"]) + offsets @ (axes / scale[:,None])


def tracked_poses(center, mount, profile):
    """Sample short displacements and independent capture headings while still facing the target."""
    r, t, reference = mount
    target = r @ center + t
    camera_yaw = math.atan2(r[1,2], r[0,2])
    bearing = math.atan2(target[1], target[0]) - camera_yaw
    active = bool(profile.get("capture_navigation"))
    distances = (.18, .28, .42, .56, .74, .86) if active else (.14, .22, .32, .44)
    if profile["view_tracking"].get("reverse_limit_mps"):
        distances += tuple(-d for d in distances)
    span = .49 if active else .37
    for distance in distances:
        for heading in np.linspace(-span,span,9 if active else 7) + bearing:
            x,y = distance * np.array([math.cos(heading), math.sin(heading)])
            look = math.atan2(target[1]-y, target[0]-x) - camera_yaw
            offsets = (-.29, -.17, 0., .17, .29) if active else (-.24,-.12,0.,.12,.24)
            for offset in offsets:
                yaw = look + offset
                c,s = math.cos(yaw),math.sin(yaw)
                turn = np.array([[c,-s,0], [s,c,0], [0,0,1]])
                position = r.T @ (np.array([x,y,0]) + turn @ t - t)
                goal = dict(reference_frame=profile["base_frame"], x_m=float(x), y_m=float(y),
                            yaw_rad=float(yaw), mount_reference=reference)
                yield position, r.T @ turn @ r, goal


def path_margin(points, goal, mount, k, distortion, width, height):
    """Project the full footprint through the executor's three motion phases."""
    x,y,yaw = (goal[key] for key in ("x_m","y_m","yaw_rad"))
    heading = math.atan2(y,x) if x > 0 else math.atan2(-y,-x)
    if max(abs(heading), abs(yaw)) > math.pi/2:
        return -1.
    phases = [
        (0., 0., angle) for angle in np.linspace(0., heading, 1+math.ceil(abs(heading)/.04))
    ] + [
        (x*f, y*f, heading) for f in np.linspace(0., 1., 1+math.ceil(math.hypot(x,y)/.035))
    ] + [
        (x, y, angle) for angle in np.linspace(heading, yaw, 1+math.ceil(abs(heading-yaw)/.04))
    ]
    r,t,_ = mount
    base_points = points @ r.T + t
    projected = []
    for px,py,theta in phases:
        c,s = math.cos(theta), math.sin(theta)
        base_turn = np.array([[c,-s,0], [s,c,0], [0,0,1]])
        projected.append(((base_points-[px,py,0]) @ base_turn - t) @ r)
    optical = np.concatenate(projected)
    if np.min(optical[:,2]) <= .05:
        return -1.
    uv = cv2.projectPoints(optical, np.zeros(3), np.zeros(3), k, distortion)[0].reshape(-1,2)
    return float(np.minimum(uv, [width-1,height-1]-uv).min())


def plan_views(task_id, operation_id, obs, camera, prior, profile, *, method=None, collection=None):
    source = dict(task_id=task_id, operation_id=operation_id, archive_sha256=obs.get("archive_sha256"),
                  stamp_ns=obs.get("camera_stamp_ns"), camera_frame=obs.get("camera_frame"),
                  profile_sha256=obs["profile_sha256"], method_sha256=obs["method_sha256"],
                  planner_version=VERSION, planner_sha256=SOURCE_SHA)
    result = dict(outcome="no_measured_camera_pose", source=source, observation=obs, scope="geometric_view_proposals",
                  candidate_count=0, candidate_refs="", goal_frame=source["camera_frame"], candidates=[], view_options=[],
                  base_goal_available=False, reachability_checked=False, lidar_visibility_predicted=False,
                  view_summary="缺少可用于取景规划的相机度量观测。")
    if not camera:
        return result
    plane, width, height = camera["plane"], camera["width"], camera["height"]
    dist = np.asarray(profile["solver"].get("distortion_coeffs", []), dtype=float)
    if dist.ndim != 1 or len(dist) not in (0, 4, 5, 8, 12, 14) or not np.isfinite(dist).all():
        raise ValueError("invalid_view_projection_distortion")
    k, center, normal, footprint = measured_footprint(plane, camera["k"], dist, width, height)
    modules = plane.get("marker_modules_per_side")
    if modules is not None:
        # The target recognizer supplies marker corners in board order, including temporarily unseen markers.
        footprint = np.asarray(plane["all_marker_corners_m"], dtype=float)
        if (not checks.count(modules, 3, 64) or footprint.ndim != 2 or footprint.shape[1] != 3
                or not 4 <= len(footprint) <= 256 or len(footprint) % 4 or not np.isfinite(footprint).all()):
            raise ValueError("invalid_marker_layout_for_view_planning")
    mount = camera_mount(profile, source["camera_frame"])
    rejected = dict(back_facing=0, cropped_or_small=0, duplicate=0)
    tracking = profile.get("view_tracking") if mount else None
    spatial = profile.get("capture_navigation") if tracking else None
    if spatial:
        stamp = int(source["stamp_ns"])
        try:
            context = camera.get("navigation") or {}
            robot = nav.pose_at(stamp,context.get("robot"))
            perimeter = nav.scan_at(stamp,context.get("scan"))
        except ValueError as error:
            return dict(result,outcome=str(error),view_summary="等待同一时刻的启动局部位姿与完整周界扫描。")
        local = (mount[0] @ center + mount[1]).tolist()
        bearing = ("rear" if local[0] < 0 else "front_left" if local[1] > .2 else
                   "front_right" if local[1] < -.2 else "front")
        result["navigation_context"] = dict(stamp_ns=stamp,robot=robot, nearest_observed_m=nav.sectors(perimeter),
                                           target=dict(position_m=nav.world_position(robot,local),
                                                       range_m=math.hypot(*local[:2]),bearing=bearing))
        source["origin_epoch"] = robot["epoch"]
        source["perimeter_stamp_ns"] = perimeter["stamp_ns"]
        result["reachability_checked"] = True
        observed_coverage = view_coverage.coverage(prior)
        result["captured_lateral_left_m"] = round(observed_coverage["left_m"],3)
        result["captured_lateral_right_m"] = round(observed_coverage["right_m"],3)
        result["captured_rgb_cells"] = ", ".join(observed_coverage["cells"])
        rejected["outside_observed_clearance"] = 0
    if collection:
        result.update(camera_operator_needed=False, camera_view_recordable=False)
        plane_points = plane.get("corners_px", [])
        if len(plane_points) == 16 and (not collection["needs_pair"]
                                       or obs.get("outcome") in ("distinct_pose", "repeated_pose")):
            axis = footprint[1]-footprint[0]
            current = dict(points=plane_points,tilt_deg=[math.degrees(math.atan2(normal[1],normal[2])),
                                                        math.degrees(math.atan2(axis[1],axis[0]))])
            result["camera_view_recordable"] = all(
                not intrinsic_collection.repeat_view(current,row,4.5)
                for row in collection["samples"])
        observed_information = intrinsic_information.accumulated_information(
            (row["camera"] for row in prior if row.get("role")=="fit"),k,dist,[width,height])
    if tracking:
        boundary = target_outline(plane, profile) if tracking.get("outline_from_board") else footprint
        rejected["outside_path_view"] = 0
    poses = (tracked_poses(center, mount, profile) if tracking else
             candidate_poses(center, k, width, footprint, mount, profile,
                             off_axis=bool(method and "score_view_geometry" in method.spec["entrypoints"])))
    if modules:
        rejected["low_marker_resolution"] = 0
    # A surveyed mounting prior is optional. It screens candidate viewpoints only;
    # neither the fit nor the held-out verifier consumes this prior.
    envelope = profile.get("capture_view_envelope")
    if envelope and mount:
        bounds = envelope.get("elevation_deg", [])
        ranges_m = profile["solver"]["capture_range_m"]
        q = envelope.get("rotation_xyzw")
        if (not checks.reference(envelope.get("reference")) or
                envelope.get("frame") not in profile["solver"]["lidar_frames"].values() or
                not checks.vector(envelope.get("translation_m"),3) or not checks.vector(q,4) or
                abs(math.hypot(*q)-1) > .001 or not checks.vector(bounds,2) or
                not -90 <= bounds[0] < bounds[1] <= 90 or not checks.vector(ranges_m,2) or
                not 0 <= ranges_m[0] < ranges_m[1]):
            raise ValueError("view_envelope_binding_mismatch")
        lidar_mount = np.array(rotation(envelope["translation_m"], envelope["rotation_xyzw"]))
        features = np.asarray(plane.get("holes_m", plane.get("projective_hole_centers_m", footprint)))
        extent = .5 * profile["solver"].get("four_hole_board", {}).get("hole_diameter_m", 0.)
        min_el, max_el = map(math.radians, envelope["elevation_deg"])
        min_range, max_range = profile["solver"]["capture_range_m"]
        rejected["outside_sensor_envelope"] = 0

    def lidar_view_gap(in_base):
        scan = (in_base-lidar_mount[:,3]) @ lidar_mount[:,:3]
        ranges = np.linalg.norm(scan, axis=1)
        elevation = np.arctan2(scan[:,2], np.hypot(scan[:,0],scan[:,1]))
        span = np.arcsin(np.clip(extent/np.maximum(ranges,.001),0,1))
        return float(max(0.,np.max(min_el-elevation+span),np.max(elevation+span-max_el),
                         np.max(min_range-ranges+extent),np.max(ranges+extent-max_range)))

    current_lidar_gap = lidar_view_gap(features @ mount[0].T + mount[1]) if envelope and mount else 0.
    pool = []
    score_views = method and "score_view_geometry" in method.spec["entrypoints"]
    if score_views:
        role = method.sample_role(len(prior))
        planes = method.sample_groups(row["camera"] for row in prior)[role]
    measured = [*prior, {"camera":plane}]
    evaluated_paths = {}
    for position, orientation, goal in poses:
        target = orientation.T @ (center-position)
        n = orientation.T @ normal
        offset = float(n @ target)
        points = (footprint-position) @ orientation
        gap = 0.
        if envelope and mount:
            # Express the measured hole centres in the future optical frame, then in the scanner.
            in_base = ((features-position) @ orientation) @ mount[0].T + mount[1]
            gap = lidar_view_gap(in_base)
            if gap:
                if not collection or (collection["needs_pair"] and gap >= current_lidar_gap-.005):
                    rejected["outside_sensor_envelope"] += 1
                    continue
        if offset <= .25*np.linalg.norm(target) or np.min(points[:, 2]) <= .05:
            rejected["back_facing"] += 1
            continue
        uv = cv2.projectPoints(points, np.zeros(3), np.zeros(3), k, dist)[0].reshape(-1,2)
        margin = np.minimum(uv, np.array([width-1,height-1])-uv).min()
        if (not np.isfinite(uv).all() or margin < .03*min(width,height)
                or np.any(np.ptp(uv,axis=0) < .10*np.array([width,height]))):
            rejected["cropped_or_small"] += 1
            continue
        predicted = dict(normal=n.tolist(), center_m=target.tolist(), offset_m=offset)
        if score_views:
            quality = method.call("score_view_geometry",[*planes,predicted])
            value, threshold = quality["value"], quality["preferred_minimum"]
            if not checks.ident(quality["metric"]) or not all(checks.scalar(n) and 0<=n<=1 for n in (value,threshold)):
                raise ValueError("invalid_method_view_geometry_score")
            predicted.update(geometry_score=round(value,5),geometry_metric=quality["metric"],sample_role=role,
                             image_offset_px=round(float(k[0,0]*target[0]/target[2]),1))
        if modules:
            # Min perpendicular projected height of each code is conservative under oblique viewing.
            corners = uv.reshape(-1,4,2)
            edges = np.roll(corners,-1,axis=1)-corners
            lengths = np.linalg.norm(edges,axis=2)
            diagonals = np.roll(corners,-2,axis=1)-corners
            cross = edges[:,:,0]*diagonals[:,:,1] - edges[:,:,1]*diagonals[:,:,0]
            pitch = np.min(np.abs(cross)/np.maximum(lengths,1.e-9)) / modules
            if pitch < 2.5:
                rejected["low_marker_resolution"] += 1
                continue
            predicted["min_marker_module_px"] = round(float(pitch),2)
        if not board_solver.distinct_pose({"camera":predicted}, measured):
            rejected["duplicate"] += 1
            continue
        if tracking:
            path_px = path_margin(boundary, goal, mount, k, dist, width, height)
            # Reserve additional space for wheel slip and the steering transient
            # during active lateral passes. Prediction alone never authorizes motion.
            path_fraction = .09 if spatial else .04
            if not math.isfinite(path_px) or path_px < path_fraction*min(width,height):
                rejected["outside_path_view"] += 1
                continue
            goal["tracking"] = dict(target=tracking["target"], min_margin_px=round(path_px, 1))
        if spatial:
            xy = (goal["x_m"],goal["y_m"])
            if xy not in evaluated_paths:
                evaluated_paths[xy] = nav.clearance(perimeter, xy)
            clear = evaluated_paths[xy] - spatial["swept_radius_m"]
            if clear <= 0:
                rejected["outside_observed_clearance"] += 1
                continue
            side = view_coverage.lateral_m(plane,position)
            limit = spatial["minimum_lateral_m"]
            if side is None:
                continue
            cells = view_coverage.image_cells(uv,[width,height])
            identified = "left" if side >= limit else "right" if side <= -limit else "center"
            predicted.update(lateral_m=round(side,3),board_view=identified,
                             new_rgb_cells=len(cells-set(observed_coverage["cells"])),
                             clearance_margin_m=round(clear,3),
                             fills_missing_side=(identified != "center" and observed_coverage[identified+"_m"] < limit))
        if collection:
            axis = points[1]-points[0]
            angles = [math.degrees(math.atan2(n[1], n[2])), math.degrees(math.atan2(axis[1], axis[0]))]
            sample = dict(tilt_deg=angles, cells=view_coverage.image_cells(uv,[width,height]),
                          lidar=True if gap==0 else None, points=uv.tolist())
            if any(intrinsic_collection.repeat_view(sample,row,6.) for row in collection["samples"]):
                continue
            predicted.update(helps_camera_coverage=intrinsic_collection.meets_aim(
                collection["aim"],sample,collection["joint"],collection),camera_pitch_deg=round(angles[0],1),
                          camera_roll_deg=round(angles[1],1), lidar_view_gap=gap,
                          intrinsic_information_gain=intrinsic_information.information_gain(
                              observed_information,points,k,dist,[width,height]))
        predicted["min_feature_margin_px"] = round(float(margin), 1)
        pose = {"reference_frame":source["camera_frame"], "position_m":position.tolist(),
                "rotation_xyzw":rotation_xyzw(orientation)}
        identity = json.dumps([source, pose], sort_keys=True, allow_nan=False).encode()
        row = {"candidate_ref":"view."+hashlib.sha256(identity).hexdigest()[:20], "camera_pose":pose,
               "predicted_camera":predicted}
        if goal is not None:
            row["base_goal"] = goal
        pool.append(row)
    if collection and result["reachability_checked"]:
        useful = [row for row in pool if row["predicted_camera"]["helps_camera_coverage"]]
        aim = collection["aim"]
        missing_angle = aim in ("pitch_up","pitch_down","roll_left","roll_right")
        assessed = dict(aim=aim,axes=["x","y","yaw"], evaluated_safe_views=len(pool),
                        useful_safe_views=len(useful), stamp_ns=source["stamp_ns"], operation_id=operation_id)
        result["capture_reachability"] = assessed
        if result["camera_view_recordable"]:
            pool = []
        elif useful:
            pool = useful
        elif missing_angle or aim == "bottom":
            # Use reachable baselines first. Once another route cannot supply the requested
            # view, a separate attended tool can collect the missing angle at rest.
            if collection["verified_move"] and (len(collection["samples"]) >= 3 or not pool):
                result["camera_operator_needed"] = True
                pool = []
    # Choose a small diverse subset in calibration-observable normal/range space.
    # The controller, localization and obstacles have not been queried.
    def difference(a, b):
        angle = math.acos(float(np.clip(np.dot(a["normal"], b["normal"]), -1., 1.)))
        return math.hypot(angle / math.radians(6), (a["offset_m"]-b["offset_m"]) / .10)

    chosen = []
    if score_views and pool and not spatial:
        # The solver uses the same sample partition. A low-rank initialization can still
        # use the full constellation, so retain the best attainable views if none pass.
        best_ratio = max(r["predicted_camera"]["geometry_score"] for r in pool)
        floor = min(threshold,.75*best_ratio)
        pool = [r for r in pool if r["predicted_camera"]["geometry_score"] >= floor]
    # Reserve one nearby, informative alternative before considering larger baselines.
    if spatial and pool:
        limit = spatial["minimum_lateral_m"]
        missing_left = observed_coverage["left_m"] < limit
        missing_right = observed_coverage["right_m"] < limit
        direction = int(missing_left)-int(missing_right)
        def preference(row):
            p = row["predicted_camera"]
            progress = round(direction*p["lateral_m"]/.05)
            margin = row["base_goal"]["tracking"]["min_margin_px"]
            paired_progress = -p["lidar_view_gap"] if collection and collection["needs_pair"] else 0.
            return (p.get("helps_camera_coverage",False), paired_progress, p["fills_missing_side"], progress, p["new_rgb_cells"],
                    round(p.get("intrinsic_information_gain",0.),3), round(margin/12),
                    p.get("geometry_score",0), abs(p["lateral_m"]))

        # Retain alternatives on different sides so the model can choose against the
        # same evidence, while favouring the unobserved coverage.
        for label in ("left", "right", "center"):
            group = [row for row in pool if row["predicted_camera"]["board_view"] == label]
            if group:
                chosen.append(max(group,key=preference))
        remaining = [r for r in pool if r not in chosen]
        if remaining:
            chosen.append(max(remaining,key=preference))
        chosen.sort(key=preference,reverse=True)
    elif pool:
        closest = min(pool, key=lambda r:np.linalg.norm(r["camera_pose"]["position_m"]))
        chosen.append(closest)
        measured.append({"camera":closest["predicted_camera"]})
        pool = [r for r in pool if board_solver.distinct_pose({"camera":r["predicted_camera"]}, measured)]
    while not spatial and pool and len(chosen) < (4 if score_views else 6):
        best = max(pool, key=lambda row:min(difference(row["predicted_camera"], other["camera"]) for other in measured))
        chosen.append(best)
        measured.append({"camera":best["predicted_camera"]})
        pool = [p for p in pool if board_solver.distinct_pose({"camera":p["predicted_camera"]}, measured)]
    if not spatial:
        chosen.sort(key=lambda row:np.linalg.norm(row["camera_pose"]["position_m"]))
    if score_views and not spatial:
        chosen.sort(key=lambda row:row["predicted_camera"]["geometry_score"],reverse=True)
    count = len(chosen)
    base_goals = bool(mount and count)
    options = []
    for row in chosen:
        target = row["predicted_camera"]
        distance = np.linalg.norm(target["center_m"])
        tilt = math.acos(float(np.clip(target["offset_m"]/distance, -1., 1.)))
        origin = row["camera_pose"]["position_m"]
        option = dict(ref=row["candidate_ref"], optical_x_m=round(origin[0],3), optical_z_m=round(origin[2],3),
                      target_range_m=round(float(distance),3), obliquity_deg=round(math.degrees(tilt),1),
                      margin_px=target["min_feature_margin_px"], displacement_m=round(float(np.linalg.norm(origin)),3))
        if "min_marker_module_px" in target:
            option["min_marker_module_px"] = target["min_marker_module_px"]
        if score_views:
            option.update({k:target[k] for k in ("geometry_score","geometry_metric","sample_role","image_offset_px")})
            if target["geometry_metric"] == "anchor_ratio":
                option["anchor_ratio"] = target["geometry_score"]
        if "base_goal" in row:
            option["base_xy_yaw"] = [round(row["base_goal"][key],4) for key in ("x_m","y_m","yaw_rad")]
            if "tracking" in row["base_goal"]:
                option["path_margin_px"] = row["base_goal"]["tracking"]["min_margin_px"]
        if spatial:
            goal = row["base_goal"]
            motion = "forward" if goal["x_m"] > 0 else "reverse"
            direction = "left" if goal["y_m"] > .065 else "right" if goal["y_m"] < -.065 else "straight"
            option.update(path=f"{motion}_{direction}", board_view=target["board_view"],
                          board_lateral_m=target["lateral_m"], clearance_margin_m=target["clearance_margin_m"],
                          new_rgb_cells=target["new_rgb_cells"], fills_missing_side=target["fills_missing_side"],
                          end_xyz_m=[round(v,3) for v in nav.world_position(robot,[goal["x_m"],goal["y_m"],0.])],
                          end_heading_deg=round(math.degrees(math.remainder(robot["yaw_rad"]+goal["yaw_rad"],math.tau)),1))
        if collection:
            option.update(camera_pitch_deg=target["camera_pitch_deg"],camera_roll_deg=target["camera_roll_deg"],
                          helps_camera_coverage=target["helps_camera_coverage"],
                          intrinsic_information_gain=round(target["intrinsic_information_gain"],5))
            if collection["needs_pair"]:
                option["lidar_view"] = "predicted_overlap" if target["lidar_view_gap"]==0 else "towards_overlap"
        options.append(option)
    outcome = ("capture_current_view" if result.get("camera_view_recordable") else
               "operator_angle_needed" if result.get("camera_operator_needed") else
               "candidate_views" if count else "no_distinct_visible_candidate")
    return dict(result, outcome=outcome,
                candidate_count=count, candidate_refs=", ".join(p["candidate_ref"] for p in chosen), candidates=chosen,
                view_options=options,
                base_goal_available=base_goals, goal_frame=profile["base_frame"] if base_goals else source["camera_frame"],
                rejected=rejected, nominal_lidar_envelope_ref=envelope["reference"] if envelope and mount else None,
                extent_source="observed_target_features", size_px=[width,height],
                assumptions="fixed_target; planar_base" if mount else "fixed_target; optical_y_axis_rotation",
                view_summary=(f"{count} 个不同视角，按当前相机的靶标特征投影筛选。"
                              + ("含当前底盘帧的候选终点与朝向。" if base_goals else "仅含相机光学坐标，缺少可用的底盘目标。")
                              + ("已按整个换位路径的相机可见性筛选。" if tracking else "")
                              + "各候选仍需检查通行条件并实测雷达可见性。"))
