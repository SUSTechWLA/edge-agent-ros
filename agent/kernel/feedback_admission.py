"""Bind one feedback-planner choice to the commissioned physical controller."""
import hashlib
import json
from copy import deepcopy
from pathlib import Path

from edge_contracts import invoke as protocol
from edge_contracts.errors import OutcomeUnknown, failure
from edge_contracts.soft_transport import proposal_digest
from . import authorization
from .verification import commissioned_motion_binding


INTENT_KIND = "feedback.physical_step.v1"


def hold(reason):
    return failure(OutcomeUnknown, reason + "，请按原操作号对账",
                   "planner.feedback_admission_unconfirmed", "physical.controller")


def policy_snapshot(path):
    if not path:
        raise hold("原反馈工位的物理授权已撤销")
    try:
        data = Path(path).read_bytes()
        policy = json.loads(data)
        allowed = policy.get("commissioned_motion_tools", [])
        if not isinstance(allowed, list) or any(not isinstance(k, str) for k in allowed):
            raise ValueError("invalid commissioned_motion_tools")
        grants = authorization.goal_motion_grants_bytes()
        return allowed, hashlib.sha256(data + (b"\0"+grants if grants else b"")).hexdigest()
    except (OSError, AttributeError, ValueError) as exc:
        raise hold("无法核对当前反馈工位的物理授权") from exc


def check_binding(step, caps, allowed, operation=None):
    name = step.get("tool")
    cap = caps.get(name)
    feedback = step.get("feedback", {})
    roles = feedback.get("required_motion_tools")
    if (feedback.get("unmet_requirement") or not isinstance(roles, list) or name not in roles
            or name in ("inspect", "status") or name not in allowed or "soft_pin" in step or "soft_cap" in step
            or not commissioned_motion_binding(name, cap, cap.get("_physical") if cap else None)):
        raise hold("所选工具未获当前工位的物理许可")
    pin = cap["_physical"]
    public = {k:v for k,v in cap.items() if k not in ("_physical","_legacy_contract")}
    expected = {**pin, "reservation_id":operation} if operation else pin
    if ("reservation_id" in pin or pin["binding"] != step.get("binding") or cap["version"] != step.get("version")
            or proposal_digest(public, expected) != proposal_digest(step.get("physical_cap"), step.get("physical_pin"))):
        raise hold("所选工具与已登记的部署绑定或操作号不一致")


def plan_round(journal, task, trace, policy_file, *, registry, planner, physical_request):
    history = task["steps"]
    index = task["cursor"] + 1
    prior_digest = proposal_digest("feedback-history", history)
    caps = registry()
    intent = journal.soft_intent(task["id"])
    if intent is None:
        active_policy = policy_snapshot(policy_file)
        provider, steps = planner(deepcopy(task), caps, policy_file, trace)
        if (len(steps) != index or len(history) != index-1 or
                proposal_digest("feedback-history", steps[:-1]) != prior_digest):
            raise hold("反馈规划器修改了已执行步骤或给出了多个后续动作")
        step = steps[-1]
        if not any(k in step for k in ("physical_cap","physical_pin")):
            if ("_physical" in caps.get(step["tool"], {}) or caps.get(step["tool"], {}).get("effect")=="actuation"
                    or "soft_pin" in step or "soft_cap" in step):
                raise hold("反馈步骤缺少物理部署绑定")
            return provider, steps
        allowed, policy_digest = policy_snapshot(policy_file)
        if active_policy != (allowed, policy_digest):
            raise hold("模型决策期间物理授权已更改")
        check_binding(step, caps, allowed)
        step = {**step, "feedback":{**step["feedback"], "motion_policy_sha256":policy_digest}}
        intent = journal.soft_intent(task["id"], dict(kind=INTENT_KIND, index=index, history=prior_digest,
                                                     provider=provider, step=step))
    if (not isinstance(intent, dict) or intent.get("kind") != INTENT_KIND
            or type(intent.get("index")) is not int or intent["index"] != index
            or len(history) != index-1 or intent.get("history") != prior_digest
            or not isinstance(intent.get("provider"), str) or not isinstance(intent.get("step"), dict)):
        raise hold("持久化提案与当前反馈轮或已执行历史不一致")
    step, provider = intent["step"], intent["provider"]
    requested = authorization.explicit_motion_request(task["text"])
    if not requested:
        feedback = step["feedback"]
        grant = authorization.station_motion_grant(task["text"],feedback["goal"]["tool"])
        requested = bool(grant and step["tool"] in grant["tools"]
                         and history and history[0]["feedback"].get("station_grant") == grant["digest"]
                         and feedback.get("station_grant") == grant["digest"]
                         and authorization.has_placement_receipt(task,grant))
    if not requested or any(s.get("feedback", {}).get("unmet_requirement") for s in history):
        raise hold("原任务没有机体运动授权或已进入只读受阻状态")
    allowed, policy_digest = policy_snapshot(policy_file)
    if step.get("feedback", {}).get("motion_policy_sha256") != policy_digest:
        raise hold("持久化提案的工位授权已更改")
    check_binding(step, caps, allowed)
    operation = f"{task['id']}/step/{index}"
    frozen = {**step, "physical_pin":{**step["physical_pin"], "reservation_id":operation}}
    # Compare canonical JSON, including value types, against the exact proposal sent to the owner.
    expected = proposal_digest(provider, [frozen])
    reply = physical_request("freeze_step", task=task["id"], index=index, provider=provider, step=step)
    if (not isinstance(reply, (tuple,list)) or len(reply) != 2 or
            proposal_digest(reply[0], [reply[1]]) != expected):
        raise hold("物理所有者的预约回执与本轮原提案不一致")
    return provider, [*history, reply[1]]


