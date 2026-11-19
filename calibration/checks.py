"""SDD-038 v1 stationary-fixture acceptance, with measurements expressed in SI units."""
from bisect import bisect_left
import math
import re
from statistics import fmean

KINDS = ("lidar", "rgbd", "imu")
IDENTIFIER = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._:/-]{0,127}\Z")


class Invalid(ValueError):
    def __init__(self, code, detail):
        super().__init__(detail)
        self.code = code


def require(ok, code, detail):
    if not ok:
        raise Invalid(code, detail)


def obj(value):
    return value if isinstance(value, dict) else {}


def ident(value):
    return isinstance(value, str) and bool(IDENTIFIER.fullmatch(value))


def reference(value):
    return isinstance(value, str) and 0 < len(value.strip()) <= 512 and value.isprintable()


def scalar(value):
    return type(value) in (int, float) and -1e12 <= value <= 1e12 and math.isfinite(value)


def vector(value, size):
    return isinstance(value, list) and len(value) == size and all(map(scalar, value))


def count(value, lo, hi):
    return type(value) is int and lo <= value <= hi


def stamp(value):
    return count(value, 1, 2**63-1)


def pinhole(cam):
    w,h,k = cam.get("width"),cam.get("height"),cam.get("k")
    return (count(w, 1, 32768) and count(h, 1, 32768) and vector(k, 9)
            and min(k[0], k[4]) > 0 and 0 <= k[2] < w and 0 <= k[5] < h
            and k[3] == 0 and k[6:] == [0, 0, 1])


def extrinsic(profile, kind, src):
    pose = obj(obj(profile.get("extrinsics")).get(kind))
    require(pose.get("method") in ("target_fit","surveyed_mount") and reference(pose.get("reference")),
            "extrinsic.unmeasured", "提供同一序列号的底盘安装测量记录或已验收工装靶点解。")
    require(pose.get("robot_id") == profile.get("robot_id") and pose.get("serial") == src["serial"]
            and pose.get("parent_frame") == profile.get("base_frame") and pose.get("child_frame") == src["frame"]
            and pose.get("parent_frame") != pose.get("child_frame"),
            "extrinsic.identity", "请核对测量记录中的机器人、序列号及父子坐标系。")
    q = pose.get("rotation_xyzw")
    require(vector(pose.get("translation_m"), 3) and vector(q, 4) and abs(math.hypot(*q)-1) <= .001,
            "extrinsic.transform", "安装测量需提供米制平移及单位四元数 xyzw。")
    if pose["method"] == "target_fit":
        errors = [pose.get(k) for k in ("fit_rms_m", "validation_rms_m", "validation_max_m")]
        require(isinstance(pose.get("input_sha256"),str) and re.fullmatch(r"[0-9a-f]{64}",pose["input_sha256"])
                and count(pose.get("validation_count"), 3, 1000000)
                and all(scalar(x) and x >= 0 for x in errors) and max(errors[:2]) <= .02
                and errors[1] <= errors[2] <= .04, "extrinsic.validation",
                "核查拟合与独立验证靶点数据、米制残差和原始输入哈希。")


def sample_window(samples):
    require(isinstance(samples,list) and 5 <= len(samples) <= 4096,
            "capture.count", "每路需要 5 至 4096 份连续实测。")
    times = []
    previous_rx = 0
    for sample in samples:
        row = obj(sample)
        t,rx = row.get("stamp_ns"),row.get("received_ns")
        require(stamp(t) and stamp(rx), "capture.timestamp", "核查采集时间与同域接收时间（ns）。")
        require(not times or t > times[-1], "capture.repeated_stamp", "采集时间重复或回退。")
        require(rx >= previous_rx and -20_000_000 <= rx-t <= 500_000_000,
                "capture.delay", "检查设备时钟、消息积压与接收时间域。")
        require(not times or t-times[-1] <= 600_000_000, "capture.gap", "连续样本间隙超过 0.6 秒。")
        times.append(t)
        previous_rx = rx
    require(times[-1]-times[0] >= 1_000_000_000, "capture.duration", "每路静止采样至少覆盖 1 秒。")
    return times


def inspect_samples(profile, kind, src, samples):
    if kind == "lidar":
        ratios = []
        for row in samples:
            total,valid = row.get("point_count"),row.get("finite_xyz_count")
            require(count(total, 32, 10000000) and count(valid, 32, total) and valid/total >= .2,
                    "lidar.invalid_cloud", "工装视场内每帧至少需要 32 个有限 XYZ 点，有效率至少为 20%。")
            ratios.append(valid/total)
        return {"min_finite_fraction":min(ratios)}
    if kind == "rgbd":
        cam = obj(profile.get("camera_intrinsics"))
        require(cam.get("serial") == src["serial"] and cam.get("frame") == src["frame"]
                and reference(cam.get("reference")) and pinhole(cam),
                "camera.intrinsics_missing", "提供本机出厂或工装标定档案、分辨率和已标定的针孔内参。")
        ratios = []
        skews = []
        for row in samples:
            require(pinhole(row) and (row["width"],row["height"]) == (cam["width"],cam["height"])
                    and all(abs(a-b) <= .5 for a,b in zip(row["k"],cam["k"])),
                    "camera.intrinsics_mismatch", "CameraInfo 与本机已登记的彩色对齐深度配置不符。")
            t,col,info = row["stamp_ns"],row.get("color_stamp_ns"),row.get("info_stamp_ns")
            require(stamp(col) and stamp(info), "camera.sync", "需要同一光学帧的彩色、对齐深度和内参时间。")
            skew=max(t,col,info)-min(t,col,info)
            require(skew <= 50_000_000, "camera.sync", "彩色、深度及内参时间跨度超过 50 ms。")
            area = row["width"]*row["height"]
            valid = row.get("depth_valid_count")
            require(count(valid, 1, area) and valid/area >= .2, "camera.depth_invalid",
                    "检查工装视场及深度单位，每帧有效正深度像素须达到 20%。")
            ratios.append(valid/area)
            skews.append(skew)
        return {"min_depth_fraction":min(ratios),"max_rgbd_skew_ns":max(skews)}
    for row in samples:
        require(vector(row.get("accel_mps2"),3) and vector(row.get("gyro_rad_s"),3),
                "imu.invalid", "加速度与角速度均应是三个有限 SI 分量。")
    accel = [r["accel_mps2"] for r in samples]
    gyro = [r["gyro_rad_s"] for r in samples]
    mean_a = list(map(fmean, zip(*accel)))
    mean_g = list(map(fmean, zip(*gyro)))
    da = max(math.dist(v, mean_a) for v in accel)
    dg = max(math.dist(v, mean_g) for v in gyro)
    require(9.3 <= math.hypot(*mean_a) <= 10.3 and da <= .45
            and math.hypot(*mean_g) <= .05 and dg <= .025,
            "imu.stationarity", "核对工装固定状态、振动与 IMU 单位，重新采集静止窗口。")
    return {"accel_mean_mps2":mean_a,"gyro_bias_rad_s":mean_g,
            "max_gyro_residual_rad_s":dg,"max_accel_residual_mps2":da}


