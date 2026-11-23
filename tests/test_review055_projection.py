import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from console.server import review_site


class ReviewProjection(unittest.TestCase):
    def test_completed_operation_keeps_its_binding_after_site_removal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            opts=SimpleNamespace(agent="fixture",config=root,frame=root/"frame.json",history=root/"ops.jsonl")
            frame={"rgb_ref":"sha256:"+"1"*64,"depth_ref":"sha256:"+"2"*64,"depth_stamp_ns":"1000"}
            opts.frame.write_text(json.dumps(frame))
            frame_ref="sha256:"+hashlib.sha256(opts.frame.read_bytes()).hexdigest()
            old={"tool":"old_tool","provider":"review.old","binding":"sha256:"+"3"*64,"origin":"site_workflow"}
            new={**old,"tool":"new_tool","provider":"review.new","binding":"sha256:"+"4"*64}
            (root/"implementations.json").write_text(json.dumps({"contracts":{"review":{"implementations":[new]}}}))
            selected={"event_id":"decision","trace_id":"trace","tool":"old_tool","candidates":[{**old,"eligible":True}]}
            pin={"plugin_id":"review.old","version":"1.0.0","content_digest":"sha256:"+"5"*64,"binding":old["binding"]}
            step={"soft_pin":pin,"operation_id":"op","binding":old["binding"],"tool":"old_tool","args":{},
                  "status":"SUCCEEDED","evidence":{"frame_ref":frame_ref,"analysis":frame,"review":{"review_order":[2]}}}
            task={"id":"t","trace_id":"trace","trigger":{"kind":"user"},"text":"review","state":"SUCCEEDED",
                  "created":"2026-01-01T00:00:00Z","steps":[step],"events":[{"kind":"planner.implementation_selected",
                  "event_id":"decision","payload":{"trace":selected},"utc":"2026-01-01T00:00:01Z"}]}
            def read(_base,endpoint,**_query):
                return 200,{"tasks":task,"task-index":{"tasks":[{"id":"t"}]},"capabilities":{"records":[]}}[endpoint]
            with patch.object(review_site,"native",read):
                snap=review_site.project(opts)
                self.assertEqual(snap["providers"][0]["id"],"review.new")
                self.assertEqual(snap["runs"][0]["provider"],"review.old")
                self.assertEqual(snap["runs"][0]["review"]["review_order"],[2])
                pin["binding"]=new["binding"]
                self.assertNotIn("review",review_site.project(opts)["runs"][0])
