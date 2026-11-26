"""Replay a four-hole station archive offline, without access to ROS, the model or a robot."""
import base64
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from calibration import board_provider
from calibration.board_clip import cloud_context, stationary
from calibration.methods import selected
from four_hole_scene import score


def replay(folder):
    publication = json.loads((folder/"revision.json").read_bytes())
    profile = json.loads((folder/"profile.json").read_bytes())
    method = selected(profile)
    if publication["method_sha256"] != method.sha256:
        raise ValueError("installed spatial calibration adapter differs from the recording")
    config = {**profile["solver"],"four_hole_board":method.metrics(publication["board"]["metrics"])}
    detectors = Path(board_provider.__file__).parent/"station/recognizers"
    poses = []
    frame_count = 0
    with ZipFile(folder/"observations.zip") as captured:
        def read(sha, suffix):
            data = captured.read(sha+suffix)
            if hashlib.sha256(data).hexdigest() != sha:
                raise ValueError("corrupt capture: " + sha)
            return data
        for sha in publication["samples"]:
            clip = json.loads(read(sha, ".json"))
            tracks = []
            for meta in clip["frames"]:
                data = read(meta["sha256"],".npz")
                req = {"archive":base64.b64encode(data).decode("ascii"),"target":method.spec["target"]}
                receipt = board_provider.detect(req,config,detectors)
                if receipt["detector"] != clip["detector"]:
                    raise ValueError("recorded recognizer differs from installed source")
                tracks.append(dict(archive=req["archive"], detections=receipt))
                frame_count += 1
            mid = len(tracks)//2
            measured=[]
            for part in (tracks[:mid],tracks[mid:]):
                request = {**part[-1],"prior":poses}
                if len(part)>1:
                    request["prior_clouds"]=cloud_context(part[:-1])
                sample = board_provider.measure(request, config, method)
                if sample["condition"]!="distinct_pose":
                    raise ValueError("could not reproduce " + sample["condition"])
                measured.append(sample["observation"])
            for key in config["lidar_frames"]:
                if not stationary(measured[0]["lidars"][key],measured[1]["lidars"][key],method.spec["clip"]):
                    raise ValueError("motion in recorded clip")
            poses.append(measured[-1])
    count=method.spec["min_fit"]
    candidate = method.call("fit",poses[:count],config)
    report = method.call("verify",candidate,poses[count:],config)
    if report != publication["report"]:
        raise ValueError("recomputed frozen report differs from the committed result")
    scores = score(dict(publication,report=report))
    return dict(status="VERIFIED",frame_count=frame_count,clip_count=len(poses),
                exact_report_match=True,source_mount_error=scores["independent_truth"])


if __name__=="__main__":
    print(json.dumps(replay(Path(sys.argv[1])),ensure_ascii=False,indent=2))
