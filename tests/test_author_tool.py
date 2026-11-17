"""Exercise authoring validation, atomic publication and recovery on a private registry."""
from concurrent.futures import ThreadPoolExecutor
import copy
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

AGENT = Path(__file__).resolve().parents[1] / "agent"
sys.path.insert(0, str(AGENT))
import author_tool
from derived_tools import capability, load_directory


def speed_case(speed, expected, **predicate):
    return {
        "sample": {"source":"/fixture/odom", "frame":"odom", "stamp_ns":17, "speed":speed},
        "expected": {"unit":"km/h", "value":expected, **predicate},
    }


def blocked_task(code="planner.capability_missing"):
    task_id, trace_id = "measure-17", "operator-6"
    diagnosis = {"code":code, "stage":"capability_check", "origin":"planner"}
    trace = {"schema_version":1, "event_id":"event-2", "trace_id":trace_id, "parent_event_id":"event-1",
             "module":"agent", "layer":"planner", "code":code, "diagnosis":diagnosis}
    payload = {"provider":None, "prior_state":"PLANNING", "state":"BLOCKED", "retries":0,
               "diagnostic":diagnosis, "trace":trace}
    event = {"seq":2, "task_id":task_id, "step":0, "kind":"blocked", "operation_id":None,
             "event_id":"event-2", "payload":json.dumps(payload)}
    accepted_trace = {"schema_version":1, "event_id":"event-1", "trace_id":trace_id, "parent_event_id":None,
                      "module":"agent", "layer":"task", "code":"agent.accepted"}
    accepted = dict(event, seq=1, kind="accepted", event_id="event-1", payload=json.dumps({"trace":accepted_trace}))
    return {"id":task_id, "text":"用千米每小时报告前向速度", "state":"BLOCKED", "trace_id":trace_id,
            "steps":[], "cursor":0, "events_has_more":False, "events":[accepted,event]}


