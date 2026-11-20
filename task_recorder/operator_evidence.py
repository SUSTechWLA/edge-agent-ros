"""Independently replay content-addressed scalar programs and their author/install journal."""
import hashlib
import json
import math
import operator
from statistics import median

from .sensor_review import ns, read, require, sha


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")).hexdigest()


def norm(value):
    require(type(value) in (float, int) and math.isfinite(value), "invalid operator number")
    return float(value) if value else 0.


def normalized(program):
    require(isinstance(program, dict) and set(program) in ({"tokens"}, {"instructions"}), "invalid published program")
    code = next(iter(program.values()))
    require(isinstance(code, list) and 1 <= len(code) <= 32, "invalid published program")
    return {"tokens":[token if isinstance(token, str) else norm(token) for token in code]}


def evaluate(program, values):
    nums = list(map(norm, values))
    require(1 <= len(nums) <= 128 and all(abs(x) <= 10**12 for x in nums), "invalid operator samples")
    readers = {"min":min, "max":max, "sum":sum, "count":len, "mean":lambda v: sum(v) / len(v),
               "median":median, "first":lambda v:v[0], "last":lambda v:v[-1]}
    unary = {"neg":operator.neg, "abs":abs, "sqrt":math.sqrt}
    binary = {"add":operator.add, "sub":operator.sub, "mul":operator.mul, "div":operator.truediv}
    stack, used = [], False
    for token in normalized(program)["tokens"]:
        if not isinstance(token, str):
            require(abs(token) <= 10**12, "constant exceeds evaluator bounds")
            value = token
        elif token in readers:
            value, used = readers[token](nums), True
        elif token in unary and stack:
            value = unary[token](stack.pop())
        elif token in binary and len(stack) >= 2:
            b, a = stack.pop(), stack.pop()
            value = binary[token](a, b)
        else:
            raise ValueError("invalid operator or stack")
        require(math.isfinite(value) and abs(value) <= 10**18, "unbounded result")
        stack.append(value)
    require(used and len(stack) == 1, "operator did not compute one value from the measurements")
    return stack[0]


def validation(program, cases):
    proof = []
    for case in cases:
        value, expected = evaluate(program, case["sample"]), norm(case["expected"])
        require(math.isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12), "independent validation case failed")
        proof.append({"input_sha256":checksum(list(map(norm, case["sample"]))), "expected":expected})
    require(2 <= len(proof) <= 64 and len({c["input_sha256"] for c in proof}) == len(proof)
            and len({c["expected"] for c in proof}) >= 2, "insufficient independent cases")
    return dict(language="numeric.rpn.v1", program_sha256=checksum({"language":"numeric.rpn.v1", **normalized(program)}),
                case_count=len(proof), cases_sha256=checksum(proof))


def qualified_receipt(evidence, installed, policy, digest, contract):
    record, policy_doc = read(installed), read(policy)
    spec = policy_doc["contracts"][contract]
    derived = evidence["derived"]
    program = normalized(derived["program"] if "program" in derived else {"instructions":derived["instructions"]})
    proof = validation(program, spec["cases"])
    require(record["language"] == proof["language"] and record["operator_digest"] == digest == derived["operator_digest"]
            == "sha256:" + proof["program_sha256"] and record["program"] == program
            and derived["contract"] == record["contract"] == contract and proof == derived["validation"] == record["validation"]
            and evidence["operator_policy_digest"] == "sha256:" + sha(policy),
            "deployed definition, independent cases or software binding differ from receipt")
    values = [s["value"] for s in evidence["analysis"]["regions"]]
    result = evaluate(program, values)
    require(values == derived["input_values"] and derived["input_sha256"] == checksum(list(map(norm, values)))
            and derived["unit"] == evidence["analysis"]["unit"] and math.isclose(result, derived["value"],
            rel_tol=1e-9, abs_tol=1e-12), "derived result differs from independent pixel measurement")
    return dict(contract=contract, description=spec["description"], digest=digest, program=program,
                cases=spec["cases"], proof=proof, inputs=values, value=result, unit=derived["unit"],
                installed_file_sha256=sha(installed), policy_file_sha256=sha(policy))


def trace_operator(journal, task, context, answer, final, read_artifact):
    gap = answer["computation"]
    name, inputs = gap["contract"], gap["input"]
    read_plan = context["numeric_computation"]
    offered, = [c for c in read_plan["contracts"] if c["id"] == name]
    require(answer["steps"] == [] and final["disposition"] == "ready" and len(final["steps"]) == 1,
            "invalid split computation")
    invocation = final["steps"][0]
    digest = invocation["args"]["operator_digest"]
    require(invocation == {"tool":read_plan["runner"], "args":dict(inputs, operator_digest=digest)}
            and task["steps"][0]["read_operator"] == name, "compiled invocation differs from proposed computation")
    decomp, p = journal.one("planner.decomposed")
    require(read_artifact(p["trace"]) == answer and p["trace"]["runner"] == invocation["tool"],
            "decomposition does not match the planner response")
    resume, pres = journal.one("planner.resumed")
    require(pres["trace"]["operator_digest"] == digest and pres["trace"]["contract"] == name,
            "different calculation resumed")
    rows = []
    authors = {}
    for r,p in journal.events:
        phase, tr = r["phase"], p["trace"]
        if not phase.startswith("operator.") and tr.get("module") != "operator-author":
            continue
        row = dict(phase=phase, stamp_ns=ns(r["stamp_ns"]))
        if phase.startswith("operator."):
            require(tr["contract"] == name, "operator lifecycle belongs to another contract")
            for key in ("operator_digest", "validation", "attempt", "code"):
                if key in tr:
                    row[key] = tr[key]
            if "operator_digest" in tr:
                require(tr["operator_digest"] == digest, "inconsistent operator digest in lifecycle")
        elif phase == "model.request":
            req = read_artifact(tr)
            user, = (m for m in req["messages"] if m["role"] == "user")
            obj = json.loads(user["content"])
            require(obj == {"request":task["text"], "calculation":offered["description"]},
                    "author input differs from the stated task and public contract")
            authors[tr["call_id"]] = req["model"]
            row.update(model=req["model"], call_id=tr["call_id"], request_sha256=tr["artifact"]["sha256"])
        elif phase == "model.response":
            res = read_artifact(tr)
            require(tr["call_id"] in authors and authors[tr["call_id"]] == res["model"], "unrelated author response")
            candidate = json.loads(res["message"]["content"])
            row.update(model=res["model"], call_id=tr["call_id"], candidate=candidate,
                       response_sha256=tr["artifact"]["sha256"])
        else:
            raise ValueError("unsupported author lifecycle " + phase)
        rows.append(row)
    phases = [row["phase"] for row in rows]
    if "operator.installed" in phases:
        require(offered["installed"] is None and phases[0] == "operator.gap" and phases[-2:] ==
                ["operator.verified", "operator.installed"] and "model.response" in phases, "unproven authored release")
        mode = "installed"
    else:
        require(offered["installed"] == digest and phases == ["operator.reused"], "unproven reuse of an installed operator")
        mode = "reused"
    require(all(ns(decomp["stamp_ns"]) <= r["stamp_ns"] <= ns(resume["stamp_ns"]) for r in rows),
            "operator lifecycle outside parent task")
    return dict(contract=name, spec=offered["description"], original_inputs=inputs, runner=invocation["tool"],
                digest=digest, mode=mode, events=rows, decomposed_ns=ns(decomp["stamp_ns"]),
                resumed_ns=ns(resume["stamp_ns"]))
