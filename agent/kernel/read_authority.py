"""Deployment-owned authorization for autonomous planning against DSH read tools."""
import json
import os
from pathlib import Path
import re

from edge_contracts.descriptor import NAME, SHA
from edge_contracts.errors import OutcomeUnknown, Refused, failure
from edge_contracts.spatial_facts import identifier


# Direct change requests are stopped before any model or author runs. The DSH
# allowlist independently enforces the absence of system-changing capabilities.
_PROTECTED = re.compile(
    r"系统框架|(?:agent|机器人|导航|运行时|系统)\s*(?:的)?(?:内核|框架|源码)|底层控制|底盘(?:的)?(?:参数|控制|固件|配置|限速|速度上限|PID)"
    r"|控制器|控制参数|固件|(?:物理|执行器|底盘|运动|硬件)(?:控制|操作|写入)?(?:的)?(?:权限|授权|白名单)"
    r"|\b(?:(?:agent|robot|system|runtime|navigation)\s+(?:framework|kernel|source(?:\s+code)?)"
    r"|firmware|controllers?|(?:chassis|drive[- ]base)\s+(?:parameters?|configuration|limits?|PID)"
    r"|(?:motion|hardware|actuator|physical)(?:\s+control)?\s+(?:permissions?|authority|allowlist))\b", re.I)
_CHANGE = re.compile(
    r"修改|更改|变更|重写|重构|优化|调优|调整|升级|更新|替换|改写|改成|改为|设为|设成|设置|调到|调成|调高|调低|提高|降低|"
    r"刷写|烧录|下发|重刷|部署|关闭|移除|绕过|放开|开放|启用|授予|扩大|解除|撤销"
    r"|\b(?:modify|change|rewrite|refactor|optimi[sz]e|tune|retune|adjust|upgrade|update|replace|patch|set|"
    r"flash|reflash|deploy|disable|bypass|enable|grant|expand|revoke)\b", re.I)
_CLAUSE = re.compile(
    r"[,，。;；!！?？\n]|然后|随后|接着|并且?|同时|(?<!不)(?<!别)(?<!不要)再|之后|但是?|而是|"
    r"\b(?:and(?:\s+then)?|then|but)\b|后(?=请|把|将|读取|" + _CHANGE.pattern + ")", re.I)
_READ_OR_NEGATION = re.compile(
    r"读取|只读|查看|分析|检查|检测|查询|解释|介绍|说明|记录|列出|探讨|评估|讨论|如何|怎么|是否|建议|"
    r"禁止|不要|不得|不许|不用|无需|不必|不准|不能|别|不允许|不做|不对|不(?:再|去)?$"
    r"|\b(?:read|inspect|analy[sz]e|check|explain|describe|review|compare|report|verify|suggest|"
    r"recommend|whether|how|never|not|without|avoid|prohibit)\b|n['’]t\b", re.I)


def system_change_review():
    return failure(Refused, "变更机器人框架、控制器、固件或物理权限需进入部署方的独立变更审查",
                   "planner.system_change_requires_review", "deployment")


def check_read_request(text):
    for clause in _CLAUSE.split(text):
        change = _CHANGE.search(clause)
        target = _PROTECTED.search(clause)
        if change and target and not _READ_OR_NEGATION.search(clause[:change.start()]):
            raise system_change_review()


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


def authorize_dispatch(steps, text=None):
    try:
        policy = load()
        if policy is not None and text is not None:
            check_read_request(text)
        require_steps(policy, steps)
    except Refused as exc:
        if steps[0].get("operation_id") or steps[0].get("status") != "PENDING":
            raise failure(OutcomeUnknown, "已派发操作需要按原操作 ID 对账。" + str(exc),
                          exc.code, exc.origin) from exc
        raise