def overlap(times):
    start = max(ts[0] for ts in times.values())
    end = min(ts[-1] for ts in times.values())
    require(end-start >= 500_000_000, "capture.overlap", "三路需要至少 0.5 秒同时工作的静止窗口。")
    worst = 0
    # Compare nearest source stamps within the shared part of the capture.
    for reference_times in times.values():
        for t in reference_times:
            if not start <= t <= end:
                continue
            for other in times.values():
                pos = bisect_left(other,t)
                neighbours = other[max(0,pos-1):pos+1]
                worst = max(worst,min(abs(t-u) for u in neighbours))
    require(worst <= 250_000_000, "capture.cross_source_skew", "共同窗口内最近异源样本间隔超过 250 ms。")
    return {"common_start_ns":start,"common_end_ns":end,"worst_nearest_skew_ns":worst}


def assess(profile, capture):
    profile, capture = obj(profile),obj(capture)
    issues, stats, times = [], {}, {}
    def check(condition, code, source, detail):
        if not condition:
            issues.append({"code":code,"source":source,"detail":detail})
    for name, packet in (("profile",profile),("capture",capture)):
        check(type(packet.get("schema_version")) is int and packet["schema_version"] == 1,
              "schema.version",name,"需要 schema_version=1 的记录。")
    check(ident(profile.get("robot_id")) and profile.get("environment") in ("simulation","hardware"),
          "profile.identity","profile","提供有效的机体 ID 和运行环境。")
    check(ident(profile.get("base_frame")),"profile.base_frame","profile","底盘坐标系无效。")
    check((capture.get("robot_id"),capture.get("environment")) == (profile.get("robot_id"),profile.get("environment")),
          "capture.identity","capture","采样记录的机体或环境与工装档案不符。")
    check(reference(capture.get("clock_domain")) and reference(capture.get("stationary_reference")),
          "capture.fixture","capture","注明同域时间基准与实物固定台架的记录编号。")
    for channel, detail in obj(capture.get("acquisition_errors")).items():
        if channel in (*KINDS,"color","info","station"):
            check(False,"capture.acquisition",channel,str(detail)[:240])
    for kind in KINDS:
        src = obj(obj(profile.get("sensors")).get(kind))
        obs = obj(obj(capture.get("sensors")).get(kind))
        try:
            require(ident(src.get("serial")) and ident(src.get("frame")) and isinstance(src.get("topic"),str)
                    and re.fullmatch(r"(/[A-Za-z_][A-Za-z0-9_]*)+",src["topic"]),
                    "source.config","登记本机实物序列号、ROS 话题和传感器坐标系。")
            require(bool(obs) and (obs.get("frame") or obs.get("samples")),
                    "capture.missing_stream","窗口内未收到该传感器数据，请检查供电、驱动、主题与 QoS。")
            require(all(src[k] == obs.get(k) for k in ("serial","topic","frame")),
                    "source.mismatch","请核对实物序列号及该设备的 ROS 话题、坐标系。")
            stamps=sample_window(obs.get("samples"))
            times[kind]=stamps
            stats[kind]={"samples":len(stamps),"first_ns":stamps[0],"last_ns":stamps[-1],
                         **inspect_samples(profile,kind,src,obs["samples"])}
            extrinsic(profile,kind,src)
        except Invalid as error:
            check(False,error.code,kind,str(error))
    topics = [obj(obj(profile.get("sensors")).get(k)).get("topic") for k in KINDS]
    check(all(isinstance(x,str) for x in topics) and len(set(str(x) for x in topics)) == 3,
          "source.alias","profile","三路消息类型不同，必须分别配置话题。")
    poses = {}
    for kind in KINDS:
        pose = obj(obj(profile.get("extrinsics")).get(kind))
        frame = pose.get("child_frame")
        if ident(frame):
            value = (pose.get("parent_frame"),pose.get("translation_m"),pose.get("rotation_xyzw"))
            check(frame not in poses or poses[frame] == value, "extrinsic.alias",kind,
                  "同一 TF 子坐标系出现不一致的安装测量，请核对帧名与安装记录。")
            poses[frame] = value
    if len(times) == len(KINDS):
        try:
            stats["timing"] = overlap(times)
        except Invalid as error:
            check(False,error.code,"capture",str(error))
    return issues,stats
