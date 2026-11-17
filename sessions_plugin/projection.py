"""Pure default reading rules migrated from console/server/sessions.py (SDD-010)."""
import json
import math
from urllib.parse import urlencode

STATE_NAMES = {
    "PLANNING": "正在规划", "RUNNING": "正在执行", "WAITING": "正在等待结果",
    "SUCCEEDED": "已成功", "BLOCKED": "已受阻", "RECOVERY_REQUIRED": "需要恢复",
    "FAILED": "已失败", "CANCELED": "已取消",
    "UNKNOWN": "状态未知",
}
IMPORTANT = (
    "agent.accepted", "agent.planned", "agent.dispatch", "agent.verified", "agent.transition",
    "agent.blocked", "agent.error", "agent.timeout", "agent.retry", "agent.retry_requested",
    "agent.retry_exhausted", "agent.canceled", "action.braking", "action.result", "step.verified",
)
TEXT_LIMIT = 240
FINAL_KINDS = {
    "SUCCEEDED":("agent.verified",), "BLOCKED":("agent.blocked","agent.retry_exhausted"),
    "RECOVERY_REQUIRED":("agent.error","agent.timeout","agent.retry_exhausted"),
    "CANCELED":("agent.canceled",), "FAILED":("agent.error","agent.blocked"),
}


def short(value, limit=TEXT_LIMIT):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def object_value(value, default):
    try:
        result = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError):
        return default
    return result if isinstance(result, type(default)) else default


def audit_event(event):
    return (event.get("module") in ("evidence.query","session.default")
            or event["kind"].startswith(("evidence.query_","session.query_")))


def select_events(rows, owner_state):
    """Retain provenance boundaries, latest repeated facts and owner anchors."""
    groups = {}
    audit_count = 0
    ordered = sorted(rows,key=lambda event:event["seq"])
    for event in ordered:
        if audit_event(event):
            audit_count += 1
            continue
        if event["kind"] not in IMPORTANT and event["level"] not in ("warn","error"):
            continue
        key = tuple(event.get(name) for name in ("source","module","source_id","kind","code","operation_id","detail"))
        previous = groups.get(key)
        groups[key] = {**event,"repeats":previous["repeats"]+1 if previous else 1}
    candidates = sorted(groups.values(),key=lambda row:(row["level"] in ("warn","error"),row["seq"]),reverse=True)
    selected = {row["seq"]:row for row in candidates[:16]}
    owner = [event for event in ordered if event["source"]=="mission_agent/tasks"]
    for kind, reverse in (("agent.accepted",False),("agent.planned",True)):
        found = next((row for row in (reversed(owner) if reverse else owner) if row["kind"]==kind),None)
        if found:
            selected.setdefault(found["seq"],{**found,"repeats":1})
    final = next((row for row in reversed(owner) if row["kind"] in FINAL_KINDS.get(owner_state,("agent.transition",))),None)
    if final:
        selected.setdefault(final["seq"],{**final,"repeats":1})
    return sorted(selected.values(),key=lambda row:row["seq"]),len(candidates)>16,audit_count


def reference(event):
    return {key:event.get(key) for key in ("seq","event_id","kind","code")}


def diagnostic(event):
    payload = object_value(event["data"].get("payload"),{})
    trace = object_value(payload.get("trace"),{})
    return object_value(payload.get("diagnostic"),{}) or object_value(trace.get("diagnosis"),{})


def description(event):
    kind, data = event["kind"], event["data"]
    code = event.get("code") or ""
    detail = event.get("detail") or ""
    if kind == "action.braking":
        distance = data.get("front_range_m")
        if isinstance(distance,(int,float)) and math.isfinite(distance):
            return "执行器报告制动，当时前方净空为 "+format(distance,".3f")+" 米。"
    if code == "navigation.path_blocked" or "[code:navigation.path_blocked]" in detail:
        text = "导航执行报告前方路径受阻。"
        if "stationary odometry confirmed" in detail:
            text += "里程计回执已确认机器人停稳。"
        return text
    if code == "navigation.arrived" or "[code:navigation.arrived]" in detail:
        return "导航执行报告已到达目标。" + ("里程计回执已确认停稳。" if "stationary odometry confirmed" in detail else "")
    if kind == "step.verified" and all(isinstance(data.get(key),(int,float)) for key in ("observed_x","observed_y")):
        return "已核验位置 ("+format(data["observed_x"],".2f")+", "+format(data["observed_y"],".2f")+") 米。"
    labels = {
        "agent.verified":"执行步骤已取得并核验结果证据。",
        "agent.dispatch":"已登记本步骤的执行意图，开始调用对应工具。",
        "agent.retry_requested":"已收到续办请求，将从原步骤继续核对。",
        "agent.retry":"曾因暂时问题进入重试。",
        "agent.retry_exhausted":"已达到自动重试上限，需要处理记录中的原因。",
        "agent.canceled":"任务控制器记录取消。",
        "agent.timeout":"等待原操作结果超时，需要核对实际执行结果。",
        "plugin.author.declined":"插件作者记录本次任务不满足自动起草条件。",
        "plugin.author.deferred":"插件作者记录本次起草已延后。",
        "plugin.manager.blocked":"插件管理器记录自动发布或续办受阻。",
    }
    return labels.get(kind) or short(diagnostic(event).get("message") or detail or kind)


