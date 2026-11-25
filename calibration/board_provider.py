"""DSH read providers for synchronized sensor frames and board geometry."""
import base64
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import sys
from threading import Lock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

LIMIT = 32 * 1024 * 1024


def encode_frame(frame):
    meta = {k:frame[k] for k in ("frames","stamp_ns","stamps_ns") if k in frame}
    archive = io.BytesIO()
    values = {"rgb":frame["rgb"], "k":frame["k"], "metadata":np.array(json.dumps(meta))}
    if frame.get("depth") is not None:
        values["depth"] = frame["depth"]
    values.update({"cloud_" + key:cloud for key,cloud in frame["clouds"].items()})
    np.savez_compressed(archive, **values)
    return base64.b64encode(archive.getvalue()).decode("ascii")


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
        result = recognize(request["target"],frame["rgb"],frame["k"],{"solver": profile},directory)
        return dict(result, **identity)


def measure(request, profile):
    from calibration.board_solver import observe_lidar, distinct_pose
    with observation(request) as (frame, meta, identity):
        if meta["frames"] != {"camera":profile["camera_frame"], **profile["lidar_frames"]}:
            raise ValueError("sensor_frame_mismatch")
        receipt = request["detections"]
        if any(receipt.get(k) != v for k,v in identity.items()) or receipt.get("target") != "checkerboard":
            raise ValueError("board_observation_binding_mismatch")
        detections = receipt["detections"]
        if not detections:
            return {"condition":"board_not_visible"}
        if len(detections) != 1 or detections[0].get("label") != "checkerboard":
            raise ValueError("ambiguous_board_detection")
        camera = detections[0]["features"]["checkerboard_plane"]
        lidars = {key:observe_lidar(frame["cloud_" + key],profile) for key in profile["lidar_frames"]}
        if any(plane is None for plane in lidars.values()):
            return {"condition":"board_missing_in_lidar", "missing":[key for key,p in lidars.items() if p is None]}
        prior = request.get("prior",[])
        if any(item.get("detector") != receipt["detector"] for item in prior):
            raise ValueError("board_detector_changed_between_samples")
        obs = {"id":str(meta["stamp_ns"]), "camera":camera, "lidars":lidars,"detector":receipt["detector"]}
        return {"condition":"distinct_pose" if distinct_pose(obs,prior) else "repeated_pose",
                "observation":obs}


class Handler(BaseHTTPRequestHandler):
    def send(self, code, value):
        data = json.dumps(value,ensure_ascii=False,allow_nan=False).encode()
        self.send_response(code)
        self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Content-Length",str(len(data)))
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
    elif role != "compute":
        raise ValueError("unknown provider role")
    lock = Lock()
    def dispatch(path, req):
        if source is not None and path == "/snapshot":
            with lock:
                frame = source.next_rgb_frame(1.2) if req.get("mode")=="rgb" else source.next_frame(1.2)
            return {"archive":encode_frame(frame) if frame is not None else None}
        if source is None:
            from calibration.board_solver import fit_extrinsics, verify_extrinsics
            config = profile["solver"]
            if path == "/detect":
                return detect(req,config,directory)
            if path == "/measure-board":
                return measure(req, config)
            if path == "/fit":
                return {"candidate":fit_extrinsics(req["observations"],config)}
            if path == "/verify":
                return verify_extrinsics(req["candidate"],req["observations"],config)
            if path == "/fit-clock":
                from calibration.time_offset import estimate_offsets
                return estimate_offsets(req["traces"],req["transforms"])
        raise ValueError("unsupported capability")
    try:
        with ThreadingHTTPServer((os.environ.get("EDGE_CALIBRATION_HTTP_BIND", "0.0.0.0"),8142), Handler) as http:
            http.dispatch = dispatch
            http.serve_forever()
    finally:
        if source is not None:
            source.close()


if __name__ == "__main__":
    main()
