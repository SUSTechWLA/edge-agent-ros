"""SDD-045 AC5/8. Reject mismatched image/entrypoint, devices, additional mounts and network domain joins."""
import copy
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"scripts"))
import check_board_plugins as checker


class BoardGuard(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.site=Path(tmp.name)
        self.entry=["python3","-B","/opt/edge/agent/soft_host.py"]
        self.wanted={"soft-host":(self.entry,"soft-data")}
        self.spec={"image":"sha256:test","entrypoint":self.entry,"read_only":True,"cap_drop":["ALL"],
                   "networks":{"soft-data":None},"volumes":[
                       {"target":k,"type":"bind" if k=="/site" else "volume","source":str(self.site) if k=="/site" else "example",
                        "read_only":not allow} for k,allow in checker.MOUNTS["soft-host"].items()]}

    def check(self, spec, internal=True):
        checker.check_compose({"services":{"soft-host":spec},"networks":{"soft-data":{"internal":internal}}},
                              self.wanted,"sha256:test",self.site)

    def test_controls_permissions_and_route_violations_fail(self):
        self.check(self.spec)
        for key,bad in {"image":"latest","entrypoint":["sh"],"privileged":True,"devices":["/dev/camera"],
                        "network_mode":"host","networks":{"physical-sensors":None},"read_only":False,
                        "ports":[8080],"cap_add":["SYS_ADMIN"],"cap_drop":[],
                        "volumes":[*self.spec["volumes"],{"target":"/opt/edge","type":"volume","source":"code"}]}.items():
            with self.subTest(field=key),self.assertRaises(ValueError):
                self.check({**self.spec,key:bad})
        with self.assertRaisesRegex(ValueError,"network isolation"):
            self.check(self.spec,internal=False)
        spec=copy.deepcopy(self.spec)
        spec["volumes"][0]["source"]=str(self.site/"substitute")
        with self.assertRaisesRegex(ValueError,"approved directory"):
            self.check(spec)

    def test_actual_platform_digest_and_source_must_match(self):
        info='[{"Id":"sha256:fixed","Os":"linux","Architecture":"amd64","Config":{"Labels":{"org.opencontainers.image.revision":"aaa"}}}]'
        manifest='[{"Descriptor":{"mediaType":"application/vnd.oci.image.manifest.v1+json","digest":"sha256:platform"}}]'
        with patch.object(checker,"docker",side_effect=[info,manifest]) as run:
            self.assertEqual(checker.board_identity("board-tag","aaa"),("sha256:fixed","sha256:platform"))
            self.assertEqual(run.call_args.args[-1],"sha256:fixed")
        with patch.object(checker,"docker",return_value=info),self.assertRaisesRegex(ValueError,"source mismatch"):
            checker.board_identity("board-tag","bbb")

    def test_camera_and_witness_require_same_approved_source_topic_and_domain(self):
        approved={"sensor":{"source_id":"camera.front","topic":"/approved/image","ros_domain_id":161}}
        env={"EDGE_CAMERA_SOURCE":"camera.front","EDGE_CAMERA_TOPIC":"/approved/image","ROS_DOMAIN_ID":"161"}
        config={"services":{name:{"environment":dict(env)} for name in ("physical-camera","physical-verifier")}}
        checker.camera_scope(config,approved)
        for service in config["services"]:
            for field in env:
                altered=copy.deepcopy(config)
                altered["services"][service]["environment"][field]+="changed"
                with self.subTest(service=service,field=field),self.assertRaisesRegex(ValueError,"differs"):
                    checker.camera_scope(altered,approved)

    def test_running_mounts_must_match_site_project_volume_and_subpath(self):
        config={"services":{"soft-host":{"volumes":[
            {"target":"/site","source":str(self.site),"type":"bind","read_only":True},
            {"target":"/journal","source":"module-sources","type":"volume","volume":{"subpath":"soft.runtime"}}]}},
            "volumes":{"module-sources":{"name":"field-a_module-sources"}}}
        live={"Mounts":[{"Destination":"/site","Source":str(self.site),"Type":"bind","RW":False},
                        {"Destination":"/journal","Name":"field-a_module-sources","Type":"volume","RW":True}],
              "HostConfig":{"Mounts":[{"Target":"/journal","VolumeOptions":{"Subpath":"soft.runtime"}}]}}
        checker.inspect_mounts(config,"soft-host",live)
        for variant in ("previous site","other project","other plugin journal","whole volume"):
            changed=copy.deepcopy(live)
            if variant=="previous site":
                changed["Mounts"][0]["Source"]=str(self.site/"previous")
            elif variant=="other project":
                changed["Mounts"][1]["Name"]="field-b_module-sources"
            elif variant=="whole volume":
                changed["HostConfig"].pop("Mounts")
            else:
                changed["HostConfig"]["Mounts"][0]["VolumeOptions"]["Subpath"]="other.plugin"
            with self.subTest(variant=variant),self.assertRaisesRegex(ValueError,"live (bind source|volume or subpath) differs"):
                checker.inspect_mounts(config,"soft-host",changed)


if __name__=="__main__":
    unittest.main()
