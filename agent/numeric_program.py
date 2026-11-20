"""Versioned, deterministic scalar programs over a caller-owned measurement vector."""
import hashlib
import json
import math
import operator
from statistics import median

if __package__:
    from .series_tools import REDUCERS
else:
    from series_tools import REDUCERS


LANGUAGE = "numeric.rpn.v1"
MAX_TOKENS = 32
MAX_SAMPLES = 128
MAX_INPUT = 10**12
MAX_RESULT = 10**18
READERS = {**REDUCERS, "median": median, "first": lambda x: x[0], "last": lambda x: x[-1]}
UNARY = {"abs": abs, "neg": operator.neg, "sqrt": math.sqrt}
BINARY = {"add": operator.add, "sub": operator.sub, "mul": operator.mul, "div": operator.truediv}
AUTHOR_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["tokens"],
    "properties": {"tokens": {"type": "array", "minItems": 0, "maxItems": MAX_TOKENS,
                             "items": {"anyOf": [
                                 {"type": "string", "enum": [*READERS, *UNARY, *BINARY]},
                                 {"type": "number", "minimum": -MAX_INPUT, "maximum": MAX_INPUT},
                             ]}}},
}
PROMPT = (
    "Write a reusable numeric.rpn.v1 program for the requested scalar calculation. "
    "Return JSON with only a tokens array. Use the shortest correct postfix expression. "
    "Each reader pushes one scalar from the entire ordered input vector: min, max, sum, count, "
    "mean (arithmetic mean), median, first, last. A numeric literal pushes that constant. "
    "abs, neg, sqrt transform the top scalar. add, sub, mul, div consume the two top scalars "
    "and push their result, with the earlier operand on the left. End with exactly one scalar. "
    "Preserve the requested operation, ordering and units. If this language cannot express the "
    "calculation, return an empty tokens array. Never invent sample values."
)


def _hash(value):
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _number(value, limit):
    if type(value) not in (int, float) or not -limit <= value <= limit:
        raise ValueError("expected a bounded finite number")
    return float(value) if value else 0.0


def validate(candidate):
    if not isinstance(candidate, dict) or set(candidate) != {"tokens"}:
        raise ValueError("expected a numeric program with exactly one tokens field")
    raw = candidate["tokens"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_TOKENS:
        raise ValueError("expected 1–32 program tokens")
    tokens = []
    depth = 0
    reads_input = False
    for token in raw:
        if type(token) in (int, float):
            token = _number(token, MAX_INPUT)
            depth += 1
        elif not isinstance(token, str):
            raise ValueError("invalid numeric program token type")
        elif token in READERS:
            depth += 1
            reads_input = True
        elif token in UNARY and depth >= 1:
            pass
        elif token in BINARY and depth >= 2:
            depth -= 1
        else:
            raise ValueError("unknown operation or missing operand")
        tokens.append(token)
    if depth != 1 or not reads_input:
        raise ValueError("program must read its input and return exactly one scalar")
    return {"tokens": tokens}


def digest(candidate):
    return _hash({"language": LANGUAGE, **validate(candidate)})


def execute(candidate, values):
    program = validate(candidate)
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_SAMPLES:
        raise ValueError("expected 1–128 measurements")
    samples = [_number(value, MAX_INPUT) for value in values]
    stack = []
    for token in program["tokens"]:
        if type(token) is float:
            result = token
        elif token in READERS:
            result = READERS[token](samples)
        elif token in UNARY:
            result = UNARY[token](stack.pop())
        else:
            right = stack.pop()
            if token == "div" and right == 0:
                raise ValueError("numeric program divided by zero")
            result = BINARY[token](stack.pop(), right)
        stack.append(_number(result, MAX_RESULT))
    return {"language": LANGUAGE, "value": stack[0], "input_count": len(samples),
            "input_sha256": _hash(samples), "program_sha256": digest(program)}


def verify(candidate, cases):
    """Check independent input/expected pairs supplied by the deployment or its reviewer."""
    program = validate(candidate)
    if not isinstance(cases, list) or not 2 <= len(cases) <= 64:
        raise ValueError("provide 2–64 independent numeric cases")
    seen_inputs = set()
    expected_values = set()
    observations = []
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict) or set(case) != {"sample", "expected"}:
            raise ValueError("each independent case requires sample and expected")
        expected = _number(case["expected"], MAX_RESULT)
        receipt = execute(program, case["sample"])
        if receipt["input_sha256"] in seen_inputs:
            raise ValueError("repeated verification sample")
        if not math.isclose(receipt["value"], expected, rel_tol=1e-9, abs_tol=1e-12):
            raise ValueError(f"numeric program failed independent case {index}")
        seen_inputs.add(receipt["input_sha256"])
        expected_values.add(expected)
        observations.append({"input_sha256": receipt["input_sha256"], "expected": expected})
    if len(expected_values) < 2:
        raise ValueError("verification needs distinct expected values")
    return {"language": LANGUAGE, "program_sha256": digest(program), "case_count": len(cases),
            "cases_sha256": _hash(observations)}
