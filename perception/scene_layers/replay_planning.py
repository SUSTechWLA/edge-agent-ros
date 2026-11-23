"""Run the existing static rover HTTP provider on immutable observations and archive clocks."""
from contextlib import ExitStack
from hashlib import sha256
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from edge_contracts.service_directory import ServiceSnapshot
from perception.rover_preview.provider import Handler, Provider
from scripts.export_field_scene_example import ARCHIVE, PROFILE, ROOT, read_recorded_scan
from .runtime import managed_change


class ReplayHandler(Handler):
    def do_POST(self):
        if self.path != "/evaluate":
            return self.send_json(404, {})
        super().do_POST()

    def end_headers(self):
        if self.path == "/evaluate" and self.command == "POST":
            with self.server.seq_lock:
                self.server.sequence += 1
                self.send_header("X-Replay-Evaluation", str(self.server.sequence))
        super().end_headers()

    def log_message(self, *args):
        pass


def digest(data):
    return "sha256:" + sha256(data).hexdigest()


def stable_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


class PlanningFixture:
    """One provider per recorded site profile, version and source clock."""
    def __init__(self, resources, state, snapshot, profile, metadata):
        self.directory = state / "runtime"
        self.snapshot = snapshot
        self.metadata = metadata
        self.profile = profile
        self.state = state
        self.clock_ns = int(snapshot["scan_stamp_ns"])
        if metadata["source_clock"] == "simulation":
            # The public endpoint expects Unix time. Apply a declared origin translation,
            # preserving simulation timestamps in the receipt and in the immutable input.
            self.clock_ns += 1_704_067_200_000_000_000
        server = resources.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), ReplayHandler))
        server.seq_lock = threading.Lock()
        server.sequence = 0
        server.provider = Provider(state / "provider", profile, "http://127.0.0.1:1")
        server.provider.evaluator.wall = lambda: self.clock_ns
        resources.callback(server.provider.pool.shutdown)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .05}, daemon=True)
        thread.start()
        resources.callback(thread.join)
        resources.callback(server.shutdown)
        self.descriptor = json.loads((PROFILE.parent / "runtime/planning.rover_preview.json").read_bytes())
        self.descriptor["endpoint"] = f"http://127.0.0.1:{server.server_port}/evaluate"
        installed = managed_change(state, self.descriptor, "publish", "absent")
        resources.callback(managed_change, state, self.descriptor, "remove", installed["version"])

    def call(self):
        binding = ServiceSnapshot(self.directory).pin("planning.local_path")
        if binding["endpoint"] != self.descriptor["endpoint"]:
            raise ValueError("archive planner endpoint binding changed")
        snapshot = {**self.snapshot, "scan_stamp_ns": str(self.clock_ns)}
        request = {"schema_version": 1, "provider_version": binding["plugin_version"],
                   "clock_id": "unix", "objective": {"kind": "static_local_path", "frame_id": snapshot["base_frame"]},
                   "profile_id": self.profile["profile_id"], "snapshot": snapshot}
        url = urlsplit(binding["endpoint"])
        conn = HTTPConnection(url.hostname, url.port, timeout=4)
        try:
            conn.request("POST", url.path, stable_bytes(request), {"Content-Type": "application/json"})
            response = conn.getresponse()
            result = json.loads(response.read())
            sequence = int(response.getheader("X-Replay-Evaluation"))
        finally:
            conn.close()
        if response.status != 200 or result.get("motion_authorized") is not False:
            raise ValueError("planning replay refused: " + str(result.get("error", response.status)))
        if result["scan_stamp_ns"] != str(self.clock_ns) or result["base_frame"] != snapshot["base_frame"]:
            raise ValueError("planner response did not bind the submitted observation")
        return {**self.metadata, "plan": result, "request_id": uuid4().hex,
                "service": binding["service"], "provider_version": binding["plugin_version"],
                "binding": binding["binding"], "evaluation_seq": sequence, "profile_id": self.profile["profile_id"],
                "source_stamp_ns": self.snapshot["scan_stamp_ns"], "admission_stamp_ns": str(self.clock_ns),
                "clock_mode": "frozen_archive_clock", "geometry_ref": digest(stable_bytes(result["path_body_m"]))}


class ReplayPlanning:
    def __init__(self, resources: ExitStack, archive: Path, site_state: Path):
        self.fixtures = {}
        manifest = json.loads((archive / "manifest.json").read_bytes())
        raw = (archive / "lidar-snapshot.json").read_bytes()
        snapshot = json.loads(raw)
        if (digest(raw) != manifest["lidar_snapshot_ref"] or digest(raw) != manifest["files"]["lidar-snapshot.json"]
                or snapshot["scan_stamp_ns"] != manifest["lidar_stamp_ns"]):
            raise ValueError("archived Jetson observation identity mismatch")
        profile = json.loads(PROFILE.read_bytes())
        profile_bytes = (archive / "chassis-profile.json").read_bytes()
        archived_profile = json.loads(profile_bytes)
        if (digest(profile_bytes) != manifest["files"]["chassis-profile.json"]
                or any(profile.get(k) != v for k, v in archived_profile.items())):
            raise ValueError("planner configuration does not match the recorded rover footprint")
        self.fixtures["field_hold"] = PlanningFixture(resources, site_state, snapshot, profile,
            {"source": "Jetson 双 MID-360 归档", "source_clock": "unix", "snapshot_ref": digest(raw)})
        candidates = {"centered": ARCHIVE, "clear": ROOT / "artifacts/verification/lingao-footprint-20261123/local/footprint-clear.tar.gz"}
        self.simulation_scene = None
        for case, source in candidates.items():
            if not source.is_file():
                continue
            window = read_recorded_scan(source, PROFILE)
            meta = {"source": "Gazebo 归档", "source_clock": "simulation", "archive_ref": digest(source.read_bytes()),
                    "snapshot_ref": digest(stable_bytes(window.snapshot))}
            self.fixtures[case] = PlanningFixture(resources, site_state / "gazebo" / case, window.snapshot, window.profile, meta)
            if case == "centered":
                # Only the display backdrop is taken from the archived visualization.
                # The path comes from a new HTTP response on every button press.
                example = json.loads((ROOT / "console/web/plan-example.json").read_bytes())
                if ("sha256:" + example["provenance"]["archive_sha256"] != meta["archive_ref"]
                        or example["scene"]["stamp_ns"] != window.snapshot["scan_stamp_ns"]
                        or example["scene"]["frame_id"] != window.snapshot["base_frame"]):
                    raise ValueError("comparison scene does not match the recorded sensor observation")
                self.simulation_scene = example["scene"]

    def run(self, case):
        if case not in self.fixtures:
            raise ValueError("unavailable archive planning case")
        result = self.fixtures[case].call()
        if case == "centered":
            result["scene"] = self.simulation_scene
        return {"case": case, **result}
