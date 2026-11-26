"""Freeze verifier bindings, persist candidates and advance independently verified steps."""
from edge_contracts.descriptor import NAME, SHA
from edge_contracts.errors import OutcomeUnknown, Refused, failure


def commissioned_motion_binding(tool, cap, pin):
    """The controller owns this binding and a distinct deployed verifier."""
    if not isinstance(pin, dict) or not isinstance(cap, dict):
        return False
    return (cap.get("id") == tool == pin.get("name") and pin.get("class") == "physical"
            and cap.get("effect") == "actuation" and not cap.get("endpoint") and "_soft" not in cap
            and isinstance(cap.get("version"), str) and cap["version"] and cap["version"] == pin.get("version")
            and all(isinstance(pin.get(k), str) and NAME.fullmatch(pin[k])
                    for k in ("plugin_id", "resource_id", "provider_id", "verifier_id"))
            and pin["provider_id"] != pin["verifier_id"]
            and all(isinstance(pin.get(k), str) and SHA.fullmatch(pin[k]) for k in ("content_digest", "binding"))
            and cap.get("binding", pin["binding"]) == pin["binding"])


def has_motion_proof(step, op, evidence):
    pin = step.get("physical_pin")
    return (commissioned_motion_binding(step["tool"], step.get("physical_cap"), pin)
            and pin.get("reservation_id") == op == step.get("operation_id") and pin["binding"] == step.get("binding")
            and step.get("version") == pin["version"] and isinstance(evidence, dict)
            and evidence.get("verifier_id") == pin["verifier_id"] and isinstance(evidence.get("verification"), dict)
            and evidence["verification"].get("operation_id") == op)


def verified_feedback_motion(task_id, steps, tools):
    return any(step["tool"] in tools and step.get("status") == "SUCCEEDED"
               and has_motion_proof(step, f"{task_id}/step/{i}", step.get("evidence"))
               for i,step in enumerate(steps,1))


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
    motion_tools = step.get("feedback", {}).get("required_motion_tools", [])
    if step["tool"] in motion_tools and not has_motion_proof(step, op, result):
        raise failure(OutcomeUnknown, "动作回执缺少与原绑定和操作一致的独立验收",
                      "tool.receipt_invalid", "physical.controller")
    step.update(status="SUCCEEDED", evidence=result)
    step["verification"].pop("candidate", None)
    task["cursor"] += 1
    task["retries"] = 0
    last = task["cursor"] == len(task["steps"])
    if last and "feedback" in step:
        if step["feedback"].get("unmet_requirement"):
            journal.save(task, "PLANNING", "已取得只读证据，原任务仍有未满足的能力要求",
                         kind="verified", op=op)
            return
        goal = step["feedback"]["goal"]
        pause_field = goal.get("operator_pause_field")
        if pause_field and isinstance(result,dict) and result.get(pause_field) is True:
            journal.save(task,"BLOCKED",str(result.get("message","等待操作员配合，可续办原任务"))[:400],
                         kind="blocked",op=op)
            return
        if (step["tool"] != goal["tool"] or not isinstance(result, dict) or result.get(goal["field"]) != goal["equals"]
                or (motion_tools and not verified_feedback_motion(task["id"], task["steps"], motion_tools))):
            journal.save(task, "PLANNING", "已取得现场反馈，重新选择下一项能力", kind="verified", op=op)
            return
    journal.save(task, "SUCCEEDED" if last else "RUNNING", "已取得步骤证据", kind="verified", op=op)
