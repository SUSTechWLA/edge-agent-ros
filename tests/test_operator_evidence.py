"""Offline verification uses the exported qualification policy and the exact measured inputs."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

try:
    from task_recorder.operator_evidence import checksum, evaluate, normalized, qualified_receipt, validation
except ModuleNotFoundError as exc:
    if exc.name != "PIL":
        raise
    evaluate = None


@unittest.skipUnless(evaluate, "Pillow is required by the recorder")
class OperatorEvidenceTest(unittest.TestCase):
    def test_model_instruction_values_match_the_content_addressed_form(self):
        self.assertEqual(normalized({"instructions":["max","min","add",2,"div"]}),
                         {"tokens":["max","min","add",2.0,"div"]})
        self.assertEqual(evaluate({"instructions":["last","first","sub"]}, [4, 2, 1]), -3)
        with self.assertRaisesRegex(ValueError, "invalid operator or stack"):
            evaluate({"tokens":["min","sub"]}, [1, 2])

    def test_rejects_wrong_policy_receipt_and_substituted_sensor_values(self):
        with TemporaryDirectory() as tmp:
            file, policy = (Path(tmp)/name for name in ("operator.json", "policy.json"))
            program = {"tokens":["max", "min", "sub"]}
            cases = [{"sample":[0,1],"expected":1}, {"sample":[3,3],"expected":0}]
            proof = validation(program, cases)
            ref = "sha256:" + proof["program_sha256"]
            who = dict(operator_digest=ref, program=program, contract="range", validation=proof)
            file.write_text(json.dumps({**who, "language":"numeric.rpn.v1"}))
            policy.write_text(json.dumps({"contracts":{"range":{"description":"range","cases":cases}}}))
            sample = [3.1, 4.1, 1.2]
            evidence = dict(operator_policy_digest="sha256:" + hashlib.sha256(policy.read_bytes()).hexdigest(),
                            analysis=dict(regions=[{"value":v} for v in sample], unit="m"),
                            derived=dict(**who, unit="m", input_values=list(sample), input_sha256=checksum(sample),
                                         value=4.1 - 1.2))
            result = qualified_receipt(evidence, file, policy, ref, "range")
            self.assertAlmostEqual(result["value"], 2.9)
            evidence["derived"]["input_values"][0] = 1.2
            with self.assertRaisesRegex(ValueError, "pixel measurement"):
                qualified_receipt(evidence, file, policy, ref, "range")
            evidence["derived"]["input_values"] = sample
            policy.write_text(policy.read_text() + " ")
            with self.assertRaisesRegex(ValueError, "independent cases"):
                qualified_receipt(evidence, file, policy, ref, "range")
