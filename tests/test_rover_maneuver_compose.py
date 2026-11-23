"""Render the production rover overlays offline. No images, containers or hardware are used."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
ROVER = "compose.rover-maneuver.yaml"
BENCH = "compose.rover-maneuver-bench.yaml"


@unittest.skipUnless(shutil.which("docker"), "Docker Compose CLI is needed to render the overlays")
class RoverComposeTests(unittest.TestCase):
    def setUp(self):
        self.env = {k:v for k,v in os.environ.items() if not k.startswith(("EDGE_", "COMPOSE_"))}
        self.env.update(COMPOSE_DISABLE_ENV_FILE="1", EDGE_ROVER_SITE=str(ROOT/"capabilities/rover-maneuver"),
                        EDGE_ROVER_IMAGE_REPOSITORY="fixture/rover", EDGE_ROVER_IMAGE_DIGEST="sha256:"+"1"*64,
                        EDGE_PHYSICAL_PLATFORM_DIGEST="sha256:"+"2"*64, EDGE_ROVER_TOKEN_VOLUME="fixture-token",
                        EDGE_ROVER_PLATFORM="linux/amd64", EDGE_ROS_DOMAIN_ID="141")

    def render(self, *files):
        cmd = ["docker", "compose", "-p", "offline-rover-render"]
        for path in files:
            cmd.extend(("-f",path))
        result = subprocess.run([*cmd,"config","--format","json"], cwd=ROOT, env=self.env, capture_output=True)
        if result.returncode:
            raise ValueError(result.stderr.decode(errors="replace"))
        return json.loads(result.stdout)

    def test_simulation(self):
        cfg=self.render("compose.yaml",ROVER)
        services=cfg["services"]
        self.assertEqual(cfg["networks"]["default"], self.render("compose.yaml")["networks"]["default"])
        self.assertTrue(cfg["volumes"]["operator-token"]["external"])
        self.assertNotIn("operator-init",services)
        token=services["agent"]["environment"]["EDGE_MOTION_TOKEN_FILE"]
        self.assertEqual(token,"/run/operator/motion")
        self.assertEqual(services["maneuver-controller"]["network_mode"],"none")
        expected=services["agent"]["image"]
        for service, command in (("rover-execute","execute"),("rover-verify","verify")):
            role=services[service]
            self.assertEqual(role["entrypoint"]+role.get("command",[]),[
                "/usr/local/bin/edge-entrypoint","python3","-B","/opt/edge/agent/rover_maneuver_ros.py",
                command,"--site","/site","--socket","/role/physical.sock"])
            self.assertEqual(role["image"],expected)
            self.assertTrue(role["read_only"])
            self.assertEqual(role["cap_drop"],["ALL"])
            self.assertEqual(set(role["networks"]),{"default"})
            self.assertEqual({mount["target"] for mount in role["volumes"]},{"/site","/role"})
            self.assertTrue(all(m["read_only"] for m in role["volumes"]))
            self.assertEqual(role["environment"]["ROS_DOMAIN_ID"],"141")
            self.assertEqual(int(role["mem_limit"]),256*1024**2)
        self.assertNotEqual(services["rover-execute"]["volumes"],services["rover-verify"]["volumes"])

    def test_bench_requires_separate_commissioning(self):
        observe=self.render("compose.hardware.yaml","compose.hardware-observe.yaml")
        self.assertNotIn("maneuver-controller",observe["services"])
        self.assertNotIn("EDGE_MOTION_TOKEN_FILE",observe["services"]["agent"]["environment"])
        with self.assertRaisesRegex(ValueError,"EDGE_ROVER_"):
            self.render("compose.hardware.yaml",ROVER,BENCH)
        self.env.update(EDGE_ROVER_COMMISSIONING_DIR=str(ROOT/"tests"),
                        EDGE_ROVER_BENCH_APPROVAL_SHA256="sha256:"+"3"*64, EDGE_ROVER_PLATFORM="linux/arm64")
        services=self.render("compose.hardware.yaml",ROVER,BENCH)["services"]
        self.assertEqual(services["agent"]["environment"]["EDGE_ROVER_REQUIRE_BENCH"],"1")
        for name in ("maneuver-controller","rover-execute","rover-verify"):
            service=services[name]
            self.assertEqual(service["platform"],"linux/arm64")
            self.assertEqual(service["command"],["--profile","bench.json"])
            self.assertEqual(service["environment"]["EDGE_ROVER_REQUIRE_BENCH"],"1")
            self.assertEqual(service["network_mode"],"none" if name=="maneuver-controller" else "host")
            mounts={m["target"]:m for m in service["volumes"]}
            self.assertTrue(mounts["/commissioning"]["read_only"] and mounts["/site"]["read_only"])
            self.assertNotEqual(mounts["/commissioning"]["source"],mounts["/site"]["source"])
            self.assertNotIn("/run/operator",mounts)


if __name__=="__main__":
    unittest.main()
