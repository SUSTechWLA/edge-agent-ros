"""Freeze verifier bindings, persist candidates and advance independently verified steps."""
from edge_contracts.errors import OutcomeUnknown, Refused, failure


def verification_plan(cap, policy, requirements):
    required = requirements(cap, policy)
    result = {"required":required}
    if required:
        result.update(outcomes=dict(cap.get("outcomes", {})), pins=[], decisions=[])
    return result


def prepare_verification(step, cap, services):
    if "verification" not in step:
        check = verification_plan(cap, services.load_policy(), services.requirements)
        if check["required"] and (step["status"] != "PENDING" or step.get("operation_id")):
            raise failure(OutcomeUnknown, "旧操作已派发且没有验收绑定，请按原操作 ID 核查",
                          "verification.legacy_unbound", "registry")
        step["verification"] = check
    check = step["verification"]
    if check["required"] and not check["pins"]:
        directory = services.snapshot()
        check["pins"] = [directory.pin(name) for name in check["required"]]


def save_candidate(journal, task, step, op, candidate, receipt):
    step["verification"]["candidate"] = candidate
    step["status"] = "VERIFYING"
    task["retries"] = 0
    journal.save(task, "RUNNING", "已保存工具候选结果，等待独立验收", kind="verification_pending",
                 op=op, tool_response=receipt)


def verify_candidate(journal, task, step, op, trace, services):
    check = step["verification"]
    # Completed decisions were committed separately; resume at the first outstanding service.
    for pin in check["pins"][len(check["decisions"]):]:
        services.snapshot().pin(pin["service"], expected_binding=pin["binding"])
        request = {"operation_id":op, "task_id":task["id"], "trace_id":task["trace_id"],
                   "tool":step["tool"], "arguments":step["args"], "outcomes":check["outcomes"],
                   "evidence":check["candidate"], "started_at":step["started_at"],
                   **{k:pin[k] for k in ("service","plugin_id","plugin_version")}}
        decision = services.verify(pin, request, trace)
        if decision["verdict"] == "fail":
            check["decisions"].append(decision)
            raise failure(Refused, "独立验收发现反证：" + pin["service"],
                          "verification.rejected", pin["plugin_id"], decision["evidence"])
        check["decisions"].append(decision)
        task["retries"] = 0
        journal.save(task, "RUNNING", "已记录独立验收：" + pin["service"], kind="verification_passed", op=op)
    return check["candidate"]


def finish_step(journal, task, step, op, result):
    step.update(status="SUCCEEDED", evidence=result)
    step["verification"].pop("candidate", None)
    task["cursor"] += 1
    task["retries"] = 0
    last = task["cursor"] == len(task["steps"])
    if last and "feedback" in step:
        goal = step["feedback"]["goal"]
        if step["tool"] != goal["tool"] or not isinstance(result, dict) or result.get(goal["field"]) != goal["equals"]:
            journal.save(task, "PLANNING", "已取得现场反馈，重新选择下一项能力", kind="verified", op=op)
            return
    journal.save(task, "SUCCEEDED" if last else "RUNNING", "已取得步骤证据", kind="verified", op=op)
