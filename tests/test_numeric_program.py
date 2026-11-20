import math
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent import numeric_program as program


class NumericProgramTest(unittest.TestCase):
    def test_author_reply_converts_to_the_existing_program_identity(self):
        instructions = ["max", "min", "sub"]
        stored = {"tokens": instructions}
        self.assertEqual(program.AUTHOR_SCHEMA["required"], ["instructions"])
        self.assertEqual(program.digest(program.from_author({"instructions": instructions})), program.digest(stored))
        for invalid in (stored, {"instructions": instructions, "other": []}, {"instructions": []}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                program.from_author(invalid)

    def test_ordered_operands_and_reusable_versions(self):
        definition = {"tokens": ["last", "first", "sub", 2, "div"]}
        integer_receipt = program.execute(definition, [4, -8, 11])
        self.assertEqual(integer_receipt["value"], 3.5)
        float_receipt = program.execute({"tokens": ["last", "first", "sub", 2., "div"]}, [4., -8., 11.])
        self.assertEqual(integer_receipt, float_receipt)
        reverse = program.execute(definition, [11, -8, 4])
        self.assertEqual(reverse["value"], -3.5)
        self.assertEqual(reverse["program_sha256"], integer_receipt["program_sha256"])
        self.assertNotEqual(reverse["input_sha256"], integer_receipt["input_sha256"])
        self.assertNotEqual(program.digest({"tokens": ["median"]}), integer_receipt["program_sha256"])

    def test_verified_blind_samples_reject_incorrect_candidate(self):
        cases = [{"sample": [3, 7, 5], "expected": 4},
                 {"sample": [-10, -2, -3.5], "expected": 8},
                 {"sample": [0, 0], "expected": 0}]
        candidate = {"tokens": ["max", "min", "sub"]}
        proof = program.verify(candidate, cases)
        self.assertEqual(proof["program_sha256"], program.digest(candidate))
        self.assertEqual(proof["case_count"], 3)
        self.assertEqual(len(proof["cases_sha256"]), 64)
        self.assertNotEqual(proof["cases_sha256"], program.verify(candidate, cases[:2])["cases_sha256"])
        with self.assertRaises(ValueError):
            program.verify({"tokens": ["last", "first", "sub"]}, cases)
        for fixtures in ([cases[0]], [cases[0], cases[0]], [cases[0], {"sample": [4, 8], "expected": 4}],
                         [cases[0], {"sample": [-3], "expected": True}], [cases[0], {"sample": [1]}]):
            with self.subTest(fixtures=fixtures), self.assertRaises(ValueError):
                program.verify(candidate, fixtures)

    def test_language_domain_and_numeric_bounds(self):
        self.assertEqual(program.execute({"tokens": ["median", "abs", "sqrt", "neg"]}, [-5, -3])["value"], -2)
        self.assertEqual(program.execute({"tokens": ["sum", "count", "div", "mean", "sub"]}, [1, 3])["value"], 0)
        for tokens in ([], ["min", "max"], [1, 2, "add"], ["min", "add"], ["abs"], ["sum", "exec"],
                       ["min", {}], [True], ["mean", float("nan"), "mul"], ["max"] * 33, ["mean", "0", "add"]):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                program.validate({"tokens": tokens})
        for sample in ([], [True], [None], [float("nan")], [math.inf], [10**1000], ["1"], [1] * 129, "[1]"):
            with self.subTest(sample=sample), self.assertRaises(ValueError):
                program.execute({"tokens": ["min"]}, sample)
        for tokens, values in ((["min", "sqrt"], [-1]), (["mean", "first", "div"], [0, 1]),
                               (["first", "last", "mul"], [1e12, 1e12])):
            with self.subTest(tokens=tokens), self.assertRaises(ValueError):
                program.execute({"tokens": tokens}, values)
        with self.assertRaises(ValueError):
            program.validate({"tokens": ["min"], "source": "/dev/camera"})


if __name__ == "__main__":
    unittest.main()
