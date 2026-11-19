"""SDD-041. Exercise the release procedure on an isolated Git repository."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import release_robot


class Release(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = Path(tmp.name)
        mocked = patch.object(release_robot, "ROOT", self.repo)
        mocked.start()
        self.addCleanup(mocked.stop)
        self.git("init", "-q", "-b", "source")
        self.git("config","user.email","test@localhost")
        self.git("config","user.name","release-test")
        for name in ("deploy/robot/README.md","compose.hardware.yaml","scripts/robot.sh","calibration/factory.py",
                     "exploration/frontier.py", "docs/research.md", "agent/test_old.py", "exploration/ros_sim.py"):
            self.write(name,"test")
        self.commit()

    def git(self,*args):
        return subprocess.check_output(["git",*args],cwd=self.repo).decode().strip()

    def write(self,name,data):
        path = self.repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(data, encoding="utf8")

    def commit(self):
        self.git("add",".")
        self.git("commit","-qm","fixture")

    def test_runtime_changes_only_and_lineage(self):
        source = self.git("rev-parse","HEAD")
        self.write("agent/local-token", "untracked")
        a = release_robot.publish(source)
        (self.repo/"agent/local-token").unlink()
        self.assertEqual(self.git("show",a+":ROBOT_SOURCE"),source)
        self.assertEqual(self.git("rev-list","--count",a),"1")
        self.assertEqual(set(release_robot.entries(a)),{"README.md","ROBOT_SOURCE","compose.hardware.yaml",
                         "scripts/robot.sh","calibration/factory.py","exploration/frontier.py"})
        self.write("docs/research.md","new experiments")
        self.commit()
        self.assertEqual(release_robot.publish("HEAD"),a)
        self.write("exploration/frontier.py","new implementation")
        self.commit()
        b = release_robot.publish("HEAD")
        self.assertEqual(self.git("rev-parse",b+"^"),a)
        with self.assertRaisesRegex(ValueError,"diverged"):
            release_robot.publish(source)
        self.assertEqual(self.git("branch","--show-current"),"source")

    def test_modified_branch_and_secrets_rejected(self):
        a = release_robot.publish("HEAD")
        self.git("checkout","-q","deploy/robot")
        self.write("README.md","local edit")
        self.commit()
        self.git("checkout","-q","source")
        with self.assertRaisesRegex(ValueError,"independent"):
            release_robot.publish("HEAD")
        self.write("agent/.env.local","fake")
        self.commit()
        with self.assertRaisesRegex(ValueError,"unexpected runtime"):
            release_robot.projection("HEAD")

    def test_site_profiles_and_physical_bench_are_projected_without_station_fixtures(self):
        shipped={"edge_contracts/__init__.py","edge_contracts/descriptor.py","compose.soft-runtime.yaml","compose.soft-runtime-hardware.yaml","compose.physical-camera.yaml",
                 "scripts/prepare_physical_camera.py","scripts/prepare_soft_pilots.py","scripts/robot-plugins.sh","scripts/check_board_plugins.py",
                 "capabilities/soft-pilots/pack.json","capabilities/camera/pack.json","profiles/bench.json",
                 "profiles/bench-camera.json","profiles/observe.json","agent/site_pack.py"}
        local={"compose.physical-camera-test.yaml","tests/system/physical_camera_source.py","profiles/.env",
               "profiles/site-local.json","capabilities/soft-pilots/keys.json"}
        for name in shipped|local:
            self.write(name,"fixture")
        self.commit()
        actual=set(release_robot.projection("HEAD"))
        self.assertTrue(shipped<=actual)
        self.assertFalse(local&actual)


if __name__=="__main__":
    unittest.main()
