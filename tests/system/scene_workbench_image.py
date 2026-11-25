"""Run inside the packaged sim image with only the fusion fixture mounted read-only."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import signal
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen


def read(base, path):
    with urlopen(base.rstrip("/") + path, timeout=15) as response:
        return response.read(), response.headers.get_content_type()


def verify(evidence, scratch):
    for station in ("station-a", "station-b"):
        with tarfile.open(evidence / (station + ".tar.gz")) as archive:
            archive.extractall(scratch, filter="data")
    manifest = (evidence / "map.json").read_bytes()
    (scratch / "map.json").write_bytes(manifest)
    pin = "sha256:" + sha256(manifest).hexdigest()
    command = [sys.executable, "-B", "-m", "perception.scene_layers.workbench", "--port", "0",
               "--fusion-directory", str(scratch), "--fusion-manifest-ref", pin]
    with subprocess.Popen(command, stdout=subprocess.PIPE) as process:
        try:
            startup = process.stdout.readline()
            if not startup:
                raise RuntimeError("The packaged workbench exited during startup")
            base = json.loads(startup)["url"]
            html, kind = read(base, "/fusion")
            if kind != "text/html" or b"fusion-map.js" not in html:
                raise RuntimeError("The packaged workbench did not serve its browser entry point")
            payload = json.loads(read(base, "/api/fusion/map")[0])
            scene = payload["scene"]
            if (scene["source_refs"]["manifest"] != pin or scene["motion_authorized"] is not False
                    or len(scene["windows"]) != 2):
                raise RuntimeError("The fusion service did not bind the requested survey")
            url = f"/api/fusion/{payload['scene_ref'][7:]}/{payload['layer_ref'][7:]}.ply"
            ply, mime = read(base, url)
            if mime != "application/octet-stream" or ply != (evidence / "fusion-map.ply").read_bytes():
                raise RuntimeError("The packaged map export differs from the frozen survey")
            return {"status": "passed", "manifest_ref": pin, "points": len(scene["points"]),
                    "motion_authorized": scene["motion_authorized"], "ply_sha256": sha256(ply).hexdigest()}
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(15)
                except subprocess.TimeoutExpired:
                    process.kill()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixtures", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory() as folder:
        print(json.dumps(verify(args.fixtures, Path(folder))), flush=True)
