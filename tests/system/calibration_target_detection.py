"""Replay one Gazebo RGB frame through production HTTP detection and an isolated hot-unload."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import cv2
import numpy as np

from calibration.board_provider import encode_frame


def query(url, archive, target):
    body = json.dumps(dict(archive=archive, target=target)).encode()
    with urlopen(Request(url+"/detect",data=body,headers={"Content-Type":"application/json"}), timeout=8) as r:
        return json.load(r)


def check(frame, profile, url, out):
    cfg = json.loads(profile.read_bytes())
    with np.load(frame, allow_pickle=False) as f:
        rgb = f["rgb"]
        ns = int(f["stamp_ns"])
        # This replay needs only RGB, its K and its acquisition stamp.
        archive = encode_frame(dict(rgb=rgb,k=f["k"],clouds={},stamp_ns=ns,stamps_ns={"camera":ns},
                                    frames={"camera":cfg["camera"]["frame"]}))
    results = {t:query(url,archive,t) for t in ("checkerboard", "campus_sign")}
    board, sign = results.values()
    assert all(r["stamp_ns"]==ns and r["condition"]=="target_visible" and len(r["detections"])==1
               for r in results.values())
    assert board["archive_sha256"] == sign["archive_sha256"] and board["frame"] == sign["frame"]
    assert board["detections"][0]["features"]["checkerboard_plane"]["corner_count"] == 24
    assert "features" not in sign["detections"][0] and board["detector"] != sign["detector"]
    with tempfile.TemporaryDirectory() as temp:
        registry = Path(temp)/"recognizers"
        shutil.copytree(profile.parent/"recognizers",registry)
        endpoint = "http://127.0.0.1:8142"
        env = dict(os.environ,EDGE_TARGET_DETECTORS=str(registry),EDGE_CALIBRATION_PROFILE=str(profile))
        proc = subprocess.Popen([sys.executable,"-m","calibration.board_provider","compute"],env=env)
        try:
            until = time.monotonic()+5.
            while True:
                try:
                    before = query(endpoint,archive,"checkerboard")
                    break
                except OSError:
                    if time.monotonic() >= until or proc.poll() is not None:
                        raise
                    time.sleep(.10)
            assert before == board
            (registry/"checkerboard.json").unlink()
            try:
                query(endpoint,archive,"checkerboard")
            except HTTPError as err:
                code, detail = err.code, json.load(err)
            else:
                raise AssertionError("withdrawn detector still returned a result")
            assert code==422 and "unavailable_detector" in detail["error"]
            assert query(endpoint,archive,"campus_sign") == sign and proc.poll() is None
            shutil.copy2(profile.parent/"recognizers/checkerboard.json", registry/"checkerboard.json")
            assert query(endpoint,archive,"checkerboard") == before
        finally:
            proc.terminate()
            proc.wait(timeout=5)
    out.mkdir(exist_ok=True,parents=True)
    preview = cv2.cvtColor(rgb,cv2.COLOR_RGB2BGR)
    for result, color in zip(results.values(),[(65,210,105),(40,190,250)]):
        box=result["detections"][0]["bbox_xyxy"]
        cv2.rectangle(preview,tuple(box[:2]),tuple(box[2:]),color,2)
        cv2.putText(preview,result["target"],(box[0],max(15,box[1]-5)),cv2.FONT_HERSHEY_SIMPLEX,.48,color,1,cv2.LINE_AA)
    assert cv2.imwrite(str(out/"target-detection.png"),preview)
    import hashlib
    report = dict(schema_version=1,source="Gazebo rendered RGB replay",raw_npz_sha256=hashlib.sha256(frame.read_bytes()).hexdigest(),
                  runtime_http_endpoint=url+"/detect",results=results,
                  isolated_hot_unload=dict(restarted=False,withdrawn="checkerboard",http_status=code,
                                           response=detail,unaffected="campus_sign",restored=True))
    (out/"target-detection.json").write_text(json.dumps(report,indent=2,allow_nan=False)+"\n",encoding="utf-8")


if __name__ == "__main__":
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--url",required=True)
    cli.add_argument("--frame",type=Path,required=True)
    cli.add_argument("--profile",type=Path,required=True)
    cli.add_argument("--out",type=Path,required=True)
    args=cli.parse_args()
    check(args.frame,args.profile,args.url,args.out)
