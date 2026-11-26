"""The numerical runtime independently checks content, qualification and the actual measurements."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent import numeric_program
from agent.numeric_server import Evaluator


class OperatorServiceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Path(temp.name)
        policy = ROOT/"profiles/region-operator-contracts.json"
        self.ev = Evaluator(self.store, policy, "sha256:"+"a"*64)
        program = {"tokens":["max", "min", "sub"]}
        proof = numeric_program.verify(program, json.loads(policy.read_bytes())["contracts"]["regional_range"]["cases"])
        self.ref = "sha256:"+numeric_program.digest(program)
        self.file = self.store/(self.ref[7:]+".json")
        self.record = {"operator_digest":self.ref, "contract":"regional_range", "validation":proof, "program":program}
        self.file.write_text(json.dumps(self.record))

    def test_recompute_and_reject_tampering(self):
        call = {"operator_digest":self.ref, "values":[.2, -.7, .1]}
        value = self.ev.evaluate(call)["derived"]
        self.assertAlmostEqual(value["value"], .9)
        self.assertEqual(value["input_values"], call["values"])
        for mod in ({"validation":{}}, {"contract":"unapproved"}, {"program":{"tokens":["min"]}}):
            self.file.write_text(json.dumps({**self.record, **mod}))
            with self.assertRaises(ValueError):
                self.ev.evaluate(call)

    def test_reject_invalid_identifiers_nonfinite_and_oversize(self):
        for ref, seq in (("../path", [4]), (None, [1]), (self.ref, []), (self.ref, [False]),
                         (self.ref, [float("nan")]), (self.ref,[1]*33)):
            with self.assertRaises(ValueError):
                self.ev.evaluate({"operator_digest":ref, "values":seq})


if __name__ == "__main__":
    unittest.main()
