import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"agent"), str(ROOT/"scripts")]
from kernel.ledger import trigger_input
from planning import plan_read_only
from prepare_navigation import bench_settings, prepare
from prepare_perception_site import prepare as extend
from site_pack import verify


class SiteTest(unittest.TestCase):
    def test_add_only_and_pin(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            settings = bench_settings()
            del settings["sensing.replay"]
            manifest = json.dumps({"schemaVersion": 2, "layers": [], "config": {"digest": "sha256:"+"a"*64}}).encode()
            old = prepare(root/"old", manifest, "b"*40, live=True, settings=settings)
            locks = extend(root/"old", old, root/"out", "c"*40, "sha256:"+"d"*64,
                           ROOT/"examples/stationary-perception/jetson-rover.json", "rover")
            initial, _ = verify(root/"out/ready", locks["ready"])
            candidate, _ = verify(root/"out/candidate", locks["candidate"])
            self.assertEqual({"perception.mark_target"}, set(candidate["plugins"]) - set(initial["plugins"]))
            self.assertTrue(all(candidate["plugins"][k] == v for k,v in initial["plugins"].items()))
        pin = {"id": "mark_target", "binding": "sha256:"+"1"*64}
        trigger_input({"kind":"agent","source":"plugin.supervisor","id":"follow","parent_task_id":"original",
                       "trace_id":"original","parent_event_id":"release","read_only_tool":pin})
        cap = {"id":"mark_target","version":"1.0.0","parameters":{"target":"string"},"effect":"read",
               "_soft":{"binding":pin["binding"]}, "utterances":["在二维和三维标注{target}"]}
        provider, steps = plan_read_only("在二维和三维标注左侧椅子", {"mark_target":cap}, pin)
        self.assertEqual("左侧椅子", steps[0]["args"]["target"])
        for change in ({"effect":"actuation"}, {"_soft":{"binding":"sha256:"+"f"*64}}, {"_soft":None}):
            with self.assertRaises(Exception):
                plan_read_only("在二维和三维标注左侧椅子", {"mark_target":{**cap, **change}}, pin)


if __name__ == "__main__":
    unittest.main()
