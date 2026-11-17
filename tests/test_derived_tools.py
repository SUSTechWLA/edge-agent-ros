"""Contracts for AI-authored declarative tools, exercised without a model or ROS."""
import json
import math
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
from derived_tools import MAX_BYTES, capability, evaluate, load_directory, parse_definition, validate


SPEED = {"schema_version": 1, "id": "derived.forward_speed", "description": "读取前向速度",
         "field": "speed", "unit": "cm/s"}
SAMPLE = {"source": "/robot/odom", "frame": "wheel_odom", "stamp_ns": 345_000_000,
          "x": 1.25, "y": -0.5, "speed": -0.25, "yaw": math.pi / 2}


class DerivedToolsTest(unittest.TestCase):
    def test_canonical_versions_change_with_definition(self):
        definition = {**SPEED, "comparison": {"op": "le", "threshold": 1}}
        same = {**dict(reversed(list(definition.items()))), "comparison": {"threshold": 1.0, "op": "le"}}
        first = capability(definition)
        self.assertRegex(first["version"], r"^[a-f0-9]{64}$")
        self.assertEqual(first, capability(same))
        self.assertEqual(first["effect"], "read")
        self.assertEqual(first["parameters"], {})
        self.assertNotIn("endpoint", first)
        for change in ({"unit": "m/s"}, {"description": "另一个描述"},
                       {"comparison": {"op": "gt", "threshold": 1}},
                       {"comparison": {"op": "le", "threshold": 1.5}}):
            self.assertNotEqual(first["version"], capability({**definition, **change})["version"])
        definition["comparison"]["threshold"] = 9000
        self.assertEqual(first["derived"]["comparison"]["threshold"], 1)

    def test_only_declared_read_operations_can_be_loaded(self):
        for patch in ({"endpoint": "http://provider"}, {"command": "ignored"}, {"effect": "idempotent"},
                      {"parameters": {"p": "string"}}, {"schema_version": True}, {"schema_version": 2},
                      {"schema_version": 1.0}, {"field": ["speed"]}, {"field": "cmd_vel"},
                      {"unit": "deg"}, {"comparison": None}, {"description": " \t"},
                      {"description": "first\nnext"}, {"description": "s" * 257},
                      {"id": "inspect"}, {"id": "derived../tool"}, {"id": "derived.Speed"},
                      {"id": "derived." + "a" * 60}):
            with self.subTest(patch=patch), self.assertRaises(ValueError):
                validate({**SPEED, **patch})
        for compare in ({"op": "ge"}, {"op": "run", "threshold": 1}, {"op": "lt", "threshold": True},
                        {"op": "lt", "threshold": "1"}, {"op": "eq", "threshold": 1},
                        {"op": "gt", "threshold": float("nan")}, {"op": "le", "threshold": float("inf")},
                        {"op": "lt", "threshold": 10**500}, {"op": "gt", "threshold": 1, "action": "inspect"}):
            with self.subTest(compare=compare), self.assertRaises(ValueError):
                capability({**SPEED, "comparison": compare})
        with self.assertRaises(ValueError):
            validate({key: value for key, value in SPEED.items() if key != "id"})

    def test_conversions_use_the_selected_measurement_and_keep_source_evidence(self):
        for field, unit, expected in (("x", "m", 1.25), ("x", "cm", 125), ("x", "mm", 1250),
                                      ("y", "mm", -500), ("yaw", "rad", math.pi / 2), ("yaw", "deg", 90),
                                      ("speed", "m/s", -0.25), ("speed", "cm/s", -25), ("speed", "km/h", -0.9)):
            with self.subTest(field=field, unit=unit):
                evidence = evaluate({**SPEED, "field": field, "unit": unit}, SAMPLE)
                self.assertAlmostEqual(evidence["value"], expected)
                self.assertEqual(evidence["raw_value"], SAMPLE[field])
                self.assertEqual(evidence["raw_unit"], {"x": "m", "y": "m", "yaw": "rad", "speed": "m/s"}[field])
                self.assertEqual(evidence["field"], field)
                self.assertEqual(evidence["unit"], unit)
                self.assertNotIn("matches", evidence)
                for key in ("source", "frame", "stamp_ns"):
                    self.assertEqual(evidence[key], SAMPLE[key])

    def test_threshold_compares_in_requested_unit(self):
        for op, expected in (("lt", False), ("le", True), ("gt", False), ("ge", True)):
            compare = {"op": op, "threshold": -25}
            result = evaluate({**SPEED, "comparison": compare}, SAMPLE)
            self.assertIs(result["matches"], expected)
            self.assertEqual(result["comparison"], compare)

    def test_bad_observations_never_produce_evidence(self):
        for change in ({"speed": float("nan")}, {"speed": float("inf")}, {"speed": True},
                       {"speed": 1e308}, {"speed": "0"}, {"speed": None}, {"stamp_ns": 1.0},
                       {"stamp_ns": False}, {"stamp_ns": 0}, {"stamp_ns": -1}, {"source": ""},
                       {"frame": "  "}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                evaluate(SPEED, {**SAMPLE, **change})

    def test_decode_limits_and_duplicate_keys(self):
        valid = json.dumps(SPEED).encode()
        self.assertEqual(parse_definition(b"\xef\xbb\xbf" + valid), SPEED)
        for raw in (b"\xff", b'{"schema_version":1,"schema_version":1}',
                    valid[:-1] + b',"comparison":{"op":"gt","threshold":0,"threshold":2}}',
                    valid + b" " * MAX_BYTES, b"[" * 1500 + b"]" * 1500):
            with self.assertRaises(ValueError):
                parse_definition(raw)

    def test_atomic_replacement_and_unload_are_visible_to_next_snapshot(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            self.assertEqual(load_directory(root / "not-installed"), {})
            live = root / (SPEED["id"] + ".json")
            staging = root / "draft.tmp"
            staging.write_text(json.dumps(SPEED), encoding="utf-8")
            self.assertEqual(load_directory(root), {})
            staging.replace(live)
            old = load_directory(root)
            broken = root / "derived.broken.json"
            broken.write_bytes(b"{")
            (root / "derived.alias.json").write_text(json.dumps(SPEED), encoding="utf-8")
            (root / "derived.oversized.json").write_bytes(b" " * (MAX_BYTES + 1))
            staging.write_text(json.dumps({**SPEED, "unit": "km/h"}), encoding="utf-8")
            staging.replace(live)
            with self.assertLogs("derived_tools", "WARNING"):
                new = load_directory(root)
            self.assertEqual(set(new), {SPEED["id"]})
            self.assertNotEqual(old[SPEED["id"]]["version"], new[SPEED["id"]]["version"])
            self.assertEqual(old[SPEED["id"]]["derived"]["unit"], "cm/s")
            live.unlink()
            with self.assertLogs("derived_tools", "WARNING"):
                self.assertEqual(load_directory(root), {})


if __name__ == "__main__":
    unittest.main()
