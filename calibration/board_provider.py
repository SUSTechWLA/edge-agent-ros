"""DSH read providers for synchronized sensor frames and board geometry."""
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import time
from threading import Lock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

LIMIT = 32 * 1024 * 1024


class LiveView:
    """A separate camera subscription; an operator opening the console never advances a recorder."""
    def __init__(self, profile, source_factory):
        self.profile = profile
        self.source_factory = source_factory
        self.source = None
        self.lock = Lock()

    def read(self, *, model_only=False):
        if not self.lock.acquire(timeout=.35):
            return {"available":False}
        try:
            if self.source is None:
                self.source = self.source_factory(self.profile, preview_only=True)
            frame = self.source.next_live_rgb_frame()
            if frame is None:
                return {"available":False}
            age = (time.time_ns()-frame["received_unix_ns"])//1_000_000
            if not 0 <= age <= 1500:
                return {"available":False}
            if model_only:
                return stream_camera_model(self.profile, frame)
            import cv2
            rgb = frame["rgb"]
            h,w = rgb.shape[:2]
            scale = min(1.,640/w,480/h)
            if scale < 1.:
                rgb = cv2.resize(rgb,(max(1,round(w*scale)),max(1,round(h*scale))),interpolation=cv2.INTER_AREA)
            ok,jpg = cv2.imencode(".jpg", cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY,72])
            if not ok or jpg.nbytes > 256*1024:
                raise ValueError("live_camera_encoding_failed")
            age = (time.time_ns()-frame["received_unix_ns"])//1_000_000
            if not 0 <= age <= 1500:
                return {"available":False}
            return dict(available=True,source="live_camera",robot_id=self.profile["robot_id"],
                        environment=self.profile["environment"],camera_id=self.profile["camera"]["id"],
                        frame_id=frame["frames"]["camera"],stamp_ns=str(frame["stamp_ns"]),
                        received_unix_ns=str(frame["received_unix_ns"]),width=rgb.shape[1],height=rgb.shape[0],
                        age_ms=age,max_age_ms=1500,jpeg=base64.b64encode(jpg).decode("ascii"))
        finally:
            self.lock.release()

    def close(self):
        with self.lock:
            if self.source is not None:
                self.source.close()


def encode_frame(frame):
    meta = {k:frame[k] for k in ("frames","stamp_ns","stamps_ns","camera_info","navigation") if k in frame}
    archive = io.BytesIO()
    values = {"rgb":frame["rgb"], "k":frame["k"], "metadata":np.array(json.dumps(meta))}
    if frame.get("depth") is not None:
        values["depth"] = frame["depth"]
    values.update({"cloud_" + key:cloud for key,cloud in frame["clouds"].items()})
    np.savez_compressed(archive, **values)
    return base64.b64encode(archive.getvalue()).decode("ascii")


def stream_camera_model(profile, frame):
    camera = profile["camera"]
    if frame["frames"]["camera"] != camera["frame"]:
        raise ValueError("camera_frame_mismatch")
    info = frame["camera_info"]
    width, height = info["resolution"]
    if [height, width] != list(frame["rgb"].shape[:2]):
        raise ValueError("camera_model_resolution_mismatch")
    mode = camera.get("image_geometry","pinhole")
    k = np.asarray(frame["k"],dtype=float).reshape(3,3)
    field = "p" if mode=="rectified" else "k"
    matrix = np.asarray(info[field],dtype=float).reshape(3,-1)
    if (matrix.shape[1] != (4 if field=="p" else 3) or not np.isfinite(k).all()
            or not np.array_equal(k, matrix[:,:3]) or min(k[0,0], k[1,1])<=0
            or not np.allclose(k[2],[0.,0.,1.])):
        raise ValueError("decoded_model_differs_from_camera_info")
    roi = info["roi"]
    full_image = (max(info["binning"]) <= 1 and roi["x_offset"] == roi["y_offset"] == 0
                  and (roi["width"],roi["height"]) in ((0,0),(width,height)))
    raw_d = info["d"]
    if raw_d is not None and len(raw_d)>32:
        raise ValueError("unexpected_camera_distortion_array")
    return dict(outcome="camera_stream_model_read", available=True,
                source="sensor_msgs/msg/CameraInfo", independently_calibrated=False,
                source_geometry=mode, effective_k_source="CameraInfo.P[:3,:3]" if mode=="rectified" else "CameraInfo.K",
                width=width, height=height, fx_px=float(k[0,0]), fy_px=float(k[1,1]),
                cx_px=float(k[0,2]), cy_px=float(k[1,2]), full_frame_geometry=full_image,
                published_d_values=", ".join(format(v,".9g") for v in raw_d) if raw_d else "not supplied",
                factory_provenance="unknown", validation_scope="metadata_consistency_only",
                published_d_scope="raw_image_camera_info", decoded_image_geometry="pinhole",
                distortion_model=info["distortion_model"], reported_d=info["d"],
                camera_id=camera["id"], robot_id=profile["robot_id"], environment=profile["environment"],
                frame_id=camera["frame"], image_topic=camera["topic"], info_topic=camera["info_topic"],
                image_stamp_ns=str(frame["stamps_ns"]["camera"]), info_stamp_ns=str(frame["stamps_ns"]["info"]),
                received_unix_ns=str(frame["received_unix_ns"]), decoded_k=k.ravel().tolist(),
                published_camera_info=info)


