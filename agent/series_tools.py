"""Bounded, deterministic operations on a JSON integer array supplied by the caller."""
import hashlib
import json
import re


TRANSFORMS = {
    "diff": lambda values: [b - a for a, b in zip(values, values[1:])],
    "abs": lambda values: [abs(value) for value in values],
    "sort": sorted,
    "unique": lambda values: list(dict.fromkeys(values)),
}
REDUCERS = {"min": min, "max": max, "sum": sum, "count": len, "mean": lambda values: sum(values) / len(values)}
PROMPT = """用最少的操作编写一个数列计算工具。保持输入顺序和正负号。
transforms 是按顺序执行的可选步骤；仅加入用户要求的变换。diff 表示按原顺序计算相邻样本的有符号间隔
(后一个值减前一个值)。abs 只在要求绝对值时使用，sort 只在明确要求排序时使用，unique 表示去重。
reduce 是最终统计。min/max/sum/mean/count 已直接实现最小值、最大值、总和、均值、计数，
不要求预先排序。用户只要基础统计时 transforms 填空数组。无法用这些步骤完整表示时 reduce 填 unsupported。"""
AUTHOR_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["transforms", "reduce"],
    "properties": {
        "transforms": {"type": "array", "maxItems": 4, "items": {"type": "string", "enum": list(TRANSFORMS)}},
        "reduce": {"type": "string", "enum": [*REDUCERS, "unsupported"]},
    },
}


def pipeline(definition):
    stages, reducer = definition.get("transforms"), definition.get("reduce")
    if (not isinstance(stages, list) or len(stages) > 4
            or any(not isinstance(op, str) or op not in TRANSFORMS for op in stages)):
        raise ValueError("expected up to four known series transforms")
    if not isinstance(reducer, str) or reducer not in REDUCERS:
        raise ValueError("unsupported series reduction")
    return {"transforms": list(stages), "reduce": reducer}


def values_from(arguments):
    if not isinstance(arguments, dict) or set(arguments) != {"series"}:
        raise ValueError("expected exactly the series argument")
    raw = arguments["series"]
    if not isinstance(raw, str) or len(raw) > 4096 or not re.fullmatch(r"\[\s*[-0-9,\s]*\]", raw):
        raise ValueError("series must be a bounded JSON integer array")
    values = json.loads(raw)
    if not 1 <= len(values) <= 128 or any(type(n) is not int or abs(n) > 1_000_000_000 for n in values):
        raise ValueError("series needs 1–128 integers with absolute value at most 10^9")
    return values


def split_request(text):
    """Extract the original series argument and reusable capability description."""
    match = re.fullmatch(r"([^\[\]{}]+?)\s*(\[[^\[\]]*\])\s*[。.!！]?\s*", text.strip())
    if not match or not match[1].strip():
        return None
    return match[1].strip(), {"series": match[2]}


def evaluate(definition, arguments):
    values = values_from(arguments)
    canonical = json.dumps(values, separators=(",", ":")).encode("ascii")
    count = len(values)
    for op in definition["transforms"]:
        values = TRANSFORMS[op](values)
        if not values:
            raise ValueError("series transform produced an empty sequence")
    return {"source": "arguments.series", "input_sha256": hashlib.sha256(canonical).hexdigest(),
            "input_count": count, "output_count": len(values), "value": REDUCERS[definition["reduce"]](values)}
