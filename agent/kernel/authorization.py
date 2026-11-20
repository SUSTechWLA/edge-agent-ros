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


def explicit_motion_request(text):
    """Independently catch direct movement commands if a model proposes a partial read."""
    for clause in re.split(r"[,，。;；!！?？\n]|然后|接着|并且|随后|\band then\b|\bthen\b", text, flags=re.I):
        imperative = re.match(
            r"\s*(?:(?:请|麻烦|先|并|再|让|将|把|控制|让你|机器人|小车|无人机|机器狗|底盘|你)\s*)*"
            r"(?:(?:往|向|朝)(?:前|后|左|右)(?:方|边)?(?:移|行|开|走|退|转)|前进|后退|左转|右转|起飞|降落|"
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
