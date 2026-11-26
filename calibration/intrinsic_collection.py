"""Task-scoped pose collection for a stationary observer and an attended target."""
import base64
import math
import time


MIN_FIT, MIN_HOLDOUT = 7, 3
BOTTOM = {"bottom_left","bottom_center","bottom_right"}
HINTS = {
    "common": "让四个方码和四个圆孔都进入相机与前雷达视野。按当前提示变换板的距离或侧向角度，然后保持不动。",
    "bottom": "把板移近并降低板底一侧，让下排方码进入 RGB 画面的底部。可将板上缘向远离相机的方向倾斜。四个方码都要留在画面内，板底不要穿过地面。",
    "pitch_down": "保持板中心在画面内，将板上缘朝相机倾约 20°，再保持不动。四个方码都需要可见。",
    "pitch_up": "将板上缘朝远离相机的方向倾约 20°，然后保持不动。四个方码都需要可见。",
    "roll_left": "从相机画面看，把左上角压低约 15°，并保持四个方码完整可见。摆好后请保持不动。",
    "roll_right": "从相机画面看，把右上角压低约 15°，并保持四个方码完整可见。摆好后请保持不动。",
    "spread": "把板稍微移向画面侧边或改变距离，再换一个斜向角度。保留完整的四个方码并停稳。",
    "holdout": "请换一个尚未使用的位置或倾角，让相机看全四码。联合验证还需要前雷达看到四孔。本组只用于验证。",
}


def report(samples, joint, *, mobile=False):
    train = [row for row in samples if row["role"]=="fit"]
    held = [row for row in samples if row["role"]=="holdout"]
    occupied = {c for row in train for c in row["cells"]}
    pitch,roll = [0.], [0.]
    for row in train:
        p,r = row["tilt_deg"]
        pitch.append(p)
        roll.append(r)
    fit_pairs = sum(row["lidar"] is not None for row in train)
    held_pairs = sum(row["lidar"] is not None for row in held)
    missing = sorted({f"{y}_{x}" for y in ("top","middle","bottom") for x in ("left","center","right")}-occupied)
    if not train:
        target = "common"
    elif BOTTOM-occupied:
        target = "bottom"
    elif mobile and joint and fit_pairs<4:
        target = "common"
    elif mobile and joint and (len(held)<MIN_HOLDOUT or held_pairs<2):
        # Reserve independent paired views while the upright board still overlaps both sensors.
        target = "holdout"
    elif min(pitch)>-11:
        target = "pitch_down"
    elif max(pitch)<11:
        target = "pitch_up"
    elif min(roll)>-9:
        target = "roll_left"
    elif max(roll)<9:
        target = "roll_right"
    elif joint and fit_pairs<4:
        target = "common"
    elif len(train)<MIN_FIT:
        target = "spread"
    else:
        target = "holdout"
    enough = (target=="holdout" and len(train)>=MIN_FIT and len(held)>=MIN_HOLDOUT
              and (not joint or fit_pairs>=4 and held_pairs>=2))
    return dict(train_count=len(train),holdout_count=len(held),paired_fit=fit_pairs,paired_holdout=held_pairs,
                covered_cells=sorted(occupied),missing_cells=missing,next_pose=target,
                pitch_deg=[round(min(pitch),1),round(max(pitch),1)],
                roll_deg=[round(min(roll),1),round(max(roll),1)],camera_fit_ready=enough)


def distance(first, second):
    return math.sqrt(sum((a-b)**2 for p,q in zip(first["points"],second["points"])
                         for a,b in zip(p,q))/len(first["points"]))


def repeat_view(sample, prior, pixel_limit):
    return (distance(sample,prior)<pixel_limit
            and math.dist(sample["tilt_deg"],prior["tilt_deg"])<5.)


def meets_aim(aim, sample, joint, progress):
    pitch,roll = sample["tilt_deg"]
    if aim=="bottom":
        return bool(set(sample["cells"]) & BOTTOM & set(progress["missing_cells"]))
    if aim=="pitch_down":
        return pitch<=-11
    if aim=="pitch_up":
        return pitch>=11
    if aim=="roll_left":
        return roll<=-9
    if aim=="roll_right":
        return roll>=9
    return not joint or aim not in ("common","holdout") or sample["lidar"] is not None


