"""Exercise the Core path/inspection admission boundary without a motion provider."""
import subprocess
import tempfile
from pathlib import Path

import rclpy
from ament_index_python.packages import get_package_prefix
from edge_interfaces.srv import CancelMission, SubmitMission
from geometry_msgs.msg import Point


def exercise():
    rclpy.init()
    node = rclpy.create_node("path_admission_probe")
    services = {
        name: node.create_client(spec, "/edge/" + name)
        for name, spec in [
            ("submit_path", SubmitMission), ("submit_mission", SubmitMission),
            ("cancel_mission", CancelMission)
        ]
    }

    def call(name, request):
        client = services[name]
        assert client.wait_for_service(timeout_sec=6), name
        future = client.call_async(request)
        rclpy.spin_until_future_complete(node, future, timeout_sec=4)
        assert future.done(), name
        return future.result()

    def request(points):
        return SubmitMission.Request(
            mission_id="path-test", waypoints=[Point(x=x, y=y) for x, y in points]
        )

    base = [(0., 0.), (.6, .1), (1., .2)]
    executable = Path(get_package_prefix("edge_core"), "lib/edge_core/edge_core_node")
    try:
        with tempfile.TemporaryDirectory() as folder:
            args = [str(executable), "--ros-args", "-p", f"db_path:={folder}/ledger.db"]
            for run in range(2):
                core = subprocess.Popen(args)
                try:
                    if not run:
                        assert not call("submit_path", request(base[:1])).accepted
                        for xy in ((4.001, 0.), (0., -4.001), (float("nan"), 0.)):
                            refused = call("submit_path", request([base[0], xy]))
                            assert not refused.accepted and "core.waypoint_invalid" in refused.detail
                        assert call("submit_path", request(base)).state == "RUNNING"
                    replay = call("submit_path", request(base))
                    assert replay.accepted and replay.state == ("CANCELED" if run else "RUNNING")
                    changed = call("submit_path", request([base[0], (.6, .3), base[-1]]))
                    assert not changed.accepted and "core.id_conflict" in changed.detail
                    # The terminal evidence target is identical. Execution modes still conflict.
                    legacy = call("submit_mission", request([base[-1]]))
                    assert not legacy.accepted and "core.id_conflict" in legacy.detail
                    if not run:
                        canceled = call("cancel_mission", CancelMission.Request(mission_id="path-test"))
                        assert canceled.accepted and canceled.state == "CANCELED"
                finally:
                    core.terminate()
                    core.wait(timeout=8)
            campus = subprocess.Popen([*args, "-p", "site_bounds_xyxy_m:=[-4.0,-16.0,15.0,4.0]"])
            try:
                for xy in ((-4.001, 0.), (0., -16.001), (15.001, 0.), (0., 4.001)):
                    refused = call("submit_path", request([base[0], xy]))
                    assert not refused.accepted and "core.waypoint_invalid" in refused.detail
                inside = request([base[0], (12., -13.)])
                inside.mission_id = "bounded-campus"
                assert call("submit_path", inside).accepted
                assert call("cancel_mission", CancelMission.Request(mission_id=inside.mission_id)).accepted
            finally:
                campus.terminate()
                campus.wait(timeout=8)
        print("PASS immutable path, conflict, restart replay, default and site-specific bounds")
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    exercise()
