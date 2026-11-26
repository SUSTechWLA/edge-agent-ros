"""Interpreter and independent site checks for a model-authored, read-only depth screen.

The authored program selects a measurement, compiles two comparison expressions and
orders measured candidates. The host supplies one fixed source of calibrated regional
statistics. Programs have no service names, executable text, loops or device handles.
"""
import hashlib
import json
import math
import operator

from navigation.tools import wire_times


STATISTICS = {"p10": "quantile(0.1)", "p25": "quantile(0.25)", "median": "median"}
ORDERS = ("nearest_first", "farthest_first", "image_order")
COMPARISONS = {"lt": operator.lt, "le": operator.le, "gt": operator.gt, "ge": operator.ge}
FRACTIONS = (0.5, 0.6, 0.75, 0.8)


def expression_schema(fields, constants=()):
    choices = list(fields) + list(COMPARISONS) + list(constants)
    return {"type": "array", "minItems": 3, "maxItems": 3, "items": {"enum": choices}}


PROGRAM_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["statistic", "unknown_if", "near_if", "review_order"],
    "properties": {
        "statistic": {"type": "string", "enum": list(STATISTICS)},
        "unknown_if": expression_schema(["valid_fraction"], FRACTIONS),
        "near_if": expression_schema(["depth_m", "distance_m"]),
        "review_order": {"type": "string", "enum": list(ORDERS)},
    },
}
CALL_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["columns", "distance_m"],
    "properties": {
        "columns": {"type": "integer", "minimum": 1, "maximum": 8,
                    "description": "Number of equal vertical columns, in left-to-right order."},
        "distance_m": {"type": "number", "minimum": 0.1, "maximum": 8,
                       "description": "Inclusive optical-axis depth threshold in metres."},
    },
}
AUTHOR_INSTRUCTIONS = (
    "Write a small reusable depth-screen program. The input is one stationary camera snapshot. "
    "Use the lower decile of the valid depths in each requested column. With less than 60 percent "
    "valid pixels, classify the observation as unknown. At or below the caller's distance threshold "
    "mark the measured surface for human review. Rank marked columns from nearest to farthest. "
    "statistic chooses the measurement. unknown_if and near_if are postfix expressions, exactly "
    "[left operand, right operand, comparison]. Comparisons are lt, le, gt or ge. "
    "In unknown_if compare valid_fraction (0 to 1) and a numeric fraction literal. "
    "In near_if compare depth_m (measured) and distance_m (the caller's limit). "
    "review_order defines the order of candidates. Do not add task-specific distances or columns."
)


def check(condition, code):
    if not condition:
        raise ValueError(code)


def finite(value, low, high):
    return type(value) in (int, float) and low <= value <= high


def validate_program(program):
    check(type(program) is dict and set(program) == set(PROGRAM_SCHEMA["required"]), "screen.invalid_program")
    stat, order = program["statistic"], program["review_order"]
    check(type(stat) is str and stat in STATISTICS and type(order) is str and order in ORDERS,
          "screen.invalid_measurement_or_order")
    for key in ("unknown_if", "near_if"):
        expr = program[key]
        check(type(expr) is list and len(expr) == 3, "screen.invalid_expression")
        left, right, op = expr
        check(type(op) is str and op in COMPARISONS, "screen.invalid_comparison")
        if key == "unknown_if":
            named, literal = (left, right) if type(left) is str else (right, left)
            check(named == "valid_fraction" and type(literal) in (int, float) and literal in FRACTIONS,
                  "screen.invalid_coverage_operands")
        else:
            check(type(left) is str and type(right) is str and {left, right} == {"depth_m", "distance_m"},
                  "screen.invalid_depth_operands")
    return {"statistic": stat, "unknown_if": list(program["unknown_if"]),
            "near_if": list(program["near_if"]), "review_order": order}


