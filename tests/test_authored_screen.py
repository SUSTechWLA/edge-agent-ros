"""Public DSH tool path and independent qualification for authored read-only screens."""
import asyncio
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from perception.spatial_facts import authored_screen


PROGRAM = {"statistic": "p10", "unknown_if": ["valid_fraction", 0.6, "lt"],
           "near_if": ["depth_m", "distance_m", "le"], "review_order": "nearest_first"}


class Context:
    def __init__(self, program, responses):
        self.program = program
        self.responses = iter(responses)
        self.calls = []
        self.services = []
        self.tools = self
        self.registry = {}

    def config(self):
        return {"profile": {"legacy": {"id": "review_depth"}, "program": self.program},
                "settings": {"actor_ref": "robot"}}

    def require(self, name):
        self.services.append(name)

        async def call(args):
            self.calls.append(args)
            return next(self.responses)(args)
        return call

    def register(self, name, callback):
        self.registry[name] = callback


def sample(values):
    def reply(args):
        height = 10
        width = 10 * len(values) + 1
        regions = []
        for i, (fraction, value) in enumerate(values):
            x0, x1 = i*width // len(values), (i+1)*width // len(values)
            pixels = (x1-x0)*height
            regions.append({"value": value, "valid_pixels": int(fraction*pixels), "pixels": pixels,
                            "bounds_xyxy": [x0, 0, x1, height]})
        return {"ok": True, "value": {"analysis": {
            "width": width, "height": height, "channel": "depth", "unit": "m",
            "actor_ref": args["actor_ref"], "program": args["program"], "statistic": "quantile(0.1)",
            "grid": [1, len(values)], "regions": regions, "depth_ref": "sha256:"+"1"*64,
        }, "observations": {"as_of_ns": 1770000000000000000}}}
    return reply


def run(ctx, cols=3, distance=1.0):
    return asyncio.run(ctx.registry["review_depth"](
        {"operation_id": "real-op", "arguments": {"columns": cols, "distance_m": distance}}))


