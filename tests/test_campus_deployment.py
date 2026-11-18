"""SDD-025. Check the resolved optional stack against a synthetic workstation configuration."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scene.campus_large.run import SERVICES, ReloadBlocked, reload_campus


class CampusDeployment(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("EDGE_", "COMPOSE_", "DEEPSEEK_"))}
        env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_PLANNER_URL="http://private.invalid/planner",
                   EDGE_OLLAMA_URL="http://private.invalid/model", EDGE_CONTRACTS_DIR="./operator-contracts",
                   EDGE_PLUGINS_DIR="./operator-plugins", EDGE_OBSERVATIONS_DIR="./operator-observations",
                   EDGE_CONSOLE_VIEWS_DIR="./operator-views", EDGE_EXTRA_PLUGIN_DIR="/operator-author",
                   EDGE_CONSOLE_PORT="49999")
        base = ["docker","compose","-f","compose.yaml"]
        def compose(files):
            return json.loads(subprocess.check_output([*base,*files,"config","--format","json"],cwd=ROOT,env=env))
        cls.normal = compose([])
        cls.campus = compose(["-f","compose.campus.yaml"])
        cls.map_services = compose(["-f","compose.campus.yaml","--profile","map3d"])["services"]

    def test_existing_project_and_world_have_separate_identity(self):
        self.assertEqual((self.normal["name"],self.campus["name"]), ("edge-agent-ros","edge-agent-campus"))
        self.assertNotIn("campus-build", self.normal["services"])
        self.assertIn("/opt/edge/scene/start_world.py", self.normal["services"]["sim"]["command"])
        self.assertEqual(self.campus["services"]["campus-build"]["network_mode"], "none")
        self.assertTrue(set(self.normal["volumes"][k]["name"] for k in self.normal["volumes"]).isdisjoint(
                        v["name"] for v in self.campus["volumes"].values()))
        for key in ("sim","scene","campus-overview"):
            service = self.campus["services"][key]
            self.assertEqual(service["depends_on"]["campus-build"]["condition"], "service_completed_successfully")
            mounted = next(v for v in service["volumes"] if v["target"] == "/campus")
            self.assertTrue(mounted["read_only"])
            self.assertEqual(mounted["source"],"campus-world")

    def test_fake_developer_model_registries_and_ports_do_not_cross_projects(self):
        active = self.campus["services"]
        self.assertNotIn("plugin-author",active)
        self.assertNotIn("model",active)
        agent = active["agent"]
        self.assertEqual([agent["environment"][key] for key in ("EDGE_OLLAMA_URL","EDGE_PLANNER_URL")], ["",""])
        for key in ("agent","console"):
            self.assertNotIn("operator-",json.dumps(active[key]))
            self.assertTrue(all(v["type"]=="volume" for v in active[key]["volumes"]))
        for name, port in [("console","8766"), ("campus-overview","8780")]:
            self.assertEqual([(p["host_ip"],str(p["published"])) for p in active[name]["ports"]], [("127.0.0.1",port)])

    def test_map_installs_against_campus_without_controlling_world(self):
        viewer, bake = (self.map_services[name] for name in ("map3d","campus-texture"))
        self.assertEqual(set(viewer["depends_on"]), {"map3d-init","campus-texture"})
        self.assertNotIn("depends_on", bake)
        self.assertEqual(bake["network_mode"], "none")
        self.assertEqual(viewer["environment"]["EDGE_MAP_TEXTURE_DIR"], "/campus/texture")
        self.assertEqual(viewer["ports"][0]["host_ip"], "127.0.0.1")
        self.assertEqual(str(viewer["ports"][0]["published"]), "8773")
        volumes = {v["target"]:v for v in viewer["volumes"]}
        self.assertEqual(volumes["/campus"]["source"], "campus-world")
        self.assertTrue(volumes["/campus"]["read_only"])
        self.assertEqual(volumes["/etc/edge/map3d-inputs"]["source"], "campus-map-inputs")
        self.assertEqual(volumes["/journal"]["source"], "module-sources")

    def test_install_links_use_published_port_and_preserve_other_views(self):
        sys.path.insert(0,str(ROOT/"scene/campus_large"))
        self.addCleanup(sys.path.pop,0)
        from install_map import register
        with tempfile.TemporaryDirectory() as name:
            mount=Path(name)
            file=mount/"views.json"
            camera={"id":"camera","port":8888,"label":"校园相机","slot":"sensor"}
            file.write_text(json.dumps([{"id":"map3d","port":8770},camera]),encoding="utf-8")
            for port in (8851,8852):
                register(mount,file,port)
                views=json.loads(file.read_bytes())
                self.assertEqual(views,[camera,{"id":"map3d","port":port,"label":"校园纹理地图","slot":"map"}])
                self.assertEqual(json.loads((mount/"map-link.json").read_bytes()),{"port":port})


def state(stamp, *, speed=(0.,0.,0.), yaw=0., mission=None, operation=None, fresh=True):
    return {"mode":"live","environment":"simulation","ledger_id":"campus-kernel","active_operation":operation,
            "missions":[mission] if mission else [],"sources":{"semantics":{"data":{"id":"campus_large"}},
            "odom":{"stamp_s":stamp,"fresh":fresh,"data":{"linear_velocity":speed,"w":yaw}}}}


EMPTY = {"counts":{"active":0},"tasks":[]}
BUSY = {"counts":{"active":1},"tasks":[{"id":"lesson","state":"WAITING"}]}


class CampusReload(unittest.TestCase):
    def setUp(self):
        self.running = [s for s in SERVICES if s != "campus-capture"]
        self.extra = "a"*12
        def query(command,*params):
            if command == "port":
                return "127.0.0.1:42370"
            return "\n".join([f"{s} {i+1:012x}" for i,s in enumerate(self.running)]+[f"extra-ros {self.extra}"])
        self.output = query
        self.run, self.containers = Mock(), Mock()

    def check(self, *replies):
        responses = iter(replies)
        def fetch(port,path):
            self.assertEqual(port,42370)
            return next(responses)
        reload_campus(self.run,self.output,self.containers,fetch)

    def test_active_task_or_unsettled_motion_never_stops_the_clock(self):
        for snapshot, expected in ((state(1, speed=(0.,.11,0.)), "moving"),
                                   (state(1,yaw=float("nan")), "incomplete"),
                                   (state(1,fresh=False), "odometry"),
                                   (state(1,operation="nav.9"), "nav.9"),
                                   (state(1,mission={"mission_id":"mission.12","state":"RECOVERY_REQUIRED"}), "mission.12")):
            with self.subTest(reason=expected), self.assertRaisesRegex(ReloadBlocked,expected):
                self.check(EMPTY,snapshot)
            self.run.assert_not_called()
        with self.assertRaisesRegex(ReloadBlocked,"lesson"):
            self.check(BUSY)
        self.run.assert_not_called()
        self.containers.assert_not_called()

    def test_task_accepted_during_preflight_reopens_agent_with_original_world(self):
        with self.assertRaisesRegex(ReloadBlocked,"lesson"):
            self.check(EMPTY,state(5),BUSY)
        self.assertEqual([c.args for c in self.run.call_args_list],[("stop","agent"),("start","agent")])
        self.assertEqual([c.args for c in self.containers.call_args_list],
                         [("stop",self.extra),("start",self.extra)])

    def test_new_epoch_includes_all_clock_consumers_then_checks_readiness(self):
        completed = {"mission_id":"earlier","state":"SUCCEEDED"}
        self.check(EMPTY,state(4,mission=completed),EMPTY,state(4.4,mission=completed))
        cmds = [c.args for c in self.run.call_args_list]
        self.assertEqual(cmds[0], ("stop","agent"))
        self.assertEqual(set(cmds[1][1:]),set(self.running)-{"agent"})
        self.assertEqual(cmds[2],("up","-d","--no-build",*SERVICES))
        self.assertEqual(cmds[3],("exec","-T","core","/usr/local/bin/edge-entrypoint","python3","/opt/edge/client/ready.py"))
        self.assertEqual([c.args for c in self.containers.call_args_list],
                         [("stop",self.extra),("start",self.extra)])

    def test_unreadable_journal_after_closing_ingress_restores_agent(self):
        with self.assertRaisesRegex(ReloadBlocked,"incomplete"):
            self.check(EMPTY,state(3),{})
        self.assertEqual([c.args for c in self.run.call_args_list],[("stop","agent"),("start","agent")])

    def test_reload_of_intentionally_stopped_world_boots_then_verifies(self):
        reload_campus(self.run,lambda *args:"",self.containers,lambda *args:self.fail("unexpected read"))
        self.assertEqual([c.args[0] for c in self.run.call_args_list],["up","exec"])


if __name__ == "__main__":
    unittest.main()
