"""SDD-045 AC8. Reproducible locks, narrowing overlays, atomic board admission and rollback."""
from argparse import Namespace
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"agent"))
sys.path.insert(0,str(ROOT/"scripts"))
from prepare_soft_pilots import DEFAULT_SERIES, prepare_site
from prepare_physical_camera import prepare_site as prepare_camera
from site_pack import build_lock, verify
from plugin_manager import change
from v2_registry import V2Registry, RegistryError


class PackContract(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp=Path(temp.name)
        self.base=self.tmp/"input"
        manifest=json.dumps({"schemaVersion":2,"config":{"digest":"sha256:"+("0"*64)},"layers":[]}).encode()
        self.manifest=manifest
        prepare_site(self.base,manifest,"a"*40,DEFAULT_SERIES)
        self.profile=ROOT/"profiles/bench.json"

    def lock(self,name="ready",overlay=None):
        path=None
        if overlay is not None:
            path=self.tmp/(name+"-override.json")
            path.write_text(json.dumps(overlay),encoding="utf8")
        site=self.tmp/name
        digest=build_lock(ROOT/"capabilities",self.profile,self.base,path,site)
        return site,digest

    def test_lock_is_deterministic_verified_and_tamper_blocks_use(self):
        site,digest=self.lock()
        self.assertEqual(self.lock("copy")[1],digest)
        lock,items=verify(site,digest)
        self.assertEqual(set(items),{"session.default","evidence.read_session","evidence.evidence_schema",
                                      "evidence.query_evidence","derived.series_mean"})
        self.assertEqual(lock["policy"]["classes"],["soft"])
        (site/"config/derived.series_mean.json").write_text('{"trace":true}',encoding="utf8")
        with self.assertRaisesRegex(ValueError,"settings changed"):
            verify(site,digest)
        with self.assertRaisesRegex(ValueError,"fingerprint"):
            verify(site,"0"*64)

    def test_bad_overlay_rejected_without_partial_site_and_observe_is_empty(self):
        for index,overlay in enumerate([{"disable":["unknown"]},{"enable":["camera.site"]},{"policy":{
                "classes":["soft","physical"],"effects":["read"],"site_approval":False}},{"settings":{"no.such":{}}}]):
            name="reject"+str(index)
            with self.subTest(overlay=overlay),self.assertRaises(ValueError):
                self.lock(name,overlay)
            self.assertFalse((self.tmp/name).exists())
        site,digest=self.lock("smaller",{"disable":["derived.series_mean"]})
        self.assertNotIn("derived.series_mean",verify(site,digest)[1])
        self.profile=ROOT/"profiles/observe.json"
        site,digest=self.lock("observe")
        self.assertEqual(verify(site,digest)[1],{})

    def test_activate_atomic_immutable_policy_and_no_silent_remove(self):
        site,digest=self.lock()
        _,items=verify(site,digest)
        path=self.tmp/"registry"
        with V2Registry(path) as registry:
            self.assertEqual(len(registry.install_pack(list(items.values()),digest)["admitted"]),5)
            self.assertEqual(registry.install_pack(list(items.values()),digest)["admitted"],[])
            before=registry.entries()
            with self.assertRaisesRegex(RegistryError,"handoff"):
                registry.install_pack([],"0"*64)
            self.assertEqual(registry.entries(),before)
        smaller,digest2=self.lock("small",{"disable":["derived.series_mean"]})
        _,small=verify(smaller,digest2)
        isolated=self.tmp/"small-registry"
        with V2Registry(isolated) as registry:
            registry.install_pack(list(small.values()),digest2)
        req=Namespace(kind="v2",root=isolated,trust_root=site/"trust",action="publish",request_id="unlocked",
                      definition=site/"releases/derived.series_mean.plugin.json",expect="absent")
        with self.assertRaisesRegex(RegistryError,"outside the installed board profile"):
            change(req)
        bad={**items["session.default"],"policy":{**items["session.default"]["policy"],"site_approval":True}}
        with V2Registry(self.tmp/"empty") as registry:
            with self.assertRaisesRegex(RegistryError,"approval"):
                registry.install_pack([items["derived.series_mean"],bad],digest)
            self.assertEqual(registry.entries(),[])

    def test_physical_approval_is_bound_to_pack_and_never_published_as_soft(self):
        camera=self.tmp/"approved-camera"
        prepare_camera(camera,self.manifest,"a"*40)
        profile=ROOT/"profiles/bench-camera.json"
        target=self.tmp/"mixed"
        digest=build_lock(ROOT/"capabilities",profile,self.base,None,target,camera)
        lock,items=verify(target,digest)
        self.assertTrue(lock["plugins"]["camera.capture"]["site_approval"])
        self.assertEqual(items["camera.capture"]["class"],"physical")
        with V2Registry(self.tmp/"mixed-registry") as registry:
            soft=[item for item in items.values() if item["class"]=="soft"]
            self.assertNotIn("camera.capture",registry.install_pack(soft,digest)["admitted"])
            self.assertNotIn("capture_camera_frame",registry.claimed_exports())
        deploy=target/"physical/deployment.json"
        body=json.loads(deploy.read_text(encoding="utf8"))
        body["lease_ms"]=body["lease_ms"]+1
        deploy.write_text(json.dumps(body),encoding="utf8")
        with self.assertRaises(ValueError):
            verify(target,digest)


if __name__=="__main__":
    unittest.main()
