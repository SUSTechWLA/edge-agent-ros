"""Check one-shot projection qualification, restricted mounts, and restart identity."""
from pathlib import Path
import json
import os
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts import evolve_read_projection as trigger


@unittest.skipUnless(os.name=="posix","site trigger runs on the Linux edge host")
class EvolutionTrigger(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name).resolve()
        self.src=self.root/"source"
        (self.src/"client").mkdir(parents=True)
        (self.src/"client/soft_projection_evolution.py").write_text("# trusted supervisor")
        (self.src/"qualification.py").write_text("# independent validator")
        profile=self.root/"profile.json"
        profile.write_text("{}")
        approved=self.root/"approved"
        (approved/"config").mkdir(parents=True)
        (self.root/"ready").mkdir()
        self.backend="sha256:"+"c"*64
        self.phash=trigger.hash_bytes(profile.read_bytes())
        (approved/"config/example.service.json").write_bytes(trigger.encoded({"backend_digest": self.backend, "profile_digest": self.phash}))
        cfg={"validator":"qualification.py","actor":"unit-1","facts_network":"example-facts","profile":"profile.json",
             "arguments":{"query":"target"},"mounts":{"/inputs/color":"sample-volume"}}
        self.env=self.root/"site.json"
        self.env.write_bytes(trigger.encoded(cfg))
        self.out=self.root/"out"
        self.args=SimpleNamespace(source_root=self.src,approved_site=approved,ready_site=self.root/"ready",out=self.out,
                                  task="original",port=17711,timeout=1,ready_lock="0"*64,approved_lock="1"*64,
                                  image="example:test",registry_volume="example-registry",site_env=self.env)
        self.original={"id":"original","text":"natural task","trace_id":"tr","state":"BLOCKED","steps":[]}
        self.query='a punctuation; $(touch BAD)\nsecond --extra'
        self.candidate={"candidate_digest":"sha256:"+"2"*64, "tool":"some_tool", "service":"example.service",
                        "arguments":{"target":self.query}}
        self.calls=[]
        self.refusal=False

    def docker(self,*args,timeout=100):
        self.calls.append(args)
        if args[:2]==("network","inspect"):
            return b"true"
        if args[:2]==("image","inspect"):
            return ("sha256:"+"3"*64).encode()
        if args[:2]==("volume","inspect"):
            return args[-1].encode()
        if "propose" in args:
            (self.out/"candidate.json").write_bytes(trigger.encoded(self.candidate))
            return trigger.encoded(self.candidate)
        if "/gate/verify.py" in args:
            if self.refusal:
                import subprocess
                raise subprocess.CalledProcessError(1,["python3"],output=b'{"passed":false}')
            receipt={"candidate_digest":self.candidate["candidate_digest"],"tool_id":"some_tool","actor_ref":"unit-1",
                     "schema_version":1,"passed":True,"checks":["example"],"backend_digest":self.backend,
                     "profile_digest":self.phash,"query":self.query}
            return trigger.encoded(receipt)
        if "apply" in args:
            return b'{"state":"SUCCEEDED"}'
        raise AssertionError(args)

    def run_trigger(self):
        with patch.object(trigger,"docker",self.docker), patch.object(trigger,"wait_task",return_value=self.original):
            trigger.run(self.args)

    def test_uses_separate_networks_mounts_literal_argv_and_reenters_with_same_sha(self):
        self.run_trigger()
        verify=next(a for a in self.calls if "/gate/verify.py" in a)
        self.assertEqual(verify[verify.index("--network")+1],"example-facts")
        self.assertIn("--query="+self.query,verify)
        self.assertEqual(verify[verify.index("--group-add")+1],str(os.getegid()))
        self.assertEqual(self.out.stat().st_mode & 0o777,0o770)
        self.assertEqual((self.out/"qualification.json").stat().st_mode & 0o777,0o640)
        self.assertNotIn("--privileged",verify)
        self.assertFalse(any("registry" in a or "target=/out" in a or "/dev/" in a for a in verify))
        for i,flag in enumerate(verify):
            if flag=="--mount":
                self.assertTrue(verify[i+1].endswith(",readonly"))
        apply=next(a for a in self.calls if "apply" in a)
        qualification=(self.out/"qualification.json").read_bytes()
        self.assertEqual(apply[-1],trigger.hash_bytes(qualification))
        self.calls.clear()
        (self.out/"qualification.json").unlink()
        self.run_trigger()
        self.assertFalse(any("/gate/verify.py" in a for a in self.calls))
        self.assertEqual((self.out/"qualification.json").read_bytes(),qualification)
        (self.out/"qualification.json").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError,"existing evidence differs"):
            self.run_trigger()

    def test_refusal_archived_without_admission(self):
        self.refusal=True
        with self.assertRaisesRegex(ValueError,"verification failed"):
            self.run_trigger()
        self.assertFalse(any("apply" in a for a in self.calls))
        self.assertEqual(len(list(self.out.glob("qualification-rejected-*.json"))),1)

    def test_service_can_be_owned_by_separately_named_plugin(self):
        self.candidate["provider"]="plugin.spatial-core"
        config=self.args.approved_site/"config"
        (config/"example.service.json").rename(config/"plugin.spatial-core.json")
        self.run_trigger()
        self.assertTrue(any("apply" in a for a in self.calls))

    def test_refuses_registry_input_and_uncovered_task_arguments(self):
        cfg=json.loads(self.env.read_bytes())
        cfg["mounts"]["/inputs/color"]=self.args.registry_volume
        self.env.write_bytes(trigger.encoded(cfg))
        with self.assertRaisesRegex(ValueError,"separate named"):
            self.run_trigger()
        cfg["mounts"]["/inputs/color"]="sample-volume"
        self.env.write_bytes(trigger.encoded(cfg))
        self.candidate["arguments"]["unexpected"]="X"
        with self.assertRaisesRegex(ValueError,"cannot cover"):
            self.run_trigger()
        self.assertFalse(any("apply" in a for a in self.calls))


if __name__=="__main__":
    unittest.main()
