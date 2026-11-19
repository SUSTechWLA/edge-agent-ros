"""Exercise the compiled actuator-side plugin with a controllable odometry source."""
import os
from contextlib import contextmanager
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from gz.msgs10.clock_pb2 import Clock
from gz.msgs10.odometry_pb2 import Odometry
from gz.msgs10.stringmsg_pb2 import StringMsg
from gz.msgs10.twist_pb2 import Twist
from gz.transport13 import Node


WORLD = """<sdf version="1.9"><world name="body_test">
  <physics name="rt" type="ignored"><max_step_size>0.001</max_step_size>
    <real_time_factor>1</real_time_factor></physics>
  <model name="test_base"><static>true</static><link name="base"/>
    <plugin filename="edge_velocity_watchdog" name="edge_sim::VelocityWatchdog">
      <body_runtime>true</body_runtime>
    </plugin>
  </model>
</world></sdf>"""


class Probe:
    def __init__(self):
        self.node = Node()
        self.state = ()
        self.clock_ns = 0
        self.outputs = []
        self.node.subscribe(StringMsg, "/edge/body/state", self.receive_state)
        self.node.subscribe(Clock, "/clock", self.receive_clock)
        self.node.subscribe(Twist, "/edge_rover/guarded_cmd_vel", self.receive_output)
        self.command = self.node.advertise("/edge/body/command", StringMsg)
        self.odom = self.node.advertise("/odom", Odometry)
        self.legacy = self.node.advertise("/cmd_vel", Twist)

    def receive_state(self, msg):
        self.state = tuple(msg.data.split())

    def receive_clock(self, msg):
        self.clock_ns = msg.sim.sec * 1_000_000_000 + msg.sim.nsec

    def receive_output(self, msg):
        self.outputs.append((msg.linear.x, msg.angular.z))

    def sample(self):
        sample = Odometry()
        sample.header.stamp.sec, sample.header.stamp.nsec = divmod(self.clock_ns, 1_000_000_000)
        sample.twist.linear.x = 0.0
        sample.twist.angular.z = 0.0
        self.odom.publish(sample)

    def send(self, grant, instruction):
        self.command.publish(StringMsg(data=f"v1 {grant[1]} {grant[2]} driver {instruction}"))

    def wait(self, condition, label, pump=None, timeout=3):
        deadline = time.monotonic() + timeout
        while not condition():
            assert time.monotonic() < deadline, f"{label}: state={self.state}, output={self.outputs[-1:]}"
            if pump:
                pump()
            time.sleep(0.015)

    def for_seconds(self, duration, callback):
        end = time.monotonic() + duration
        self.wait(lambda: time.monotonic() >= end, "timed probe", callback, duration + 1)

    def phase(self, name):
        return len(self.state) == 5 and self.state[3] == name

    def run(self):
        self.wait(lambda: self.clock_ns > 0 and self.phase("quiet") and self.outputs,
                  "discover running plugin and clock", timeout=15)
        self.wait(lambda: self.phase("ready"), "fresh continuous witness", self.sample)
        first = self.state
        assert len(first[1]) == 32 and first[4] == "-"
        # Removing the witness AFTER ready withdraws this advertised grant.
        self.wait(lambda: self.phase("quiet"), "ready withdrawn after silence")
        assert int(self.state[2]) > int(first[2])
        self.send(first, "open")
        # Two short bursts separated by a gap must not form a 300 ms window.
        self.for_seconds(0.15, self.sample)
        time.sleep(0.24)
        self.for_seconds(0.18, self.sample)
        assert self.phase("quiet")
        self.wait(lambda: self.phase("ready"), "second witness window", self.sample)
        grant = self.state
        self.send(grant, "open")
        self.wait(lambda: self.phase("held"), "grant accepted")
        assert self.state[4] == "driver"
        self.send(grant, "drive 2 0.10 0.2")
        self.wait(lambda: self.outputs[-1] == (0.10, 0.2), "first authorized command")
        # Later arrival does not outrank a prior sequence number. Invalid high
        # sequence packets must not consume sequence 3 or renew the deadline.
        for drive in ("drive 2 0.08 0", "drive 1 0.07 0", "drive 20 nan 0",
                      "drive 21 0.321 0", "drive 22 0 1.101"):
            self.send(grant, drive)
        self.send(grant, "drive 3 0.06 0")
        self.wait(lambda: self.outputs[-1] == (0.06, 0.0), "valid successor after malformed packets")
        start = len(self.outputs)
        def invalid_packets():
            self.send(grant, "drive 3 0.09 0")
            self.send(grant, "drive 2 0.08 0")
            self.send(grant, "drive 4 0.321 0")
            self.send(grant, "drive 5 0 -1.101")
            self.send(grant, "drive 6 0 NaN")
        self.wait(lambda: self.phase("quiet") and self.outputs[-1] == (0, 0),
                  "invalid packets cannot renew a grant", invalid_packets, timeout=1.5)
        assert all(v in ((0.06, 0.0), (0.0, 0.0)) for v in self.outputs[start:])
        assert int(self.state[2]) > int(grant[2])
        self.wait(lambda: self.phase("ready"), "new grant after watchdog stop", self.sample)
        current = self.state
        self.send(current, "open")
        self.wait(lambda: self.phase("held"), "new owner active")
        start = len(self.outputs)
        def delayed_packets():
            self.send(grant, "drive 30 0.09 0")
            self.send(grant, "stop")
            naked = Twist()
            naked.linear.x = 0.09
            self.legacy.publish(naked)
        self.for_seconds(0.10, delayed_packets)
        assert self.phase("held") and self.outputs[start:] and all(v == (0, 0) for v in self.outputs[start:])
        self.send(current, "drive 1 0.04 0")
        self.wait(lambda: self.outputs[-1] == (0.04, 0.0), "current grant works")
        self.send(current, "stop")
        self.wait(lambda: self.phase("quiet") and self.outputs[-1] == (0, 0), "explicit stop")
        assert int(self.state[2]) > int(current[2])
        return first

    def after_restart(self, old):
        self.wait(lambda: self.phase("ready") and self.state[1] != old[1],
                  "new boot witnesses its own quiet window", self.sample, timeout=15)
        current = self.state
        assert current[2] == old[2]  # Exercise epoch reuse across actual process boots.
        def old_open():
            self.sample()
            self.send(old, "open")
        self.for_seconds(0.10, old_open)
        assert self.state == current
        self.send(current, "open")
        self.wait(lambda: self.phase("held"), "current boot accepts its grant")
        start = len(self.outputs)
        self.for_seconds(0.10, lambda: self.send(old, "drive 1 0.05 0"))
        assert self.outputs[start:] and all(v == (0, 0) for v in self.outputs[start:])
        self.send(current, "stop")
        self.wait(lambda: self.phase("quiet"), "restarted endpoint can close")


@contextmanager
def simulator(path):
    server = subprocess.Popen(["gz", "sim", "-s", "-r", str(path)])
    try:
        yield
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()


def main():
    os.environ["GZ_PARTITION"] = "body-endpoint-" + uuid.uuid4().hex
    os.environ["GZ_SIM_SYSTEM_PLUGIN_PATH"] = os.pathsep.join(filter(None, (
        sys.argv[1], os.getenv("GZ_SIM_SYSTEM_PLUGIN_PATH"))))
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "probe.sdf"
        path.write_text(WORLD)
        probe = Probe()
        with simulator(path):
            old = probe.run()
        with simulator(path):
            probe.after_restart(old)


if __name__ == "__main__":
    main()