class AuthoredScreenTests(unittest.TestCase):
    def test_site_holdouts_and_equivalent_reverse_operands(self):
        other = {**PROGRAM, "near_if": ["distance_m", "depth_m", "ge"], "unknown_if": [0.6, "valid_fraction", "gt"]}
        for prog in (PROGRAM, other):
            result = authored_screen.qualify(prog)
            canonical = json.dumps(prog, sort_keys=True, separators=(",", ":")).encode()
            self.assertEqual(result["program_sha256"], "sha256:"+sha256(canonical).hexdigest())
            self.assertEqual(result["case_count"], 5)
            self.assertTrue(all(x["passed"] for x in result["cases"]))

    def test_legal_but_incorrect_programs_fail_acceptance_and_loading(self):
        changes = [{"near_if": ["depth_m", "distance_m", "lt"]}, {"unknown_if": ["valid_fraction", 0.6, "le"]},
                   {"unknown_if": ["valid_fraction", 0.5, "lt"]}, {"statistic": "p25"}, {"statistic": "median"},
                   {"review_order": "farthest_first"}, {"review_order": "image_order"}]
        for change in changes:
            with self.subTest(change=change):
                prog = authored_screen.validate_program({**PROGRAM, **change})
                ctx = Context(prog, [])
                with self.assertRaisesRegex(ValueError, "qualification_failed"):
                    authored_screen.apply(ctx)
                self.assertEqual(ctx.registry, {})
                self.assertEqual(ctx.services, [])

    def test_program_rejects_arbitrary_inputs(self):
        invalid = [None, {}, {**PROGRAM, "path": "arbitrary"}, {**PROGRAM, "review_order": ["image_order"]},
                   {**PROGRAM, "unknown_if": ["valid_fraction", float("nan"), "lt"]},
                   {**PROGRAM, "unknown_if": ["valid_fraction", True, "lt"]},
                   {**PROGRAM, "unknown_if": ["valid_fraction", "0.6", "lt"]},
                   {**PROGRAM, "near_if": ["depth_m", "depth_m", "lt"]},
                   {**PROGRAM, "near_if": ["depth_m", "distance_m", "eval"]},
                   {**PROGRAM, "unknown_if": ["depth_m", 0.6, "lt"]}]
        for prog in invalid:
            with self.subTest(program=prog), self.assertRaises(ValueError):
                authored_screen.validate_program(prog)

    def test_single_measurement_per_call_and_reuse_with_new_args(self):
        ctx = Context(PROGRAM, [sample([(1., .97), (.5, .25), (1., .76)]),
                                sample([(1., .65), (0., None), (.6, .7), (1., 1.4)])])
        authored_screen.apply(ctx)
        first, second = run(ctx), run(ctx, cols=4, distance=.7)
        for result in (first, second):
            self.assertEqual((result["operation_id"], result["state"]), ("real-op", "SUCCEEDED"))
            self.assertIn("depth_ref", result["evidence"]["analysis"])
            self.assertEqual(result["evidence"]["observations"]["as_of_ns"], "1770000000000000000")
        a, b = (result["evidence"]["review"] for result in (first, second))
        self.assertEqual(a["review_order"], [3, 1])
        self.assertEqual(b["review_order"], [1, 3])
        self.assertEqual(a["unknown_columns"], [2])
        self.assertEqual([v["state"] for v in b["regions"]], ["near_surface","unknown","near_surface","above_threshold"])
        self.assertEqual(a["program_sha256"], b["program_sha256"])
        self.assertEqual(ctx.services, ["perception.frame_statistics"])
        self.assertEqual(ctx.calls, [{"actor_ref": "robot", "program": f"depth | grid(1,{c}) | quantile(0.1)"}
                                     for c in (3, 4)])

    def test_invalid_call_cannot_reach_source(self):
        ctx = Context(PROGRAM, [])
        authored_screen.apply(ctx)
        for cols, distance in [(9, 1), (True, 1), (1.0, 1), (1, 0), (1, float("inf")), (1, False), (0, .5)]:
            with self.subTest(cols=cols, distance=distance):
                self.assertEqual(run(ctx, cols, distance)["error"]["code"], "screen.invalid_arguments")
        self.assertEqual(ctx.calls, [])

    def test_millimetre_conversion_preserves_inclusive_threshold(self):
        ctx = Context(PROGRAM, [sample([(1., 947*.001), (1., 948*.001)])])
        authored_screen.apply(ctx)
        self.assertEqual(run(ctx, 2, .947)["evidence"]["review"]["review_order"], [1])

    def test_bad_measurements_and_missing_sources_fail_closed(self):
        for change in ("channel", "geometry", "bad_number", "no_number", "bad_count"):
            def source(args):
                response = sample([(1., .4)])(args)
                a = response["value"]["analysis"]
                row = a["regions"][0]
                if change == "channel":
                    a["channel"] = "brightness"
                elif change == "geometry":
                    row["bounds_xyxy"] = [0,0,0,0]
                elif change == "bad_number":
                    row["value"] = float("nan")
                elif change == "no_number":
                    row["value"] = None
                else:
                    row["valid_pixels"] = 999
                return response
            with self.subTest(change=change):
                ctx = Context(deepcopy(PROGRAM), [source])
                authored_screen.apply(ctx)
                self.assertEqual(run(ctx, 1)["state"], "BLOCKED")
        ctx = Context(PROGRAM, [lambda _: {"ok": False, "code": "grounding.unsynchronized_input"}])
        authored_screen.apply(ctx)
        self.assertEqual(run(ctx)["error"]["code"], "grounding.unsynchronized_input")

    def test_entire_frame_invalid_stays_unknown_without_claiming_measurement(self):
        ctx = Context(PROGRAM, [lambda _: {"ok": False, "code": "query.no_valid_pixels"}])
        authored_screen.apply(ctx)
        result = run(ctx, 4)
        self.assertEqual(result["state"], "BLOCKED")
        self.assertEqual(result["evidence"]["review"]["unknown_columns"], [1, 2, 3, 4])
        self.assertEqual(result["evidence"]["review"]["review_order"], [])


if __name__ == "__main__":
    unittest.main()

