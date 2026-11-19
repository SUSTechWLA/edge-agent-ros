"""Keep a single Gazebo station per world. A plugin owns only its ROS bridge processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
import xml.etree.ElementTree as ET


def ensure_station(model, sdf):
    # Removing active Ogre2 render sensors can abort the whole Harmonic world.
    # Retain a fixed model across bridge restarts, and re-create it after world replacement.
    try:
        models = subprocess.run(["gz", "model", "--list"], capture_output=True, text=True, timeout=5)
        if models.returncode or "Available models:" not in models.stdout:
            return False
        if any(line.strip() == "- " + model for line in models.stdout.splitlines()):
            return True
        request = f'sdf: {json.dumps(sdf)} name: "{model}" allow_renaming: false'
        result = subprocess.run(
            ["gz", "service", "-s", "/world/edge_lab/create", "--reqtype", "gz.msgs.EntityFactory",
             "--reptype", "gz.msgs.Boolean", "--timeout", "2000", "--req", request],
            capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and "data: true" in result.stdout
    except subprocess.TimeoutExpired:
        return False


def signal_child(child, sig):
    try:
        os.killpg(child.pid, sig)
    except ProcessLookupError:
        pass


def run_station(path: Path, commands):
    stopped = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopped.set())
    sdf = path.read_text(encoding="utf-8")
    model = ET.fromstring(sdf).find("model").attrib["name"]
    children = []
    next_check = 0
    try:
        while not stopped.is_set():
            if time.monotonic() >= next_check:
                present = ensure_station(model, sdf)
                if present and not children:
                    for command in commands:
                        children.append(subprocess.Popen(command, start_new_session=True))
                next_check = time.monotonic() + (15 if present else 1)
            if any(child.poll() is not None for child in children):
                raise RuntimeError(f"Station {model} lost a bridge or transform publisher")
            stopped.wait(0.3)
    finally:
        for child in children:
            signal_child(child, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=2)
            except subprocess.TimeoutExpired:
                signal_child(child, signal.SIGKILL)
                child.wait()