def collect(station, task_id, task, operation_id, joint, checkpoint, digest, *, attended=True, stopped=None):
    samples = task.setdefault("camera_samples",[])
    mobile = task.get("active_view_requested",False)
    progress = report(samples,joint,mobile=mobile)
    if progress["camera_fit_ready"]:
        return dict(outcome="camera_samples_ready",**progress)
    if len(samples)>=22:
        raise ValueError("camera_sample_budget_exhausted")
    aim = progress["next_pose"]
    guidance = HINTS[aim] if attended else "底盘已停稳，正在采集当前视点的稳定角点和同步点云。"
    if aim=="holdout" and not attended:
        guidance = "底盘已停稳，正在录制独立验证视点。本组数据只用于留出验证。"
    if attended and stopped and aim=="bottom":
        guidance = "保持标定板在原位置，将板上缘向远离相机方向倾斜，让下排方码进入画面底部。保持四码可见，等采集确认。"
    task["capture_feedback"] = dict(progress, guidance=guidance, capture_operation=operation_id)
    if attended and stopped:
        task["capture_feedback"]["capture_reachability"] = task["camera_view_decision"]["reachability"]
    station.note(task,"WAITING_FOR_TILT" if attended else "RECORDING",guidance)
    end = time.monotonic()+(80 if attended else 8)
    paired_capture = joint and (not mobile or aim in ("common","bottom","holdout"))
    paired_priors = [item["camera_pose"] for item in samples if item["lidar"] is not None and "camera_pose" in item]
    previous = None
    while time.monotonic()<end:
        capture = station.service(task,"frame",{"mode":"synchronized" if paired_capture else "rgb"})
        archive = capture.get("archive")
        if not archive:
            continue
        row = station.service(task,"camera-sample",{"archive":archive,"paired_priors":paired_priors}).get("sample")
        if (not row or stopped and int(row["id"]) <= stopped["sensor_stamp_ns"]
                or attended and not meets_aim(aim,row,joint,progress)
                or aim in ("common","holdout") and joint and row["lidar"] is None
                or any(repeat_view(row,p,4.5) for p in samples)):
            previous = None
            continue
        if previous is None or row["id"]==previous["id"] or distance(row,previous)>1.3:
            previous = row
            continue
        if not 130_000_000 <= int(row["id"])-int(previous["id"]) <= 2_000_000_000:
            previous = row
            continue
        kind = "camera-adjust" if attended and task.get("active_view_requested") else "camera-record"
        station.intent(task_id,operation_id,kind)
        raw = base64.b64decode(archive,validate=True)
        sha = digest(raw)
        if sha!=row["archive_sha256"]:
            raise ValueError("camera_sample_archive_changed")
        path = station.root/"camera-captures"/task_id
        path.mkdir(parents=True,exist_ok=True)
        checkpoint(path/(sha+".npz"),raw)
        samples.append(dict(row,role="holdout" if aim=="holdout" else "fit",operation_id=operation_id,
                            source="operator_tilt" if attended else "stationary_view",
                            motion_operation=stopped["operation_id"] if stopped else None))
        task.pop("camera_candidate",None)
        result = report(samples,joint,mobile=mobile)
        task["capture_feedback"] = dict(result,guidance=f"已保存 {len(samples)} 组有效板姿，正在重新计算覆盖。")
        station.note(task,"RECORDED",task["capture_feedback"]["guidance"])
        if task.get("active_view_requested"):
            result.update(camera_operator_needed=False,camera_view_recordable=False)
        return dict(outcome="camera_pose_recorded",camera_candidate_ready=False,**result)
    instruction = f"尚未取得符合要求的稳定角点。{guidance}" if attended else "当前视点没有新增有效角点，请检查可达取景点。"
    station.note(task,"MORE_VIEWS",instruction)
    if task.get("active_view_requested"):
        progress.update(camera_operator_needed=False,camera_view_recordable=False)
    return dict(outcome="camera_pose_missing",**progress)
