"""A fresh read-only object tool preserves the existing live site and pins its real input frame."""
import http.client
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT/"agent"), str(ROOT/"scripts"), str(ROOT/"tests")]
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.object_server import ObjectPerception, read_message, socket_prediction
from perception.spatial_facts.object_service import locate
from perception.spatial_facts.perception_server import handler as compute_handler


class ObjectRelease(unittest.TestCase):
    def test_add_to_current_site_without_rebinding_other_modules(self):
        from planning import local_plan
        from prepare_navigation import bench_settings, prepare
        from prepare_perception_site import prepare as extend
        from site_pack import verify
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            settings = bench_settings()
            del settings["sensing.replay"]
            manifest = json.dumps({"schemaVersion":2,"config":{"digest":"sha256:"+"a"*64},"layers":[]}).encode()
            old_lock = prepare(base/"nav",manifest,"a"*40,live=True,settings=settings)
            backend = "sha256:"+"b"*64
            prior = extend(base/"nav",old_lock,base/"initial","c"*40,backend,
                           ROOT/"examples/stationary-perception/jetson-rover.json","rover")["candidate"]
            site = base/"initial/candidate"
            current, _ = verify(site, prior)
            result = extend(site,prior,base/"next","d"*40,backend,
                            ROOT/"examples/object-perception/jetson-rover.json","rover",objects=True)
            latest, releases = verify(base/"next/objects", result["objects"])
            self.assertEqual({"perception.see_object","perception.object_location"}, set(latest["plugins"])-set(current["plugins"]))
            self.assertTrue(all(latest["plugins"][key]==v for key,v in current["plugins"].items()))
            service = releases["perception.object_location"]
            self.assertEqual(service["requires"][0]["name"],"sensing.observations")
            self.assertEqual(service["network"],{"egress":["object-perception"],"ingress":[]})
            cap = releases["perception.see_object"]["config_schema"]["properties"]["profile"]["const"]["legacy"]
            self.assertEqual(local_plan("请帮我找到画面右侧的绿植，量一下它离相机有多远。", {cap["id"]:cap}),
                             [{"tool":"see_object","args":{"target":"画面右侧的绿植"}}])

    def test_protocol_size_and_partial_frame_rejected(self):
        for raw in (b"",struct.pack("!I",10)+b"1",struct.pack("!I",35)):
            with self.assertRaisesRegex(SpatialFactError,"model_protocol"):
                read_message(io.BytesIO(raw),20)

    @unittest.skipUnless(sys.platform.startswith("linux"), "Linux detector Unix socket")
    def test_end_to_end_two_peers_and_pinned_evidence(self):
        from http.server import HTTPServer
        from socketserver import UnixStreamServer
        from perception.spatial_facts.object_worker import handler as model_handler, warmup_png
        from perception.spatial_facts.artifact_ring import ArtifactRing
        from test_object_grounding import ObjectSurface
        from threading import Thread
        f = ObjectSurface()
        f.setUp()
        demo = f.fixture
        profile = {"schema_version":1,"actor_ref":demo.profile["actor_ref"],"grounding":dict(demo.profile["grounding"])}
        g = profile["grounding"]
        g["depth_profile"] = demo.profile["sources"][g["depth_source"]]
        ids = [g[k] for k in ("rgb_source","depth_source","calibration_source")]
        profile["mounts"] = {s:{"format":fmt,"root":s} for s,fmt in zip(ids,["image","image","calibration"])}
        backend_ref = "sha256:"+"c"*64
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            for sample in demo.batch["samples"]:
                sid = sample["source_id"]
                ref = sample["artifact_ref"]
                data = demo.store[ref]
                target = root/sid
                target.mkdir()
                if sid == g["calibration_source"]:
                    (target/"artifacts").mkdir()
                    (target/"artifacts"/(ref[7:]+".json")).write_bytes(data)
                    (target/"observations.json").write_text(json.dumps({**demo.batch,"samples":[sample]}))
                else:
                    ArtifactRing(target,profile["actor_ref"],sid,media_type="image/png",suffix=".png").publish(
                        {**sample,"stamp_ns":int(sample["stamp_ns"])},data)
            path = root/"site.json"
            path.write_text(json.dumps(profile))
            addr = root/"detector.sock"
            fixture_service = ObjectPerception(path,root,backend_ref,addr)
            with UnixStreamServer(str(addr), model_handler(f.prediction)) as model, HTTPServer(
                    ("127.0.0.1",0),compute_handler(fixture_service,endpoints=("/locate",))) as api:
                Thread(target=model.serve_forever,daemon=True).start()
                Thread(target=api.serve_forever,daemon=True).start()
                try:
                    with self.assertRaisesRegex(SpatialFactError,"model_binding_mismatch"):
                        socket_prediction(addr,warmup_png(),"sha256:"+"d"*64)
                    conn = http.client.HTTPConnection
                    cfg={"backend_digest":backend_ref,"profile_digest":fixture_service.profile_digest}
                    req={"actor_ref":profile["actor_ref"],"target":"画面右侧的绿植"}
                    with patch("perception.spatial_facts.object_server.time.time_ns",return_value=demo.now), patch(
                            "perception.spatial_facts.object_service.HTTPConnection",
                            side_effect=lambda *a,**kw:conn(*api.server_address,timeout=3)):
                        result=locate(req,demo.batch,cfg)
                        self.assertEqual(result["grounding"]["point_depth_raw"],850)
                        self.assertEqual(result["observations"],demo.batch)
                        with self.assertRaisesRegex(SpatialFactError,"deployment_mismatch"):
                            locate(req,demo.batch,{**cfg,"backend_digest":"sha256:"+"d"*64})
                finally:
                    api.shutdown()
                    model.shutdown()


if __name__ == "__main__":
    unittest.main()
