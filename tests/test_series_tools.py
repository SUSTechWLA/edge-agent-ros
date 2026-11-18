"""Verify data-only authored software tools and their compatibility with the Agent."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"agent"))
import author_tool
from derived_tools import capability, evaluate, validate
from planning import binding, local_plan, plan, plan_read_only, Refused, validate_series_input


DEFINITION = {"schema_version": 2, "id": "derived.sample_gap", "description": "最大采样间隔",
              "transforms": ["diff"], "reduce": "max"}


class SoftwareTools(unittest.TestCase):
    def test_calculation_in_input_order_and_provenance(self):
        args = {"series": "[4, 1, 6, 2]"}
        result = evaluate(DEFINITION, args)
        self.assertEqual(result, {"source": "arguments.series", "input_sha256": hashlib.sha256(b"[4,1,6,2]").hexdigest(),
                                  "input_count": 4, "output_count": 3, "value": 5})
        for operations, reducer, expected in [
            (["sort", "diff"], "max", 2), (["diff","abs"], "sum", 12),
            (["unique"], "count", 4), ([], "mean", 3.25), ([], "min", 1),
        ]:
            self.assertEqual(evaluate(dict(DEFINITION, transforms=operations, reduce=reducer), args)["value"], expected)
        self.assertEqual(evaluate(dict(DEFINITION, transforms=["unique","diff"], reduce="min"),
                                  {"series": "[7,7,2,7,4]"})["value"], -5)

    def test_bounded_declarations_and_inputs(self):
        for new in ({"transforms":["exec"]}, {"transforms":["abs"]*5}, {"reduce":"script"},
                    {"endpoint":"http://example"}, {"transforms":[{}]}, {"description":"{series}"}):
            with self.subTest(new=new), self.assertRaises(ValueError):
                validate({**DEFINITION, **new})
        for value in ("[]", "[9]", "[true,2]", "[1.0,2]", "[[1,2]]", "[1000000001,2]", "[NaN,0]",
                      "[0," + "1,"*127 + "2]", "open(file)", "[1" + " "*4096 + "]"):
            with self.subTest(sample=value[:30]), self.assertRaises(ValueError):
                evaluate(DEFINITION, {"series": value})
        for args in ({}, {"series": "[1,2]", "file":"a"}, {"series":[1,2]}):
            with self.assertRaises(ValueError):
                evaluate(DEFINITION, args)

    def test_local_and_pinned_plans_use_task_series_and_version(self):
        cap = capability(DEFINITION)
        caps = {cap["id"]:cap}
        request = "最大采样间隔 [3,7,12]"
        with tempfile.TemporaryDirectory() as empty, patch.dict(os.environ, {"EDGE_CONTRACT_DIR":empty}):
            for provider, steps in (plan(request, caps), plan_read_only(request, caps, cap)):
                self.assertEqual(steps[0]["args"], {"series":"[3,7,12]"})
                self.assertEqual(steps[0]["binding"], binding(cap))
            for wrong in ("不同标题 [3,7,12]", "最大采样间隔", "最大采样间隔 [1.25]"):
                with self.assertRaises(Refused):
                    plan_read_only(wrong, caps, cap)
            with self.assertRaises(Refused):
                validate_series_input(request, {"series":"[3,6,12]"}, cap)
            shorter = capability({**DEFINITION, "id":"derived.other", "description":"最大", "reduce":"sum"})
            both = {cap["id"]:cap, shorter["id"]:shorter}
            self.assertEqual(local_plan(request, both)[0]["tool"], cap["id"])
            self.assertIsNone(local_plan("最大限度解释结果", both))
            named = capability({**DEFINITION, "description":"计算：差值"})
            self.assertEqual(plan("计算：差值 [3,7]", {cap["id"]: named})[1][0]["args"], {"series":"[3,7]"})
            with self.assertRaisesRegex(Refused, "requested operation"):
                validate_series_input(request, {"series":"[3,7,12]"}, shorter)
            changed = capability({**DEFINITION, "transforms":["sort","diff"]})
            with self.assertRaises(Refused):
                plan_read_only(request, {cap["id"]:changed}, cap)
            old = capability({"schema_version":1, "id":cap["id"], "description":"之前的读数",
                              "field":"speed","unit":"m/s"})
            with self.assertRaises(Refused) as swapped:
                plan_read_only(old["description"], caps, old)
            self.assertEqual(swapped.exception.code, "planner.approved_binding_unavailable")

    def test_author_prompt_excludes_sample_and_publish_rejects_incorrect_semantics(self):
        reply = {"reduce":"max","transforms":["diff"]}
        body = json.dumps({"message":{"content":json.dumps(reply)}}).encode()
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = SimpleNamespace(id=DEFINITION["id"],request="最大采样间隔 [333,736,1005]",from_task=None,
                                   record=root/"model.json",ollama_url="http://local",model="fixture")
            with patch.object(author_tool, "urlopen", return_value=io.BytesIO(body)) as call:
                self.assertEqual(author_tool.draft(args), DEFINITION)
                self.assertNotIn(b"736", call.call_args.args[0].data)
            self.assertEqual(json.loads(args.record.read_bytes())["reply"], reply)
            args.request = "读取本机前向速度 [km/h]"
            v1 = {"field":"speed","unit":"km/h","op":"read","threshold":0}
            reply_v1 = json.dumps({"message":{"content":json.dumps(v1)}}).encode()
            with patch.object(author_tool, "urlopen", return_value=io.BytesIO(reply_v1)):
                self.assertEqual(author_tool.draft(args)["schema_version"], 1)
            candidate, cases = root/"candidate.json", root/"cases.json"
            cases.write_text(json.dumps([{"sample":{"series":"[4,8,9]"}, "expected":{"value":4}},
                                         {"sample":{"series":"[10,6,2]"}, "expected":{"value":-4}}]))
            candidate.write_text(json.dumps(DEFINITION))
            self.assertEqual(author_tool.checked_definition(candidate, cases)[1]["case_count"], 2)
            candidate.write_text(json.dumps({**DEFINITION, "transforms":[]}))
            with self.assertRaisesRegex(ValueError, "differs"):
                author_tool.checked_definition(candidate, cases)
            candidate.write_text(json.dumps(DEFINITION))
            values = json.loads(cases.read_bytes())
            values[0]["expected"]["value"] += 1e-10
            cases.write_text(json.dumps(values))
            with self.assertRaisesRegex(ValueError, "differs"):
                author_tool.checked_definition(candidate, cases)


if __name__ == "__main__":
    unittest.main()
