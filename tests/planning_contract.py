"""Contract regression for language input, capability swaps, and untrusted plans."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "agent"))
import planning
import tracing


class DeployedConfiguration(unittest.TestCase):
    """Read the same checked-in files as Compose, without contacting any provider."""

    def check_tools(self, plugins, extra):
        with patch.dict(os.environ, {"EDGE_PLUGIN_DIR": str(plugins), "EDGE_EXTRA_PLUGIN_DIR": str(extra)}):
            caps = planning.registry()
        seen = set()
        for folder in (plugins, extra):
            for path in sorted(folder.glob("*.json")):
                manifest = json.loads(path.read_text(encoding="utf-8-sig"))
                tool = manifest["id"]
                self.assertNotIn(tool, seen, f"{path.name}: duplicate deployed tool {tool}")
                seen.add(tool)
                self.assertTrue(caps.get(tool) == manifest, f"{path.name}: runtime did not register {tool}")

    def check_override(self, path):
        # Isolate each layer. A later valid override or a matching builtin must not hide a bad file in CI.
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            overrides = root / "contracts"
            overrides.mkdir()
            shutil.copyfile(path, overrides / path.name)
            empty_builtins = root / "defaults.json"
            empty_builtins.write_text("{}", encoding="utf-8")
            doc = json.loads(path.read_text(encoding="utf-8-sig"))
            with patch.dict(os.environ, {"EDGE_CONTRACT_DIR": str(overrides)}), patch.object(tracing, "BUILTINS", empty_builtins):
                if path.name == "planner-profile.json":
                    profile = planning.planner_profile()
                    for key in ("version", "protocol", "url"):
                        self.assertTrue(profile[key] == doc[key], f"{path.name}: different {key}")
                    return
                self.assertEqual(doc.get("schema_version"), 1, path.name)
                self.assertTrue(doc.get("prompts") or doc.get("diagnostics"), f"{path.name}: no runtime entries")
                for section, field in (("prompts", "text"), ("diagnostics", "hint")):
                    for key, entry in doc.get(section, {}).items():
                        actual = tracing.contract(section, key)
                        self.assertIsNotNone(actual, f"{path.name}: runtime did not load {section}.{key}")
                        self.assertEqual((actual["version"], actual[field]), (entry["version"], entry[field]), path.name)

    def test_compose_deployment_files(self):
        self.check_tools(ROOT / "plugins", ROOT / "scene/plugins")
        for path in sorted((ROOT / "contracts").glob("*.json")):
            with self.subTest(file=path.name):
                self.check_override(path)

    def test_rejected_deployment_files_fail_the_check(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            tools = root / "tools"
            tools.mkdir()
            manifest = {"id":"bad_tool", "version":"1", "parameters":{}, "effect":"read",
                        "description":"invalid endpoint", "endpoint":"missing-host"}
            (tools / "tool.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "did not register"):
                self.check_tools(tools, root / "extras")
            profile = root / "planner-profile.json"
            profile.write_text('{"schema_version":1,"version":"v1","protocol":"unknown","url":"http://test"}', encoding="utf-8")
            with self.assertRaises(planning.Refused):
                self.check_override(profile)
            override = root / "site.json"
            override.write_text('{"schema_version":1,"prompts":{"planner.system":{"version":"v2","text":""}}}', encoding="utf-8")
            with self.assertRaisesRegex(AssertionError, "did not load"):
                self.check_override(override)

    def test_planner_profile_and_prompt_load_without_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "planner-profile.json"
            profile.write_text('{"schema_version":1,"version":"v1","protocol":"http-planner","url":"http://test"}', encoding="utf-8")
            self.check_override(profile)
            override = root / "site.json"
            override.write_text('{"schema_version":1,"prompts":{"planner.system":{"version":"v2","text":"local test"}}}', encoding="utf-8")
            self.check_override(override)


class PlannerContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":self.directory.name,
                                 "EDGE_CONTRACT_DIR":self.directory.name, "EDGE_EXTRA_PLUGIN_DIR":"",
                                 "EDGE_PLANNER_URL":"http://planner/invoke","EDGE_OLLAMA_URL":""})
        environment.start()
        self.addCleanup(environment.stop)

    def proposal(self, text, steps):
        with patch.object(planning, "http_json", return_value={"steps":steps, "reason":""}) as request:
            value = planning.plan(text, planning.registry())
        self.assertEqual(request.call_args.args[1]["request"], text)
        return value

    def test_native_multistep(self):
        provider, steps = planning.plan("依次巡检 (0.5,0) 和 (0,0)，然后报告状态", planning.registry())
        self.assertEqual(provider, "local")
        self.assertEqual([s["tool"] for s in steps], ["inspect","status"])

    def test_same_tool_description_requires_specific_id(self):
        caps = dict(planning.BASE)
        for key in ("derived.forward", "derived.previous"):
            caps[key] = {"id":key,"description":"报告前向读数。","parameters":{}}
        with self.assertRaises(planning.Refused) as error:
            planning.local_plan("报告前向读数",caps)
        self.assertEqual(error.exception.code,"planner.tool_ambiguous")
        self.assertEqual(planning.local_plan("derived.previous",caps)[0]["tool"],"derived.previous")
        caps.pop("derived.previous")
        self.assertEqual(planning.local_plan("报告前向读数",caps)[0]["tool"],"derived.forward")

    def test_registered_provider_can_replace_builtin(self):
        config = {"id":"status","description":"读取状态","effect":"read","version":"2",
                  "parameters":{},"endpoint":"http://adapter/a"}
        path = Path(self.directory.name)/"status.json"
        path.write_text(json.dumps(config))
        caps = planning.registry()
        provider, steps = self.proposal("我想知道设备现在的姿态", [{"tool":"status","args":{},"status":"SUCCEEDED"}])
        self.assertEqual((provider, steps[0]["status"], steps[0]["version"]), ("http-planner","PENDING","2"))
        self.assertEqual(steps[0]["binding"], planning.binding(caps["status"]))
        path.write_text(json.dumps(dict(config,endpoint="http://adapter/b")))
        self.assertNotEqual(steps[0]["binding"], planning.binding(planning.registry()["status"]))
        path.unlink()
        self.assertEqual(planning.registry()["status"], planning.BASE["status"])

    def test_bad_endpoint_does_not_disable_other_tools(self):
        folder = Path(self.directory.name)
        cap = {"id":"read_gauge", "version":"1", "parameters":{}, "description":"read gauge",
               "endpoint":"http://fixture/read", "effect":"read"}
        (folder/"healthy.json").write_text(json.dumps(cap))
        for endpoint in (42, ["wrong"], {"url":"wrong"}):
            with self.subTest(endpoint=endpoint):
                (folder/"bad.json").write_text(json.dumps(dict(cap, id="broken", endpoint=endpoint)))
                with patch("builtins.print") as logged:
                    caps = planning.registry()
                self.assertEqual(caps["read_gauge"], cap)
                self.assertEqual(caps["status"], planning.BASE["status"])
                self.assertNotIn("broken", caps)
                self.assertIn("bad.json: endpoint", str(logged.call_args))

    def test_unreadable_manifest_is_isolated(self):
        folder = Path(self.directory.name)
        good = {"id":"read_gauge","version":"1","parameters":{},"description":"read gauge",
                "endpoint":"http://fixture/read","effect":"read"}
        (folder/"ok.json").write_text(json.dumps(good))
        (folder/"deep.json").write_text("["*1200 + "]"*1200)
        (folder/"large.json").write_text(json.dumps(dict(good, id="oversize", description="x"*131072)))
        with patch("builtins.print"):
            caps = planning.registry()
        self.assertEqual(caps["read_gauge"], good)
        self.assertEqual(caps["status"], planning.BASE["status"])
        self.assertNotIn("oversize", caps)

    def test_rejects_unsupported_or_incomplete_physical_plan(self):
        def inspect(*points):
            return {"tool":"inspect","args":{"points":list(points)}}

        cases = [
            ("去拿一杯水", [{"tool":"pick","args":{}}]),
            ("麻烦去 (1,0) 和 (2,0)", [inspect([1,0])]),
            ("麻烦去 (1,0) 和 (2,0)", [inspect([2,0],[1,0])]),
            ("请去 (1,0) 看一眼", [inspect([3,0])]),
            ("请不要去 (1,0)", [inspect([1,0])]),
            ("麻烦去 (8,0)", [inspect([8,0])]),
            ("麻烦去 (1,0)", [{"tool":"status","args":{}}]),
        ]
        for text, steps in cases:
            with self.subTest(text=text,steps=steps), self.assertRaises(planning.Refused):
                self.proposal(text, steps)

    def test_required_deliverable_cannot_be_satisfied_by_status(self):
        status = {"tool":"status","args":{}}
        for request in ("帮我拍一张照片","报告位置，同时拍照","帮我测量电池电量","替我写一篇作文"):
            with self.subTest(request=request), self.assertRaises(planning.Refused):
                self.proposal(request, [status])
        self.assertEqual(len(self.proposal("能读一下位置、航向、速度吗？",[status,status])[1]),1)
        camera = {"id":"camera","version":"1","effect":"idempotent","parameters":{},"description":"采集图像",
                  "endpoint":"http://camera/capture","outcomes":{"image":"uri"},"outcome_terms":{"image":["扫描相纸"]}}
        (Path(self.directory.name)/"camera.json").write_text(json.dumps(camera))
        with self.assertRaises(planning.Refused):
            self.proposal("请扫描相纸", [status])
        self.assertEqual(self.proposal("请拍一张照片",[{"tool":"camera","args":{}}])[1][0]["tool"],"camera")
        with self.assertRaises(planning.Refused):
            self.proposal("请去 (1,0) 拍一张照片",[{"tool":"camera","args":{}}])
        _,steps=self.proposal("请去 (1,0) 拍一张照片",[
            {"tool":"inspect","args":{"points":[[1,0]]}},{"tool":"camera","args":{}}])
        self.assertEqual([s["tool"] for s in steps],["inspect","camera"])


if __name__ == "__main__":
    unittest.main()
