"""Keep each campus viewpoint tied to the Gazebo world that rendered it."""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
from queue import Empty, Full, Queue
import sys
import tempfile
import time

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[2]))

from edge_contracts.image_frames import chunk, encode_gazebo as encode


VIEWS = ("overview", "campus", "teaching", "track", "woods", "road", "lawn")
WORLD = Path("/campus")
RUNTIME = Path("/run/campus/world.json")


def file_digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def identity(root):
    return {"world_sha256": file_digest(root / "world.sdf"), "layout_sha256": file_digest(root / "layout.json")}


def read_record(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError, UnicodeError):
        return {}


def view_names(root):
    places = read_record(root/"overview/manifest.json").get("areas")
    if places is None:
        return VIEWS  # Historical worlds and fixtures without an area registry.
    names = ["overview"] + [place["id"] for place in places]
    if len(set(names)) != len(names) or not all(re.fullmatch(r"[a-z][a-z0-9_]{0,47}", n) for n in names):
        raise ValueError("Expected unique campus viewpoint IDs")
    return names


def atomic(path, data):
    output = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".pending-", delete=False)
    tmp = Path(output.name)
    try:
        with output:
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def record(path, data):
    atomic(path, (json.dumps(data, indent=2, sort_keys=True) + "\n").encode())


def prepare(root, current):
    directory = root / "overview"
    manifest = directory / "capture.json"
    before = read_record(manifest)
    views = before.get("views", {}) if before.get("world") == current else {}
    if not isinstance(views, dict):
        views = {}
    valid = {}
    names = view_names(root)
    for name in names:
        image = directory / (name + ".png")
        item = views.get(name)
        if isinstance(item, dict) and image.is_file() and file_digest(image) == item.get("sha256"):
            valid[name] = item
        else:
            image.unlink(missing_ok=True)
    receipt = {"schema_version": 1, "world": current, "views": valid, "complete": len(valid) == len(names)}
    record(manifest, receipt)
    return receipt


def capture(node, message_type, topic, deadline):
    frames = Queue(maxsize=1)
    def receive(message):
        try:
            frames.put_nowait(message)
        except Full:
            pass
    if not node.subscribe(message_type, topic, receive):
        raise RuntimeError(f"cannot subscribe to {topic}")
    try:
        problem = "no sample"
        while time.monotonic() < deadline:
            try:
                return encode(frames.get(timeout=min(1., max(.01, deadline - time.monotonic()))))
            except Empty:
                continue
            except ValueError as exc:
                problem = str(exc)
        raise TimeoutError(f"timed out waiting for {topic}: {problem}")
    finally:
        node.unsubscribe(topic)


def photos(root, runtime, timeout=240):
    from gz.msgs10.image_pb2 import Image
    from gz.transport13 import Node

    current = identity(root)
    receipt = prepare(root, current)
    if receipt["complete"]:
        print(f"Verified {len(receipt['views'])} cached campus images for this world.", flush=True)
        return
    deadline = time.monotonic() + timeout
    while read_record(runtime) != current:
        if time.monotonic() >= deadline:
            raise TimeoutError("Gazebo is not running this campus revision. Recreate the campus sim service.")
        time.sleep(.25)
    node = Node()
    names = view_names(root)
    for name in names:
        if name in receipt["views"]:
            continue
        topic = f"/campus/view/{name}/image"
        png, meta = capture(node, Image, topic, deadline)
        if read_record(runtime) != current or identity(root) != current:
            raise RuntimeError("campus world changed during capture")
        atomic(root / "overview" / (name + ".png"), png)
        receipt["views"][name] = {"topic": topic, **meta}
        receipt["complete"] = len(receipt["views"]) == len(names)
        record(root / "overview/capture.json", receipt)
        print(f"{name}: {meta['width']} x {meta['height']} from {topic} at {meta['stamp_ns']} ns", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "world", "photos"])
    args = parser.parse_args()
    if args.action == "world":
        record(RUNTIME, identity(WORLD))
        os.execlp("gz", "gz", "sim", "-s", "-r", "--headless-rendering", str(WORLD / "world.sdf"))
    else:
        # Both writers run in Linux containers, including on Docker Desktop. Repeated
        # compose up/run commands must not delete a photo between its PNG and receipt writes.
        import fcntl
        with (WORLD / ".capture.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if args.action == "prepare":
                prepare(WORLD, identity(WORLD))
            else:
                photos(WORLD, RUNTIME)
