"""Kernel admission for pinned follow-ups, side effects and execution budgets."""
import os
import re
import time

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


def retry_exhausted(attempts):
    return attempts > int(os.getenv("EDGE_RETRY_LIMIT", "7"))


def step_timed_out(step):
    elapsed = time.time() - step.get("resumed_at", step["started_at"])
    return elapsed > float(os.getenv("EDGE_STEP_TIMEOUT", "1800"))
