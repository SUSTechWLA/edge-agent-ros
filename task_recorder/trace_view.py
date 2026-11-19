"""Human-readable summaries of the recorder's allowlisted Agent journal projection."""
LABELS = {
    "created":"受理任务", "accepted":"受理任务", "submitted":"受理任务", "planner.context":"读取能力目录",
    "planned":"计划已保存", "dispatch":"记录工具执行意图", "tool.request":"发起工具调用",
    "tool.response":"收到工具回执", "tool.error":"工具调用失败", "verified":"步骤已验证",
    "verification_pending":"等待独立验证", "retry_requested":"请求重试", "retry":"安排重试",
    "failed":"任务失败", "blocked":"任务受阻", "transition":"任务状态变更",
    "waiting_for_submission":"等候任务提交", "task_temporarily_missing":"暂未读到任务",
    "agent_unavailable":"暂时无法连接 Agent", "task_state":"观测到任务状态",
}
LAYERS = {"adapter":"适配器","ros":"ROS","model":"模型","llm":"模型","planner":"规划器",
          "verification":"验证器","verifier":"验证器","tool":"工具"}
ACTIONS = {"request":"发起请求","response":"返回响应","error":"报错"}


def title(row):
    phase = row.get("phase", row["kind"])
    if row["kind"] == "agent_request":
        return "收到用户任务", str(row.get("text",""))
    if row["kind"] == "agent_state":
        name = LABELS.get(phase,phase)
        if row.get("state"):
            name += " · " + str(row["state"])
        detail = ""
        if type(row.get("step_count")) is int and type(row.get("cursor")) is int:
            detail = f'已推进 {row["cursor"]} / {row["step_count"]} 步。'
        return name, detail
    layer, _, action = phase.partition(".")
    fallback = LAYERS[layer]+ACTIONS[action] if layer in LAYERS and action in ACTIONS else phase
    name = LABELS.get(phase, fallback)
    target = row.get("tool") or (row.get("module") if layer in LAYERS else None)
    if target:
        name += " · " + str(target)
    if row.get("outcome"):
        name += " · " + str(row["outcome"])
    pieces = []
    for label, key in [("操作","operation_id"),("错误类型","error_type"),("代码","code"),("日志序号","source_seq"),("源时间","source_utc")]:
        if row.get(key) is not None:
            pieces.append(f'{label} {row[key]}')
    if type(row.get("duration_ms")) in (int,float):
        pieces.append(f'耗时 {row["duration_ms"]} ms')
    return name, "　".join(pieces)


def milestone_key(row):
    """One chapter per meaningful stage per operation; keep every raw event in the index."""
    phase = row.get("phase",row["kind"])
    if row["kind"] == "agent_state":
        return (row["kind"], phase, row.get("state"))
    if row["kind"] != "agent_trace":
        return (row["kind"],phase)
    if row.get("outcome")=="RUNNING":
        return None
    if phase in {"accepted","tool.request","tool.response","verified","planned","dispatch","failed","blocked","retry","retry_requested"} or phase.endswith(".error"):
        return (phase,row.get("operation_id"),row.get("tool"),row.get("outcome"),row.get("code"))
    if phase.startswith(("verification.","verifier.")):
        return (phase,row.get("operation_id"),row.get("outcome"),row.get("code"))
    return None


def tool_history(rows):
    """Rows for the visible tool table. Collapse duplicate polling exchanges by operation and outcome."""
    history = []
    seen = set()
    for row in rows:
        if row["kind"] != "agent_trace" or not row.get("operation_id"):
            continue
        key = milestone_key(row)
        if key is None or key in seen:
            continue
        seen.add(key)
        history.append(row)
    return history