def authorize_dispatch(step, operation, cap, policy_file, registry):
    if not policy_file and "feedback" not in step:
        return
    if (cap.get("effect")!="actuation" and "_physical" not in cap
            and "physical_pin" not in step and "physical_cap" not in step):
        return
    allowed, policy_digest = policy_snapshot(policy_file)
    if step.get("feedback", {}).get("motion_policy_sha256") != policy_digest:
        raise hold("恢复的物理步骤缺少当前工位的原始授权")
    check_binding(step, registry(), allowed, operation)


def poll_or_issue(step, operation, cap, policy_file, registry, *, owner_status, issue):
    """Only RESERVED may request a new grant. Every issued operation stays with its original owner."""
    pin = step.get("physical_pin")
    marker = step.get("feedback", {}).get("motion_policy_sha256")
    if (not commissioned_motion_binding(step["tool"], step.get("physical_cap"), pin)
            or step.get("operation_id") != operation or pin.get("reservation_id") != operation
            or pin["binding"] != step.get("binding") or pin["version"] != step.get("version")
            or not isinstance(marker, str) or len(marker) != 64):
        raise hold("无法匹配本轮原始物理预约")
    row = owner_status(pin, "operation_status")
    if (not isinstance(row, dict) or row.get("operation_id") != operation or
            proposal_digest(row.get("pin"),row.get("args")) != proposal_digest(pin,step["args"])):
        raise hold("物理所有者状态与原操作号、绑定或参数不一致")
    state = row.get("state")
    if state == "RESERVED":
        if row.get("settled") is not False or step.get("reconcile_only"):
            raise hold("只读对账不能下发未执行的预约")
        authorize_dispatch(step, operation, cap, policy_file, registry)
        return issue()
    if state in ("OFFERED","CLAIMED","CANDIDATE") or (state == "RECOVERY_REQUIRED" and row.get("pending_stop") is True):
        return None
    if state == "CANCELED":
        reply = {"state":state,"detail":row.get("reason") or "原物理操作已取消"}
    elif state == "FAILED":
        reply = {"state":state,"error":{"code":row.get("failure_code") or "tool.verification_failed",
                                       "message":row.get("reason") or "原物理操作未通过独立验收"}}
    elif state == "SUCCEEDED" and isinstance(row.get("proof"),dict) and isinstance(row.get("candidate"),dict):
        if row["proof"].get("operation_id") != operation:
            raise hold("独立验收回执与原操作号不一致")
        reply = {"state":state,"evidence":{**row["candidate"],"verifier_id":pin["verifier_id"],"verification":row["proof"]}}
    else:
        raise hold("物理所有者尚未确认原操作的结果")
    return protocol.candidate(dict(reply,operation_id=operation), operation, step, cap)
