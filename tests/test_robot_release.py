"""SDD-041. Exercise the release procedure on an isolated Git repository."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import package_robot, release_robot


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

    def test_exact_source_and_lineage(self):
        source = self.git("rev-parse","HEAD")
        self.write("agent/local-token", "untracked")
        a = release_robot.publish(source)
        (self.repo/"agent/local-token").unlink()
        self.assertEqual(self.git("show",a+":ROBOT_SOURCE"),source)
        self.assertEqual(self.git("rev-list","--count",a),"1")
        self.assertEqual(set(release_robot.entries(a)),{"ROBOT_SOURCE","compose.hardware.yaml",
                         "scripts/robot.sh","calibration/factory.py","exploration/frontier.py"})
        self.assertEqual(release_robot.publish(source),a)
        self.write("docs/research.md","new experiments")
        self.commit()
        doc_release = release_robot.publish("HEAD")
        self.assertEqual(self.git("rev-parse", doc_release+"^"),a)
        self.assertEqual(self.git("show",doc_release+":ROBOT_SOURCE"),self.git("rev-parse","HEAD"))
        self.assertEqual(self.git("diff","--name-only",a,doc_release),"ROBOT_SOURCE")
        self.assertEqual(release_robot.publish("HEAD"),doc_release)
        self.write("exploration/frontier.py","new implementation")
        self.commit()
        b = release_robot.publish("HEAD")
        self.assertEqual(self.git("rev-parse",b+"^"),doc_release)
        with self.assertRaisesRegex(ValueError,"diverged"):
            release_robot.publish(source)
        self.assertEqual(self.git("branch","--show-current"),"source")

    def test_legacy_migration_and_kit_guide(self):
        source = self.git("rev-parse","HEAD")
        legacy_entries = release_robot.projection(source, legacy=True)
        self.assertIn("README.md", legacy_entries)
        # Construct a v1 commit with its original message and the source's guide.
        self.git("switch", "--orphan", "deploy/robot")
        for name, (_, oid) in legacy_entries.items():
            self.write(name, self.git("cat-file", "blob", oid))
        self.write("ROBOT_SOURCE", source+"\n")
        self.git("add", ".")
        tree = self.git("write-tree")
        self.git("commit", "-qm", "build(robot): deploy runtime from "+source, "-m", "Robot-Tree: "+tree)
        old = self.git("rev-parse","HEAD")
        self.git("switch", "source")
        self.assertTrue(release_robot.legacy_format(old, source))
        new = release_robot.publish(source)
        self.assertEqual(self.git("rev-parse",new+"^"),old)
        self.assertFalse(release_robot.legacy_format(new, source))
        self.assertNotIn("README.md", release_robot.entries(new))
        self.assertEqual(self.git("show",old+":README.md"),"test")
        self.write("deploy/robot/README.md", "later guide")
        with tempfile.TemporaryDirectory() as folder, patch.object(package_robot,"ROOT",self.repo):
            extracted = Path(folder)
            self.assertEqual(package_robot.field_guide(extracted,source), b"test")
            (extracted/"README.md").write_bytes(b"archived guide")
            self.assertEqual(package_robot.field_guide(extracted,source), b"archived guide")

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
        shipped={"agent/kernel/ledger.py","edge_contracts/__init__.py","edge_contracts/descriptor.py","compose.soft-runtime.yaml","compose.soft-runtime-hardware.yaml","compose.physical-camera.yaml",
                 "scripts/prepare_physical_camera.py","scripts/prepare_soft_pilots.py","scripts/robot-plugins.sh","scripts/check_board_plugins.py",
                 "capabilities/soft-pilots/pack.json","capabilities/camera/pack.json","profiles/bench.json",
                 "profiles/bench-camera.json","profiles/observe.json","agent/site_pack.py"}
        shipped.update({"deploy/robot/map-inputs/.gitkeep","src/edge_core/CMakeLists.txt","console/web/index.html"})
        shipped.update({"agent/kernel/task_facts.py","agent/facts_server.py","edge_contracts/conformance.py"})
        local={"compose.physical-camera-test.yaml","tests/system/physical_camera_source.py","profiles/.env",
               "deploy/robot/manual.pdf","deploy/robot/docs/start.html","deploy/robot/TIPS.txt",
               "agent/docs/guide.html","console/readme.html",
               "profiles/site-local.json","capabilities/soft-pilots/keys.json"}
        local.update({"analysis/__main__.py","analysis/examples/expected.json","analysis/README.md"})
        for name in shipped|local:
            self.write(name,"fixture")
        self.commit()
        actual=set(release_robot.projection("HEAD"))
        self.assertTrue({name.removeprefix("deploy/robot/") for name in shipped}<=actual)
        self.assertFalse({name.removeprefix("deploy/robot/") for name in local}&actual)


if __name__=="__main__":
    unittest.main()
