"""Contract regression for language input, capability swaps, and untrusted plans."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import planning


class PlannerContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = patch.dict(os.environ, {"EDGE_PLUGIN_DIR":self.directory.name,
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


if __name__ == "__main__":
    unittest.main()