@contextmanager
def observation(request):
    raw = base64.b64decode(request["archive"],validate=True)
    with np.load(io.BytesIO(raw),allow_pickle=False) as frame:
        meta = json.loads(str(frame["metadata"]))
        identity = dict(archive_sha256=hashlib.sha256(raw).hexdigest(),
                        stamp_ns=meta["stamps_ns"]["camera"],frame=meta["frames"]["camera"])
        yield frame, meta, identity


def detect(request, profile, directory):
    from perception.target_detection import recognize
    with observation(request) as (frame, meta, identity):
        if identity["frame"] != profile["camera_frame"]:
            raise ValueError("camera_frame_mismatch")
        rgb = frame["rgb"]
        result = recognize(request["target"],rgb,frame["k"],{"solver": profile},directory)
        if request.get("preview") is True:
            import cv2
            height,width = rgb.shape[:2]
            width_out = min(width, 800)
            height_out = max(1,round(height * width_out / width))
            image = cv2.resize(rgb, (width_out,height_out), interpolation=cv2.INTER_AREA)
            scale = np.array([width_out/width,height_out/height])
            for row in result["detections"]:
                for marker,corners in row.get("features",{}).get("marker_corners_px",{}).items():
                    outline=np.rint(np.asarray(corners)*scale).astype(np.int32).reshape(4,2)
                    cv2.polylines(image,[outline],True,(72,235,132),2,cv2.LINE_AA)
                    x,y=outline[0]
                    pos=(max(0,int(x)),max(16,int(y)-4))
                    cv2.putText(image,"ID "+str(marker),pos,cv2.FONT_HERSHEY_SIMPLEX,.5,(0,0,0),3,cv2.LINE_AA)
                    cv2.putText(image,"ID "+str(marker),pos,cv2.FONT_HERSHEY_SIMPLEX,.5,(164,255,191),1,cv2.LINE_AA)
            ok,data = cv2.imencode(".jpg",cv2.cvtColor(image,cv2.COLOR_RGB2BGR),[cv2.IMWRITE_JPEG_QUALITY,82])
            if not ok:
                raise ValueError("camera_preview_encoding_failed")
            result["preview"] = {"jpeg":base64.b64encode(data).decode("ascii"),"size":[width_out,height_out]}
            result["stamps_ns"] = meta["stamps_ns"]
        return dict(result, **identity)


def measure(request, profile, method=None):
    from calibration.board_solver import distinct_pose
    from calibration.methods import selected
    method = method or selected({})
    target = method.spec["target"]
    with observation(request) as (frame, meta, identity):
        if meta["frames"] != {"camera":profile["camera_frame"], **profile["lidar_frames"]}:
            raise ValueError("sensor_frame_mismatch")
        receipt = request["detections"]
        if any(receipt.get(k) != v for k,v in identity.items()) or receipt.get("target") != target:
            raise ValueError("board_observation_binding_mismatch")
        detections = receipt["detections"]
        if not detections:
            return {"condition":"board_not_visible"}
        if len(detections) != 1 or detections[0].get("label") != target:
            raise ValueError("ambiguous_board_detection")
        features = detections[0]["features"]
        camera = features.get(method.spec["plane_feature"])
        if camera is None:
            return {"condition":features.get("condition","camera_board_pose_unavailable")}
        lidars, metrics = {}, {}
        from contextlib import nullcontext
        context = nullcontext({})
        if request.get("prior_clouds"):
            context = np.load(io.BytesIO(base64.b64decode(request["prior_clouds"],validate=True)),allow_pickle=False)
        with context as prior_clouds:
            for key in profile["lidar_frames"]:
                name = "cloud_" + key
                xyz = frame[name]
                if name in prior_clouds:
                    points = prior_clouds[name]
                    if points.ndim!=2 or points.shape[1]!=3 or len(points)>100000:
                        raise ValueError("invalid_stationary_cloud_window")
                    xyz = np.concatenate((points,xyz))
                plane = method.call("observe_lidar",xyz,profile)
                lidars[key] = plane
                metrics[key] = dict(condition="board_observed" if plane else "board_missing_in_lidar",
                                    point_count=len(xyz), stamp_ns=meta["stamps_ns"][key])
                if plane:
                    metrics[key].update({name:plane[name] for name in ("plane_rms_m","hole_fit_rms_m")
                                         if name in plane})
                    if "holes_m" in plane:
                        metrics[key]["hole_count"] = len(plane["holes_m"])
        if any(plane is None for plane in lidars.values()):
            return {"condition":"board_missing_in_lidar", "missing":[key for key,p in lidars.items() if p is None],
                    "lidars":metrics}
        prior = request.get("prior",[])
        if any(item.get("detector") != receipt["detector"] for item in prior):
            raise ValueError("board_detector_changed_between_samples")
        camera = {**camera, "image_size_px":list(frame["rgb"].shape[:2][::-1])}
        obs = {"id":str(meta["stamp_ns"]), "camera":camera, "lidars":lidars,"detector":receipt["detector"]}
        return {"condition":"distinct_pose" if distinct_pose(obs,prior) else "repeated_pose",
                "observation":obs,"lidars":metrics,"stamps_ns":meta["stamps_ns"]}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, value):
        data = json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
        self.send_header("Cache-Control","no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length",0))
            if not 0 < n <= LIMIT:
                return self.send(413,{"error":"invalid_payload_size"})
            req = json.loads(self.rfile.read(n))
            return self.send(200,self.server.dispatch(self.path,req))
        except (ValueError,KeyError,TypeError) as e:
            return self.send(422,{"error":str(e)[:300]})
        except (ConnectionError,TimeoutError):
            return