def digest(program):
    data = json.dumps(program, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return "sha256:" + hashlib.sha256(data).hexdigest()


def predicate(expression, fields):
    left, right, op = expression
    a, b = fields.get(left, left), fields.get(right, right)
    # Millimetres converted to metres can land one ULP above an exact user threshold.
    if math.isclose(a, b, rel_tol=0., abs_tol=1e-12):
        a = b
    return COMPARISONS[op](a, b)


def screen(program, regions, distance):
    observations = []
    candidates = []
    unknown = []
    for column, region in enumerate(regions, start=1):
        count, total, depth = region["valid_pixels"], region["pixels"], region["value"]
        check(type(total) is int and type(count) is int and total > 0 and 0 <= count <= total,
              "screen.invalid_pixel_counts")
        check((count == 0 and depth is None) or (count > 0 and finite(depth, 0, 1e6) and depth > 0),
              "screen.invalid_depth")
        fraction = count / total
        if depth is None or predicate(program["unknown_if"], {"valid_fraction": fraction}):
            state = "unknown"
            unknown.append(column)
        elif predicate(program["near_if"], {"depth_m": depth, "distance_m": distance}):
            state = "near_surface"
            candidates.append((column, depth))
        else:
            state = "above_threshold"
        observations.append({"column": column, "state": state, "depth_m": depth, "valid_fraction": fraction,
                             "valid_pixels": count, "pixels": total})
    if program["review_order"] != "image_order":
        candidates.sort(key=lambda candidate: candidate[1], reverse=program["review_order"] == "farthest_first")
    return {"regions": observations, "review_order": [column for column, _ in candidates], "unknown_columns": unknown,
            "distance_m": distance, "statistic": program["statistic"], "column_count": len(observations),
            "scope": "camera_optical_depth_only"}


# Fixed independently labelled station fixtures. Each tuple gives the already measured
# p10/p25/median depths (m) and valid pixels per 100. No test data goes in author prompts.
# Rank is one-based and stable in image order for equal depths.
CASES = (
    ("different_reducers", 1.0, [(0.8, 1.3, 1.6, 100), (1.2, 1.4, 1.7, 100)],
     ["near_surface", "above_threshold"], [1]),
    ("inclusive_depth", 0.7, [(0.7, 0.7, 0.7, 100), (0.71, 0.71, 0.71, 100)],
     ["near_surface", "above_threshold"], [1]),
    ("coverage_boundary", 2.0, [(0.2, 0.2, 0.2, 59), (1.9, 1.9, 1.9, 60)],
     ["unknown", "near_surface"], [2]),
    ("missing_and_distant", 1.1, [(None, None, None, 0), (2.2, 2.2, 2.2, 100)],
     ["unknown", "above_threshold"], []),
    ("nearest_review_with_ties", 1.6, [(1.5, 1.5, 1.5, 100), (0.8, 0.8, 0.8, 100), (0.8, 0.8, 0.8, 100)],
     ["near_surface", "near_surface", "near_surface"], [2, 3, 1]),
)


def qualify(program):
    program = validate_program(program)
    index = ("p10", "p25", "median").index(program["statistic"])
    receipts = []
    for name, distance, columns, expected_states, expected_order in CASES:
        regions = [{"pixels": 100, "valid_pixels": values[3], "value": values[index]} for values in columns]
        review = screen(program, regions, distance)
        states = [r["state"] for r in review["regions"]]
        check(states == expected_states and review["review_order"] == expected_order,
              "screen.qualification_failed:" + name)
        receipts.append({"case": name, "states": states, "review_order": review["review_order"], "passed": True})
    return {"program_sha256": digest(program), "case_count": len(receipts), "cases": receipts}


def validate_analysis(analysis, actor, query, columns, statistic):
    check(type(analysis) is dict and analysis.get("program") == query and analysis.get("actor_ref") == actor
          and analysis.get("statistic") == statistic and analysis.get("channel") == "depth"
          and analysis.get("unit") == "m" and analysis.get("grid") == [1, columns], "screen.analysis_mismatch")
    width, height = analysis.get("width"), analysis.get("height")
    regions = analysis.get("regions")
    check(type(width) is int and columns <= width <= 8192 and type(height) is int and 1 <= height <= 8192
          and type(regions) is list and len(regions) == columns, "screen.invalid_geometry")
    for i, region in enumerate(regions):
        x0, x1 = i * width // columns, (i + 1) * width // columns
        check(type(region) is dict and region.get("bounds_xyxy") == [x0, 0, x1, height]
              and region.get("pixels") == (x1-x0)*height and "valid_pixels" in region and "value" in region,
              "screen.invalid_region")


def apply(ctx):
    config = ctx.config()
    program = validate_program(config["profile"]["program"])
    proof = qualify(program)
    actor = config["settings"]["actor_ref"]
    stats = ctx.require("perception.frame_statistics")

    async def invoke(wire):
        def blocked(reason):
            return {"operation_id": wire["operation_id"], "state": "BLOCKED",
                    "error": {"code": reason, "message": reason}}

        args = wire.get("arguments")
        if type(args) is not dict or set(args) != {"columns", "distance_m"}:
            return blocked("screen.invalid_arguments")
        columns, distance = args["columns"], args["distance_m"]
        if type(columns) is not int or not 1 <= columns <= 8 or not finite(distance, 0.1, 8):
            return blocked("screen.invalid_arguments")
        statistic = STATISTICS[program["statistic"]]
        query = f"depth | grid(1,{columns}) | {statistic}"
        response = await stats({"actor_ref": actor, "program": query})
        if not response["ok"]:
            failure = blocked(response["code"])
            if response["code"] == "query.no_valid_pixels":
                # The source returns no calibrated measurement artifact when every pixel is invalid.
                failure["evidence"] = {"review": {"reason": response["code"], "unknown_columns": list(range(1, columns+1)),
                                                  "review_order": [], "distance_m": distance}}
            return failure
        facts = response["value"]
        analysis = facts.get("analysis")
        try:
            validate_analysis(analysis, actor, query, columns, statistic)
            review = screen(program, analysis["regions"], distance)
        except ValueError as exc:
            return blocked(str(exc))
        return {"operation_id": wire["operation_id"], "state": "SUCCEEDED",
                "evidence": wire_times({**facts, "review": {**review, "program_sha256": proof["program_sha256"]}})}

    ctx.tools.register(config["profile"]["legacy"]["id"], invoke)