def plan_text(steps):
    parts = []
    for step in steps[:6]:
        if not isinstance(step,dict):
            parts.append(short(step,60))
            continue
        tool, args = step.get("tool") or step.get("capability") or "未注明工具", step.get("args") or {}
        if tool == "inspect":
            points = args.get("points") if isinstance(args,dict) else None
            parts.append("前往指定坐标巡检"+(" "+short(json.dumps(points,ensure_ascii=False),80) if points else ""))
        elif tool == "navigate_place":
            parts.append("前往"+short(args.get("place") if isinstance(args,dict) else "",60))
        elif tool == "status":
            parts.append("读取机器人状态")
        else:
            parts.append("调用 "+short(tool,60))
    if len(steps)>6:
        parts.append("另有 "+str(len(steps)-6)+" 步")
    return "，然后".join(parts)+"。" if parts else "账本尚未记录执行计划。"


def project(context, events, coverage):
    """Project already bounded public facts; no database, network or filesystem access."""
    task = dict(context["task"]) if context.get("task") else None
    scope = {key:context["scope"].get(key) for key in ("task_id","trace_id")}
    related = context.get("related_tasks",[])
    related_more = context.get("related_tasks_has_more",False)
    notes = list(context.get("notes",[]))+list(coverage.pop("notes",[]))
    coverage["task_available"] = task is not None
    filters = {"task_id":scope["task_id"]} if scope["task_id"] else {"trace_id":scope["trace_id"]}
    if not task:
        task = {"id":scope["task_id"],"text":"","state":"UNKNOWN","detail":"","created":None,"updated":None,"provider":None}
        notes.append("尚无权威任务记录，仅展示已归档的相关事实。")
    steps = object_value(task.pop("steps",None),[])
    state = task["state"]
    messages = []
    accepted = next((e for e in events if e["source"]=="mission_agent/tasks" and e["kind"]=="agent.accepted"),None)
    messages.append({"role":"user","title":"用户请求","text":short(task.get("text"),800) or "原始请求未记录。",
                     "utc":task.get("created"),"source":"task_journal","refs":[reference(accepted)] if accepted else []})
    planned = next((e for e in reversed(events) if e["source"]=="mission_agent/tasks" and e["kind"]=="agent.planned"),None)
    if not steps and planned:
        steps = object_value(object_value(planned["data"].get("payload"),{}).get("steps"),[])
    if steps or planned:
        messages.append({"role":"agent","title":"执行计划","text":plan_text(steps),
                         "utc":planned["utc"] if planned else task.get("updated"),
                         "source":"public_archive" if planned else "task_journal",
                         "refs":[reference(planned)] if planned else []})
    seen = {}
    for event in events:
        if event["source"]=="mission_agent/tasks" and event["kind"] in (
                "agent.accepted","agent.planned","agent.transition","agent.blocked","agent.error"):
            continue
        text = description(event)
        key = (text,event.get("operation_id"),event["source"],event.get("module"),
               event.get("source_id"),event.get("code"),event.get("detail"))
        if key in seen:
            existing = seen[key]
            existing["refs"] = (existing["refs"]+[reference(event)])[-3:]
            existing["text"] = text
            existing["utc"] = event["utc"]
            existing["occurrences"] += event["repeats"]
            if event["level"] in ("warn","error"):
                existing["role"]="system"
                existing["title"]="曾发生的问题" if state=="SUCCEEDED" else "执行中的问题"
            continue
        is_problem = event["level"] in ("warn","error")
        message = {"role":"system" if is_problem else "tool", "title":"曾发生的问题" if is_problem and state=="SUCCEEDED"
                   else "执行中的问题" if is_problem else "执行进展",
                   "text":text,"utc":event["utc"],"source":"public_archive","operation_id":event.get("operation_id"),
                   "refs":[reference(event)],"occurrences":event["repeats"]}
        seen[key] = message
    progress = list(seen.values())
    # Reserve room for the goal, plan and owner result. Keep abnormal facts even
    # when many successful waypoints occurred earlier.
    ordered = sorted(progress,key=lambda m:(m["role"]=="system",m["refs"][-1]["seq"]),reverse=True)[:7]
    messages.extend(sorted(ordered,key=lambda m:m["refs"][-1]["seq"]))
    coverage["truncated"] |= len(progress)>7
    final_kinds = FINAL_KINDS.get(state,("agent.transition",))
    terminal = next((e for e in reversed(events) if e["source"]=="mission_agent/tasks" and e["kind"] in final_kinds),None)
    last_problem = next((e for e in reversed(events) if e["level"] in ("warn","error")
                         or e["kind"] in ("agent.blocked","agent.error","agent.timeout")),None)
    if terminal and state in ("BLOCKED","RECOVERY_REQUIRED","FAILED"):
        last_problem=terminal
    diagnosis = None
    if last_problem:
        diag = diagnostic(last_problem)
        diagnosis = {"code":diag.get("code") or last_problem.get("code"),
                     "summary":description(last_problem), "origin":diag.get("origin") or last_problem.get("module") or last_problem["source"],
                     "hint":short(object_value(diag.get("catalog"),{}).get("hint")),
                     "historical":state=="SUCCEEDED", "affects_task_outcome":terminal is last_problem,
                     "refs":[reference(last_problem)]}
    outcome = STATE_NAMES.get(state,state or "状态未知")
    if state=="SUCCEEDED":
        result = "任务账本已记录成功。已完成步骤 "+str(sum(isinstance(s,dict) and s.get("status")=="SUCCEEDED" for s in steps))+" / "+str(len(steps))+"。"
    elif state in ("BLOCKED","RECOVERY_REQUIRED","FAILED"):
        reason = diagnosis["summary"] if diagnosis and diagnosis["affects_task_outcome"] else short(task.get("detail"))
        result = "任务"+outcome+"。" + (reason or "具体终态原因尚未归档。")
    elif state=="CANCELED":
        result = "任务账本已记录取消。可按操作回执继续核对停止情况。"
    else:
        result = "任务"+outcome+"。" + short(task.get("detail"),120)
    messages.append({"role":"agent","title":"当前结果","text":result,"utc":task.get("updated"),
                     "source":"task_journal" if coverage["task_available"] else "public_archive",
                     "refs":[reference(terminal)] if terminal else []})
    if not coverage["archived_events"]:
        notes.append("该任务的事件尚未进入当前归档，以上结果来自任务账本。")
    if diagnosis and not diagnosis["affects_task_outcome"] and state!="SUCCEEDED":
        notes.append("此诊断是关联模块的记录，与任务终态的因果关系需要继续核对。")
    if coverage["truncated"]:
        notes.append("关键进展已压缩并限制显示数量，可按任务继续读取事件。")
    notes.append("归档编号表示接收先后，跨模块因果以原事件的父引用为准。")
    coverage["selected_events"] = len({r["seq"] for m in messages for r in m["refs"]} |
                                       {r["seq"] for r in (diagnosis or {}).get("refs",[])})
    coverage["collapsed_events"] = max(0,coverage["archived_events"]-coverage["selected_events"])
    coverage["notes"] = notes
    query = urlencode(filters)
    document = {"schema_version":1,"scope":scope,"task":{key:short(value,800 if key=="text" else 400)
                if key in ("text","detail") else value for key,value in task.items()},
                "headline":result,"messages":messages,"diagnosis":diagnosis,
                "related_tasks":[{**r,"text":short(r["text"],100)} for r in related],
                "related_tasks_has_more":related_more,"coverage":coverage,
                "links":{"events":"/api/events?"+query,"export":"/api/export?"+query,
                         "markdown":"/api/session?"+query+"&format=markdown"}}
    document["markdown"] = markdown(document)
    coverage["summary_characters"] = len(document["markdown"])
    coverage["summary_bytes"] = len(document["markdown"].encode())
    return document


