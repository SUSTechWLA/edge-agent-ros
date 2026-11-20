"""Deployment-owned authorization for autonomous planning against DSH read tools."""
import json
import os
from pathlib import Path

from edge_contracts.descriptor import NAME, SHA
from edge_contracts.errors import OutcomeUnknown, Refused, failure
from edge_contracts.spatial_facts import identifier


def unique_fields(pairs):
    values = dict(pairs)
    if len(values) != len(pairs):
        raise ValueError("duplicate policy field")
    return values


def load():
    path = os.getenv("EDGE_AUTONOMOUS_READ_POLICY")
    if path is None:
        return None
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized policy")
        document = json.loads(raw, object_pairs_hook=unique_fields)
        if (set(document) != {"schema_version", "bindings"} or type(document["schema_version"]) is not int
                or document["schema_version"] != 1 or not isinstance(document["bindings"], dict)
                or not all(NAME.fullmatch(name) and isinstance(pin, str) and SHA.fullmatch(pin)
                           for name, pin in document["bindings"].items())):
            raise ValueError("invalid policy")
        return document["bindings"]
    except (OSError, TypeError, ValueError, RecursionError) as exc:
        raise failure(Refused, "部署方的只读绑定策略无法读取或格式无效",
                      "planner.read_policy_invalid", "deployment") from exc


def admitted(policy, name, cap, pin):
    # inspect/status have specialised kernel execution paths. Physical pins
    # also have a routing discriminator; a digest alone must never select it.
    return (name in policy and name not in ("inspect", "status") and isinstance(cap, dict)
            and cap.get("id") == name and cap.get("effect") == "read" and not cap.get("endpoint")
            and "_physical" not in cap and isinstance(pin, dict) and pin.get("class") in (None, "soft")
            and pin.get("name") == name and pin.get("binding") == policy[name])


def actor_ref():
    actor = os.getenv("EDGE_ACTOR_REF")
    if actor is not None and not identifier(actor):
        raise failure(Refused, "部署方的主体引用无效", "planner.read_actor_invalid", "deployment")
    return actor


def matches_actor(actor, cap, args):
    return (actor is None or ("actor_ref" not in cap.get("parameters", {}) and "actor_ref" not in args)
            or args.get("actor_ref") == actor)


def require_steps(policy, steps):
    if policy is None:
        if any("read_operator" in s for s in steps):
            raise failure(Refused, "数值算子需要部署方的只读授权", "operator.binding_denied", "deployment")
        return
    if "EDGE_READ_OPERATOR_POLICY" in os.environ or any("read_operator" in s for s in steps):
        from agent.read_operators import authorize_frozen
        authorize_frozen(steps)
    actor = actor_ref()
    for step in steps:
        tool = step.get("tool")
        if ("physical_pin" in step or "physical_cap" in step or not admitted(
                policy, tool, step.get("soft_cap"), step.get("soft_pin")) or step.get("binding") != policy[tool]):
            raise failure(Refused, "步骤超出部署方批准的只读软件绑定，禁止派发",
                          "planner.read_binding_denied", "deployment")
        if not matches_actor(actor, step["soft_cap"], step.get("args", {})):
            raise failure(Refused, "只读调用的主体与本机部署身份不符",
                          "planner.read_actor_denied", "deployment")
        check = step.get("verification")
        if check != {"required": []}:
            raise failure(Refused, "该步骤需要调用本策略未授权的验收服务",
                          "planner.read_verifier_unapproved", "deployment")


def authorize_dispatch(steps):
    try:
        require_steps(load(), steps)
    except Refused as exc:
        if steps[0].get("operation_id") or steps[0].get("status") != "PENDING":
            raise failure(OutcomeUnknown, "已派发操作需要按原操作 ID 对账。" + str(exc),
                          exc.code, exc.origin) from exc
        raise