class TestAuthorTool(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.registry = self.root / "registry"
        self.name = "derived.forward_speed"
        self.active = self.registry / (self.name + ".json")
        self.first = {"schema_version":1, "id":self.name, "description":"report signed speed",
                      "field":"speed", "unit":"km/h"}
        self.second = dict(self.first, description="report signed forward velocity")
        self.samples = [speed_case(-.5,-1.8), speed_case(1.25,4.5)]
        self.cases = self.write("cases.json", self.samples)
        self.original = self.write("first.json", self.first)
        self.updated = self.write("second.json", self.second)

    def write(self, name, data):
        path = self.root / name
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def change(self, action, definition, expect):
        return author_tool.change(SimpleNamespace(action=action, definition=definition, cases=self.cases,
                                                id=self.name, registry=self.registry, expect=expect))

    def draft_args(self, task):
        return SimpleNamespace(id=self.name, request=None, from_task=self.write("task.json", task),
                               record=self.root/"decision.json", ollama_url="http://fixture", model="test-model")

    def test_draft_from_blocked_task_uses_the_request_and_records_its_origin(self):
        answer = {"field":"speed", "unit":"km/h", "op":"read", "threshold":0}
        for code in ("planner.capability_missing", "planner.not_configured"):
            with self.subTest(reason=code):
                task = blocked_task(code)
                args = self.draft_args(task)
                wire = json.dumps({"message":{"content":json.dumps(answer)}}).encode()
                argv = ["author_tool.py","draft","--id",args.id,"--from-task",str(args.from_task),
                        "--model",args.model,"--ollama-url",args.ollama_url,"--record",str(args.record)]
                output = io.BytesIO()
                with (patch.object(author_tool,"urlopen",return_value=io.BytesIO(wire)) as endpoint,
                      patch.object(sys,"argv",argv), patch.object(sys,"stdout",SimpleNamespace(buffer=output))):
                    author_tool.main()
                candidate = json.loads(output.getvalue())
                endpoint.assert_called_once()
                http = endpoint.call_args.args[0]
                self.assertEqual(http.full_url, "http://fixture/api/chat")
                model_input = json.loads(http.data)
                user = json.loads(next(m["content"] for m in model_input["messages"] if m["role"]=="user"))
                self.assertEqual((user["request"], user["reason_code"]), (task["text"], code))
                record = json.loads(args.record.read_bytes())
                self.assertEqual(record["source_task"], {"task_id":task["id"], "trace_id":task["trace_id"],
                                                        "reason_code":code})
                self.assertEqual(record["request"], task["text"])
                self.assertEqual(candidate, dict(self.first, description=task["text"]))
                self.assertFalse(self.registry.exists())

    def test_recovery_active_or_unclear_tasks_do_not_reach_the_author_model(self):
        base = blocked_task()
        accepted, terminal = base["events"]
        payload = json.loads(terminal["payload"])
        mismatched_trace = dict(payload, trace=dict(payload["trace"], trace_id="another-trace"))
        dispatched = dict(terminal, kind="dispatch", operation_id="measure-17/step/1")
        cases = {
            "recovering":dict(base, state="RECOVERY_REQUIRED"),
            "in progress":dict(base, state="WAITING"),
            "planned step":dict(base, steps=[{"tool":"status","status":"RUNNING"}]),
            "dispatched":dict(base, events=[accepted, dispatched, terminal]),
            "incomplete page":dict(base, events_has_more=True),
            "unrelated cause":blocked_task("navigation.stop_unconfirmed"),
            "wrong task":dict(base,events=[accepted, dict(terminal,task_id="other")]),
            "wrong trace":dict(base,events=[accepted, dict(terminal,payload=json.dumps(mismatched_trace))]),
            "no terminal diagnosis":dict(base,events=[accepted, dict(terminal,payload="{}")]),
        }
        for label, task in cases.items():
            with self.subTest(case=label), patch.object(author_tool,"urlopen",side_effect=AssertionError("unexpected model call")) as endpoint:
                args = self.draft_args(task)
                with self.assertRaises(ValueError):
                    author_tool.draft(args)
                endpoint.assert_not_called()
                self.assertFalse(args.record.exists())

    def test_unsupported_model_reply_leaves_only_a_record_of_the_refusal(self):
        task = blocked_task()
        task["text"] = "测量电池温度"
        args = self.draft_args(task)
        unsupported = {"field":"unsupported", "unit":"none", "op":"read", "threshold":0}
        wire = json.dumps({"message":{"content":json.dumps(unsupported)}}).encode()
        with patch.object(author_tool,"urlopen",return_value=io.BytesIO(wire)) as endpoint:
            with self.assertRaises(ValueError):
                author_tool.draft(args)
        endpoint.assert_called_once()
        self.assertEqual(json.loads(args.record.read_bytes())["reply"], unsupported)
        self.assertFalse(self.registry.exists())

    def test_checked_definition_rejects_bad_samples_and_incompatible_dimensions(self):
        valid, evidence = author_tool.checked_definition(self.original, self.cases)
        self.assertEqual(valid, self.first)
        self.assertEqual(evidence["case_count"], 2)
        for field, bad in (("stamp_ns",0), ("source",""), ("frame",""), ("speed",True),
                           ("speed",float("nan"))):
            cases = copy.deepcopy(self.samples)
            cases[0]["sample"][field] = bad
            with self.subTest(field=field), self.assertRaises(ValueError):
                author_tool.checked_definition(self.original, self.write("invalid.json", cases))
        dimension_error = dict(self.first, field="yaw", unit="km/h")
        with self.assertRaises(ValueError):
            author_tool.checked_definition(self.write("wrong-unit.json", dimension_error), self.cases)

    def test_repeated_measurements_cannot_be_hidden_by_rounding_expected_values(self):
        repeated = [speed_case(1,3.6), speed_case(1,3.6000000005)]
        with self.assertRaises(ValueError):
            author_tool.checked_definition(self.original, self.write("repeated.json", repeated))

    def test_ambiguous_json_is_rejected_before_publication(self):
        raw = json.dumps(self.first).replace('"field": "speed"', '"field": "x", "field": "speed"')
        self.original.write_text(raw, encoding="utf-8")
        with self.assertRaises(ValueError):
            self.change("publish", self.original, "absent")
        self.assertFalse(self.active.exists())

    def test_predicate_requires_both_outcomes_including_correct_boundary(self):
        comparison = dict(self.first, comparison={"op":"ge","threshold":0})
        path = self.write("threshold.json", comparison)
        below = speed_case(-1,-3.6,matches=False)
        at = speed_case(0,0,matches=True)
        above = speed_case(1,3.6,matches=True)
        _, checked = author_tool.checked_definition(path, self.write("all.json",[below, at, above]))
        self.assertEqual(checked["case_count"], 3)
        for invalid in ([at, above], [below, speed_case(0,0,matches=False), above]):
            with self.subTest(cases=invalid), self.assertRaises(ValueError):
                author_tool.checked_definition(path, self.write("invalid.json", invalid))

    def test_two_publishers_cannot_both_claim_absence(self):
        start = threading.Barrier(2)
        def publish(path):
            args = [sys.executable, str(AGENT/"author_tool.py"), "publish", str(path),
                    "--cases", str(self.cases), "--registry", str(self.registry), "--expect", "absent"]
            start.wait(timeout=10)
            return subprocess.run(args, capture_output=True, text=True, timeout=25)

        with ThreadPoolExecutor(max_workers=2) as pool:
            processes = list(pool.map(publish, [self.original, self.updated]))
        self.assertEqual(sorted(p.returncode for p in processes), [0,1], [p.stderr for p in processes])
        success = next(p for p in processes if p.returncode == 0)
        conflict = next(p for p in processes if p.returncode != 0)
        self.assertIn("registry changed", conflict.stderr)
        receipt = json.loads(success.stdout)
        self.assertEqual(receipt["previous"], "absent")
        self.assertEqual(load_directory(self.registry)[self.name]["version"], receipt["version"])
        history = self.registry/".history"/self.name
        self.assertEqual([p.stem for p in history.glob("*.json")], [receipt["version"]])

    def test_stale_version_cannot_overwrite_or_remove_and_history_restores_original(self):
        v1 = self.change("publish", self.original, "absent")["version"]
        v2 = self.change("publish", self.updated, v1)["version"]
        self.assertNotEqual(v1, v2)
        for action in ("publish", "remove"):
            with self.subTest(action=action), self.assertRaisesRegex(ValueError, "registry changed"):
                self.change(action, self.original, v1)
        self.assertEqual(load_directory(self.registry)[self.name]["version"], v2)
        archived = self.registry/".history"/self.name/(v1+".json")
        rollback = self.change("publish", archived, v2)
        self.assertEqual((rollback["previous"], rollback["version"]), (v2, v1))
        self.assertEqual(json.loads(self.active.read_bytes()), self.first)
        self.change("remove", self.original, v1)
        self.assertEqual(load_directory(self.registry), {})
        self.assertTrue(archived.is_file())

    def test_publication_failure_keeps_previous_definition_live(self):
        self.change("publish", self.original, "absent")
        initial = self.active.read_bytes()
        v1 = capability(self.first)["version"]
        replace = author_tool.os.replace
        def fail_active_replace(source, destination):
            if destination == self.active:
                raise OSError("simulated activation failure")
            return replace(source, destination)

        with patch.object(author_tool.os, "replace", side_effect=fail_active_replace):
            with self.assertRaisesRegex(OSError, "simulated activation failure"):
                self.change("publish", self.updated, v1)
        self.assertEqual(self.active.read_bytes(), initial)
        self.assertEqual(load_directory(self.registry)[self.name]["version"], v1)
        self.assertEqual(list(self.registry.rglob(".staging-*")), [])


if __name__ == "__main__":
    unittest.main()