def markdown(document):
    lines = ["# 任务会话", "", "任务 "+str(document["scope"]["task_id"] or "未记录"),
             "调用链 "+str(document["scope"]["trace_id"] or "未记录"), ""]
    for message in document["messages"]:
        refs = " ".join("[#"+str(r["seq"])+"]" for r in message["refs"])
        repeats = " 同一操作下该类事实记录 "+str(message["occurrences"])+" 次。" if message.get("occurrences",1)>1 else ""
        lines.append(message["title"]+" — "+message["text"]+repeats+(" "+refs if refs else ""))
        lines.append("")
    diagnosis = document["diagnosis"]
    if diagnosis:
        lines.append(("历史诊断" if diagnosis["historical"] else "诊断")+" — "+
                     str(diagnosis["code"])+"，来源 "+str(diagnosis["origin"])+"。"+diagnosis["hint"])
        lines.append("")
    for related in document["related_tasks"]:
        lines.append("关联工作 "+related["id"]+" — "+related["text"]+" · "+STATE_NAMES.get(related["state"],related["state"]))
    coverage = document["coverage"]
    lines.extend(["","已归档 "+str(coverage["archived_events"])+" 条，引用 "+str(coverage["selected_events"])+" 条。"+
                  "范围截至 #"+str(coverage["through_seq"])+"。", *coverage["notes"],
                  "", "查细节 "+document["links"]["events"]])
    return "\n".join(lines)+"\n"