def main():
    role, = sys.argv[1:]
    profile_path = Path(os.environ["EDGE_CALIBRATION_PROFILE"])
    profile = json.loads(profile_path.read_bytes())
    directory = Path(os.getenv("EDGE_TARGET_DETECTORS",profile_path.parent/"recognizers"))
    source = None
    if role == "observe":
        from calibration.board_ros import BoardSource
        source = BoardSource(profile)
        live = LiveView(profile,BoardSource)
    elif role != "compute":
        raise ValueError("unknown provider role")
    lock = Lock()
    def dispatch(path, req):
        if source is not None and path in ("/live-frame","/camera-stream-model"):
            if req != {}:
                raise ValueError("live preview uses installed camera bindings")
            return live.read(model_only=path=="/camera-stream-model")
        if source is not None and path == "/snapshot":
            with lock:
                mode = req.get("mode")
                frame = (source.next_rgb_frame(1.2) if mode=="rgb" else
                         source.next_frame(1.5, require_navigation=True) if mode=="navigation" else source.next_frame(1.2))
            return {"archive":encode_frame(frame) if frame is not None else None,
                    "streams":source.stream_status() if hasattr(source,"stream_status") else {}}
        if source is None:
            from calibration.methods import selected
            method = selected(profile)
            config = profile["solver"]
            if req.get("method_sha256",method.sha256) != method.sha256:
                raise ValueError("calibration_algorithm_binding_changed")
            geometry_key = method.spec.get("geometry_key")
            if geometry_key:
                # Geometry is supplied by the session's operator-confirmed board profile.
                config = {k:v for k,v in config.items() if k!=geometry_key}
                if "board_metrics" in req:
                    config[geometry_key] = method.metrics(req["board_metrics"])
                elif path in ("/fit","/verify","/measure-board"):
                    raise ValueError("board_geometry_unconfirmed")
            if path == "/detect":
                return detect(req,config,directory)
            if path in ("/camera-sample","/camera-fit","/camera-verify"):
                from calibration import intrinsic_geometry
                if (not profile.get("estimate_intrinsics") or method.spec["id"]!="four_hole_board"
                        or profile["camera"].get("image_geometry")!="pinhole"):
                    raise ValueError("camera_estimation_unavailable")
                if path == "/camera-sample":
                    with observation(req) as (frame, meta, identity):
                        if identity["frame"]!=config["camera_frame"]:
                            raise ValueError("camera_sample_frame_changed")
                        row=intrinsic_geometry.sample(frame["rgb"],frame["k"],
                              frame["cloud_front"] if "cloud_front" in frame.files else None,config,req.get("paired_priors",()))
                        return {"sample":dict(row,id=str(identity["stamp_ns"]),archive_sha256=identity["archive_sha256"]) if row else None}
                if path == "/camera-fit":
                    return intrinsic_geometry.fit(req["training"],config,req["joint"])
                return intrinsic_geometry.verify(req["candidate"],req["holdout"],config)
            if path == "/measure-board":
                return measure(req, config, method)
            if path == "/fit":
                return {"candidate":method.call("fit",req["observations"],config)}
            if path == "/plan-views":
                from calibration.view_planner import plan_views
                return plan_views(req["task_id"],req["operation_id"],req["observation"],req["camera"],
                                  req["prior"],dict(profile,solver=config),method=method,
                                  collection=req.get("collection"))
            if path == "/verify":
                return method.call("verify",req["candidate"],req["observations"],config)
            if path == "/fit-clock":
                if not method.spec.get("clock_supported"):
                    raise ValueError("clock_calibration_method_unavailable")
                from calibration.time_offset import estimate_offsets
                return estimate_offsets(req["traces"],req["transforms"])
        raise ValueError("unsupported capability")
    try:
        with ThreadingHTTPServer((os.environ.get("EDGE_CALIBRATION_HTTP_BIND", "0.0.0.0"),8142), Handler) as http:
            http.dispatch = dispatch
            http.serve_forever()
    finally:
        if source is not None:
            live.close()
            source.close()


if __name__ == "__main__":
    main()
