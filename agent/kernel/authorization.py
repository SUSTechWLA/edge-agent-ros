"""Kernel admission for pinned follow-ups, side effects and execution budgets."""
import os
import re
import time
import hashlib
import json
from pathlib import Path

from edge_contracts.errors import Refused, Unavailable, failure


_ID = re.compile(r"derived\.[a-z][a-z0-9]*(?:_[a-z0-9]+)*")


def check_id(key):
    if not isinstance(key, str) or len(key) > 64 or not _ID.fullmatch(key):
        raise ValueError("id must be derived.<snake_name>, up to 64 characters")
    return key


def approved_read_tool(caps, pin):
    cap = caps.get(pin["id"])
    if cap is None:
        raise failure(Unavailable, "已授权的只读声明尚未挂载，等待恢复原绑定",
                      "planner.approved_binding_unavailable", "registry")
    if cap["version"] != pin["version"] or cap.get("effect") != "read" or "derived" not in cap or cap.get("endpoint"):
        raise failure(Refused, "已授权的本地只读声明或版本未挂载，需复核发布记录",
                      "planner.approved_binding_unavailable", "registry")
    return cap


def require_effect_allowed(cap, text):
    if cap["effect"]!="read" and re.search(r"不要|禁止|别去|别动|不能|不得|不许|\b(?:not|never|unless|if)\b|如果|除非",text,re.I):
        raise Refused("含否定或条件的执行请求需要明确后再派发")


def goal_motion_grants_bytes():
    path = os.environ.get("EDGE_FEEDBACK_GOAL_MOTION_GRANTS")
    if not path:
        return b""
    with Path(path).open("rb") as stream:
        data = stream.read(16385)
    if not data or len(data) > 16384:
        raise ValueError("invalid goal motion grant file")
    return data


def station_motion_grant(text, goal_tool):
    """Optional deployment grant for a narrowly scoped goal; unrelated utterances grant nothing."""
    data = goal_motion_grants_bytes()
    if not data:
        return None
    rules = json.loads(data)
    if rules["version"] != 1 or not isinstance(rules["goals"],dict):
        raise ValueError("invalid goal motion grants")
    rule = rules["goals"].get(goal_tool)
    if rule is None or not re.fullmatch(rule["request_pattern"], text.strip(), re.I):
        return None
    if (not isinstance(rule["tools"],list) or not rule["tools"]
            or not all(isinstance(k,str) for k in rule["tools"])
            or set(rule["requires_evidence"]) != {"tool","field"}):
        raise ValueError("invalid goal motion grant")
    return {**rule,"digest":hashlib.sha256(data).hexdigest()}


def has_placement_receipt(task, grant):
    pre = grant["requires_evidence"]
    return any(s["tool"] == pre["tool"] and s.get("status") == "SUCCEEDED"
               and s.get("operation_id") == f"{task['id']}/step/{i}"
               and s.get("feedback",{}).get("station_grant") == grant["digest"]
               and (s.get("evidence") or {}).get(pre["field"]) is True
               for i,s in enumerate(task["steps"],1))


def explicit_motion_request(text):
    """Independently catch direct movement commands if a model proposes a partial read."""
    for clause in re.split(r"[,，。;；!！?？\n]|然后|接着|并且?|同时|随后|再|\band(?:\s+then)?\b|\bthen\b",
                           text, flags=re.I):
        body_motion = re.match(
            r"\s*(?:(?:请|麻烦|先|你可以|你来)\s*)*"
            r"(?:(?:控制|让|使|驱动)\s*)?(?:小车|底盘|机器人|无人机|机器狗)\s*"
            r"(?:(?:自主|自动|缓慢|低速)\s*)?"
            r"(?:移动|换位|挪动|行驶|转向)(?!方向|距离|速度|角度|状态|记录|能力|功能)",
            clause)
        if body_motion:
            return True
        imperative = re.match(
            r"\s*(?:(?:请|麻烦|先|并|再|让|将|把|控制|让你|机器人|小车|无人机|机器狗|底盘|你)\s*)*"
            r"(?:(?:往|向|朝)(?:前|后|左|右)(?:方|边)?(?:移|行|开|走|退|转)|"
            r"(?:前进|后退|左转|右转|起飞|降落)(?!方向|标志|标识|距离|速度|角度|高度)|"
            r"导航到|移动到|驶向|驶到|开到|走到|前往|去往|去(?!除|噪|掉|重)|(?:移动|挪动|转动)(?:一下|底盘|机器人)|"
            r"(?:开启|启动)(?:电机|马达|底盘)|(?:please\s+)?(?:move|drive|fly|walk|go)\s+"
            r"(?:forward|backward|back|to|toward|left|right)\b|(?:please\s+)?(?:turn|rotate)\s+(?:left|right)\b)",
            clause, re.I)
        if imperative:
            return True
    return False


def retry_exhausted(attempts):
    return attempts > int(os.getenv("EDGE_RETRY_LIMIT", "7"))


def step_timed_out(step):
    elapsed = time.time() - step.get("resumed_at", step["started_at"])
    return elapsed > float(os.getenv("EDGE_STEP_TIMEOUT", "1800"))
