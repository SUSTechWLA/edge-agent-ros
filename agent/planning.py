"""Language planning is isolated from execution. Only registered capabilities can be dispatched."""
import json
import hashlib
import math
import os
import re
import unicodedata
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
if __package__:
    from . import runtime_plugins, series_tools
    from .kernel import authorization, read_authority, verification
    from .kernel.catalog import BASE, binding, catalog, is_series, reference_utterance, registry, valid_utterance, valid_verbatim_manifest
    from .tracing import CODE, artifact, contract, exchange
else:
    import _contract_path
    import runtime_plugins
    import series_tools
    from kernel import authorization, read_authority, verification
    from kernel.catalog import BASE, binding, catalog, is_series, reference_utterance, registry, valid_utterance, valid_verbatim_manifest
    from tracing import CODE, artifact, contract, exchange

from edge_contracts.descriptor import NAME, validate_instance
from agent import read_capability_index, read_operators, read_plugin_families
from edge_contracts.errors import OutcomeUnknown, Refused, Unavailable, failure, remote_failure

NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
PAIR = re.compile(rf"\(\s*({NUMBER})\s*,\s*({NUMBER})\s*\)")
# Outcome terms describe requested products, independently of which tools happen
# to be installed. A successful read of odometry cannot produce a photograph.
OUTCOME_TERMS = {
    "image": r"拍照|拍摄.{0,6}(?:图|照)|拍.{0,8}(?:张|照)|照片|相片|拍图|photo(?:graph)?|picture|snapshot",
    "video": r"录像|录制.{0,6}视频|录视频|record.{0,8}video",
    "grasp": r"抓取|抓住|抓起|拿起|夹取|拾起|pick up|grasp",
    "temperature": r"温度|室温|测温|temperature",
    "battery": r"电量|电池|剩余电|battery",
}
STATUS_TERMS = re.compile(r"状态|位姿|姿态|位置|坐标|朝向|方向|朝哪|停在哪|在哪里|在哪儿|速度|车速|航向|里程计|是否.{0,3}(?:移动|停|行驶)|status|position|pose|speed|heading|odometry|where", re.I)
MOVE_TERMS = re.compile(r"巡检|巡查|巡视|前往|去|行驶|驶到|移动到|导航到|开到|挪到|到达|走到|inspect|visit|drive|navigate|move to|go to", re.I)
KEEP_ALIVE = re.compile(r"-1|0|[1-9][0-9]{0,3}[smh]")
HYBRID_THINKING_MODEL = re.compile(r"^(?:[a-z0-9_.-]+/)?qwen3(?:[:.-]|$)", re.I)


def ollama_hybrid_thinking_model(model):
    if not isinstance(model, str):
        return False
    lower = model.strip().lower()
    return bool(HYBRID_THINKING_MODEL.match(lower)) and not any(
        marker in lower for marker in ("instruct", "coder", "thinking")
    )


def valid_keep_alive(value):
    return (type(value) is int and -1 <= value <= 86400) or (
        isinstance(value, str) and bool(KEEP_ALIVE.fullmatch(value))
    )


def requested_outcomes(text, caps):
    clauses = re.split(r"[，,。；;！!？?\n]|然后|并且|instead",normalized(text))
    patterns = dict(OUTCOME_TERMS)
    for cap in caps.values():
        for key,terms in cap.get("outcome_terms",{}).items():
            patterns[key] = "|".join(filter(None,(patterns.get(key),*(re.escape(term) for term in terms))))
    required = set()
    for clause in clauses:
        for key,pattern in patterns.items():
            for match in re.finditer(pattern, clause, re.I):
                prefix = clause[:match.start()]
                if not re.search(r"(?:不需要|无需|不用|不必|不要|禁止|别|no|without|do not|don't)\s*(?:用相机)?\s*$",prefix,re.I):
                    required.add(key)
    return required


def finite_number(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("non-finite number in provider JSON")
    return value


def http_json(url, data, timeout=6):
    body = json.dumps(data, allow_nan=False).encode()
    request = Request(url, data=body, headers={"Content-Type":"application/json"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(2_000_001)
            if len(payload) > 2_000_000:
                raise Refused("adapter response too large")
            length = response.headers.get("Content-Length")
            if length is not None and not response.chunked and len(payload) != int(length):
                raise IncompleteRead(payload)
            return json.loads(payload, parse_constant=finite_number, parse_float=finite_number)
    except IncompleteRead as exc:
        raise failure(Unavailable,"adapter response interrupted before completion",
                      "transport.incomplete_response","transport") from exc
    except HTTPError as exc:
        cls=Unavailable if exc.code==429 or exc.code>=500 else Refused
        with exc:
            try:
                raw=exc.read(16385)
            except (IncompleteRead,OSError):
                raw=b""
        try:
            reply=json.loads(raw, parse_constant=finite_number, parse_float=finite_number) if len(raw)<=16384 else {}
        except (ValueError,UnicodeError):
            reply={}
        if isinstance(reply, dict) and reply.get("error") == "model.schema_mismatch":
            reply = {"error":{"code":"model.schema_mismatch", "message":"模型返回值不符合输出契约"}}
        err=remote_failure(cls,reply if isinstance(reply,dict) else {},"transport.http_"+str(exc.code))
        err.args=(f"adapter HTTP {exc.code}: {err}",)
        raise err from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise failure(Unavailable,f"adapter unavailable: {type(exc).__name__}","transport.unavailable","transport") from exc
    except (ValueError, UnicodeError) as exc:
        raise failure(Refused,"invalid adapter JSON","transport.invalid_json","provider") from exc


def response_schema(caps, points, step_limit, *, protocol=None):
    variants = []
    for cap in caps.values():
        props = {}
        for name, kind in cap["parameters"].items():
            if kind == "xy_list":
                props[name] = {"type":"array","minItems":1,"maxItems":min(8,len(points)),
                               "items":{"enum":points}}
            else:
                props[name] = {"type":kind}
        variants.append({"type":"object","properties":{"tool":{"type":"string","enum":[cap["id"]]},
                         "args":{"type":"object","properties":props,"required":list(props),"additionalProperties":False}},
                         "required":["tool","args"],"additionalProperties":False})
    reason = {"type":"string","maxLength":160} if protocol == "ollama" else {"type":"string"}
    return {"type":"object","properties":{"steps":{"type":"array","maxItems":step_limit,"items":{"oneOf":variants}},
            "reason":reason},"required":["steps","reason"],"additionalProperties":False}


def normalized(text):
    return unicodedata.normalize("NFKC",text).strip().rstrip("。.!！")


def points_in(text):
    return [[float(x),float(y)] for x,y in PAIR.findall(normalized(text))]


def verbatim_step(text, caps):
    """A complete read-tool invocation carries opaque user data between two literal delimiters."""
    matches = []
    for cap in caps.values():
        if not cap.get("verbatim_argument"):
            continue
        name, = cap["parameters"]
        for pattern in cap["utterances"]:
            prefix, suffix = pattern.split("{" + name + "}")
            if len(text) >= len(prefix) + len(suffix) and text.startswith(prefix) and text.endswith(suffix):
                step = {"tool":cap["id"], "args":{name:text[len(prefix):-len(suffix)]}}
                if step not in matches:
                    matches.append(step)
    if len(matches) > 1:
        raise Refused("原文输入匹配多个工具或多个参数范围，请明确选择")
    return matches[0] if matches else None


def match_utterances(request,caps, source_text=None):
    matches=[]
    series_request = series_tools.split_request(source_text if source_text is not None else request)
    for cap in caps.values():
        if cap.get("verbatim_argument"):
            continue
        if is_series(cap):
            if series_request and series_request[0] == cap["description"]:
                matches.append({"tool":cap["id"],"args":series_request[1]})
            continue
        for pattern in cap.get("utterances",[]):
            params = cap["parameters"]
            if len(params) == 1:
                name, = params
                prefix, suffix = normalized(pattern).split("{" + name + "}")
                if not request.startswith(prefix) or not request.endswith(suffix):
                    continue
                value = request[len(prefix):len(request)-len(suffix)].strip()
                if not value:
                    continue
                args = {name: value}
            else:
                template = reference_utterance(normalized(pattern), params, cap.get("effect"))
                matched = template.fullmatch(request) if template else None
                if not matched:
                    continue
                args = matched.groupdict()
            step = {"tool": cap["id"], "args": args}
            if step not in matches:
                matches.append(step)
    if len(matches)>1:
        raise Refused("多个能力可匹配这条指令，请明确子目标")
    return matches or None


def local_plan(text, caps):
    request = normalized(text)
    if re.fullmatch(r"(?:请)?(?:报告|查询|查看|读取|告诉我)?(?:一下)?(?:机器人|本机)?(?:当前)?(?:的位置|位置|状态|位姿)|status",request,re.I):
        return [{"tool":"status","args":{}}]
    named = [item for item in caps.values() if item["id"] not in BASE and not item["parameters"]]
    if any(item["id"]==request for item in named):
        return [{"tool":request,"args":{}}]
    matches = [item for item in named if request==normalized(item["description"])]
    if len(matches)>1:
        raise failure(Refused, "多个工具使用了相同描述，请指定工具 ID："+"、".join(item["id"] for item in matches),
                      "planner.tool_ambiguous", "registry")
    if matches:
        return [{"tool":matches[0]["id"],"args":{}}]
    coordinates = points_in(request)
    skeleton = PAIR.sub("@",request)
    match = re.fullmatch(
        r"(?:请)?(?:依次)?(?:巡检|巡视|前往|到达|去|移动到|导航到|inspect|visit)\s*"
        r"@(?:\s*(?:和|再到|再去|然后到|然后去|、|,|and|then)?\s*@)*"
        r"(?:\s*[,、]?\s*(?:然后|并|再|then)?\s*(报告状态|查询状态|报告位置|report status))?",
        skeleton,re.I)
    if match and coordinates:
        steps = [{"tool":"inspect","args":{"points":coordinates}}]
        if match.group(1):
            steps.append({"tool":"status","args":{}})
        return steps
    return match_utterances(request,caps, text)


def plan(text, caps, trace=None, *, model_profile=None, model_request=None, operator_task=None):
    diagnostic = {"stage":"capability_check"}
    try:
        policy = runtime_plugins.load_policy()
        authority = read_authority.load()
        if authority is None:
            provider, steps = build_plan(text, caps, diagnostic, trace,
                                         model_profile=model_profile, model_request=model_request)
        else:
            read_authority.check_read_request(text)
            eligible = {name:cap for name, cap in caps.items()
                        if read_authority.admitted(authority, name, cap, cap.get("_soft"))
                        and not runtime_plugins.requirements(cap, policy)}
            provider, steps = autonomous_read_plan(text, eligible, diagnostic, trace, model_profile, model_request, operator_task)
        for step in steps:
            step["verification"] = verification_plan(caps[step["tool"]], policy)
        return provider, steps
    except runtime_plugins.PolicyError as exc:
        raise failure(Unavailable, str(exc), "verification.policy_invalid", "registry") from exc
    except (Refused,Unavailable) as exc:
        exc.diagnostic = diagnostic
        raise


def plan_read_only(text, caps, pin):
    """Resolve a restricted continuation against its approved immutable binding."""
    if "binding" in pin:
        cap = caps.get(pin["id"])
        if (not cap or not cap.get("_soft") or cap.get("_physical") or cap.get("endpoint")
                or cap.get("effect") != "read" or binding(cap) != pin["binding"]):
            raise failure(Refused, "已批准的软件绑定不可用", "planner.approved_binding_unavailable", "registry")
        steps = match_utterances(normalized(text), {pin["id"]: cap}, text)
        if not steps or len(steps) != 1:
            raise failure(Refused, "原任务没有匹配已批准工具", "planner.approved_binding_unavailable", "registry")
        step = steps[0]
        step.update(version=cap["version"], binding=binding(cap), status="PENDING",
                    verification=verification_plan(cap, runtime_plugins.load_policy()))
        return "local/pinned-soft", [step]
    cap = authorization.approved_read_tool(caps, pin)
    description, args = text, {}
    if is_series(cap):
        request = series_tools.split_request(text)
        if not request:
            raise failure(Refused, "任务缺少原始数列", "tool.series_invalid", "arguments.series")
        description, args = request
        validate_series_input(text, args, cap)
    if cap["description"] != description:
        raise failure(Refused, "已授权的本地只读声明或版本未挂载，需复核发布记录",
                      "planner.approved_binding_unavailable", "registry")
    policy = runtime_plugins.load_policy()
    return "local/pinned-derived", [{"tool":cap["id"], "args":args, "version":cap["version"],
            "binding":binding(cap), "status":"PENDING", "verification":verification_plan(cap,policy)}]


def validate_series_input(text, args, cap):
    request = series_tools.split_request(text)
    try:
        if not request or request[0] != cap["description"]:
            raise ValueError("series capability must match the requested operation")
        if series_tools.values_from(request[1]) != series_tools.values_from(args):
            raise ValueError("series must match the array supplied in the task")
    except ValueError as exc:
        raise failure(Refused, str(exc), "tool.series_invalid", "arguments.series") from exc


def verification_plan(cap, policy):
    return verification.verification_plan(cap, policy, runtime_plugins.requirements)


def planner_profile():
    """Read once per planning attempt. Invalid explicit configuration fails closed."""
    path=Path(os.getenv("EDGE_CONTRACT_DIR","/etc/edge/contracts"))/"planner-profile.json"
    try:
        with path.open("rb") as stream:
            raw=stream.read(16385)
        if len(raw)>16384:
            raise ValueError()
        profile=json.loads(raw)
        if not isinstance(profile,dict) or set(profile)-{"schema_version","version","protocol","url","model","options","timeout_s","think","keep_alive"}:
            raise ValueError()
        if "timeout_s" in profile and (type(profile["timeout_s"]) is not int or not 5 <= profile["timeout_s"] <= 240):
            raise ValueError()
        if profile.get("schema_version")!=1 or not isinstance(profile.get("version"),str) or not 1<=len(profile["version"])<=64:
            raise ValueError()
        protocol=profile["protocol"]
        url=urlsplit(profile["url"])
        if url.scheme not in ("http","https") or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError()
        if protocol=="ollama":
            if not isinstance(profile.get("model"),str) or not 1<=len(profile["model"])<=128:
                raise ValueError()
            if "think" in profile and type(profile["think"]) is not bool:
                raise ValueError()
            if "keep_alive" in profile and not valid_keep_alive(profile["keep_alive"]):
                raise ValueError()
            options=profile.get("options",{})
            if not isinstance(options,dict) or set(options)-{"temperature","num_ctx","num_thread","num_predict"}:
                raise ValueError()
            temp=options.get("temperature",0)
            ctx=options.get("num_ctx",4096)
            if type(temp) not in (int,float) or not 0<=temp<=2 or type(ctx) is not int or not 128<=ctx<=65536:
                raise ValueError()
            profile["options"]={"temperature":temp,"num_ctx":ctx}
            if "num_thread" in options:
                threads = options["num_thread"]
                if type(threads) is not int or not 1 <= threads <= 64:
                    raise ValueError()
                profile["options"]["num_thread"] = threads
            if "num_predict" in options:
                limit = options["num_predict"]
                if type(limit) is not int or not 1 <= limit <= 1024:
                    raise ValueError()
                profile["options"]["num_predict"] = limit
            if "think" not in profile and ollama_hybrid_thinking_model(profile["model"]):
                profile["think"] = False
        elif protocol!="http-planner" or any(k in profile for k in ("model","options","think","keep_alive")):
            raise ValueError()
        return profile
    except FileNotFoundError:
        plugin=os.getenv("EDGE_PLANNER_URL")
        if plugin:
            return {"version":"env","protocol":"http-planner","url":plugin}
        try:
            model=os.getenv("EDGE_OLLAMA_MODEL","qwen2.5:1.5b")
            raw_ctx=os.getenv("EDGE_OLLAMA_NUM_CTX","").strip()
            ctx=int(raw_ctx) if raw_ctx else 4096
            if not 128<=ctx<=65536:
                raise ValueError()
            opts={"temperature":0,"num_ctx":ctx}
            raw_threads=os.getenv("EDGE_OLLAMA_NUM_THREAD","").strip()
            if raw_threads:
                threads=int(raw_threads)
                if not 1<=threads<=64:
                    raise ValueError()
                opts["num_thread"]=threads
            raw_predict=os.getenv("EDGE_OLLAMA_NUM_PREDICT","").strip()
            if raw_predict:
                limit=int(raw_predict)
                if not 1<=limit<=1024:
                    raise ValueError()
                opts["num_predict"]=limit
            env_profile={"version":"env","protocol":"ollama","url":os.getenv("EDGE_OLLAMA_URL"),
                         "model":model,"options":opts}
            raw_keep=os.getenv("EDGE_OLLAMA_KEEP_ALIVE","").strip()
            if raw_keep:
                keep=int(raw_keep) if re.fullmatch(r"-?[0-9]+",raw_keep) else raw_keep
                if not valid_keep_alive(keep):
                    raise ValueError()
                env_profile["keep_alive"]=keep
            raw_think=os.getenv("EDGE_OLLAMA_THINK","").strip().lower()
            if raw_think:
                if raw_think not in ("0","1","false","true"):
                    raise ValueError()
                env_profile["think"]=raw_think in ("1","true")
            elif ollama_hybrid_thinking_model(model):
                env_profile["think"]=False
            return env_profile
        except ValueError as env_exc:
            raise failure(Refused,"规划服务配置无效，请核对环境变量或 planner-profile.json",
                          "planner.profile_invalid","registry") from env_exc
    except (ValueError,TypeError,KeyError,OSError,AttributeError) as exc:
        raise failure(Refused,"规划服务配置无效，请核对 planner-profile.json",
                      "planner.profile_invalid","registry") from exc


def model_proposal(context, schema, prompt_key, diagnostic, trace, model_profile, model_request, prompt_text=None):
    profile = planner_profile() if model_profile is None else model_profile
    request = http_json if model_request is None else model_request
    url = profile["url"]
    if not url:
        raise failure(Refused, "需要连接部署方配置的模型规划器", "planner.not_configured", "planner")
    revision = {"version":profile["version"], "sha256":hashlib.sha256(
        json.dumps(profile, sort_keys=True, ensure_ascii=False).encode()).hexdigest()}
    plugin = profile["protocol"] == "http-planner"
    provider = "http-planner" if plugin else "ollama/" + profile["model"]
    diagnostic.update(stage="planner", provider=provider)
    module = "operator-author" if prompt_key == "operator.author" else "planner"
    timeout = profile.get("timeout_s", 90)
    if plugin:
        body = {"schema_version":1, **context, "output_schema":schema}
        if prompt_key != "planner.system":
            body["instructions"] = prompt_text or contract("prompts", prompt_key)["text"]
        answer = exchange(trace, "model", module, body, lambda:request(url, body, timeout),
                          meta={"provider":provider, "protocol":"planner.http.v1", "profile":revision})
    else:
        prompt = (contract("prompts", prompt_key) if prompt_text is None else
                  {"id":prompt_key,"text":prompt_text,"version":{"planner.select_read_family":"family-selection.1",
                   "planner.station_feedback":"station-feedback.2"}.get(prompt_key,read_operators.numeric.LANGUAGE),
                   "sha256":hashlib.sha256(prompt_text.encode()).hexdigest()})
        body = {"model":profile["model"], "stream":False, "format":schema, "options":profile["options"],
                "messages":[{"role":"system", "content":prompt["text"]},
                            {"role":"user", "content":json.dumps(context, ensure_ascii=False)}]}
        if "think" in profile:
            body["think"] = profile["think"]
        elif ollama_hybrid_thinking_model(profile["model"]):
            body["think"] = False
        if "keep_alive" in profile:
            body["keep_alive"] = profile["keep_alive"]
        reply = exchange(trace, "model", module, body, lambda:request(url.rstrip("/") + "/api/chat", body, timeout),
                         meta={"provider":provider, "protocol":"ollama.chat", "profile":revision,
                               "prompt":{k:prompt[k] for k in ("id","version","sha256")}})
        answer = json.loads(reply["message"]["content"], parse_constant=finite_number, parse_float=finite_number)
    return provider, answer


def feedback_tool_ids(policy, field):
    names = policy.get(field, [])
    if (not isinstance(names, list) or len(names) > 128 or
            any(not isinstance(name, str) or not NAME.fullmatch(name) for name in names)
            or len(set(names)) != len(names)):
        raise failure(Refused, "站点只读观察范围配置无效或动作工具配置无效，机体运动权限不可用",
                      "planner.observation_policy_invalid", "deployment")
    return set(names)


def feedback_plan(task, caps, policy_file, trace, *, model_profile=None, model_request=None):
    """Select one installed station capability against persisted tool observations."""
    policy = json.loads(Path(policy_file).read_bytes())
    history = task["steps"]
    goals = dict(policy["goals"])
    motion_ids = feedback_tool_ids(policy, "commissioned_motion_tools")
    motion_requested = authorization.explicit_motion_request(task["text"])
    read_only = policy.get("read_only_request_pattern")
    read_only_request = read_only and re.search(read_only, task["text"], re.I | re.S)
    if history:
        prior = history[0]["feedback"]["goal"]
        goals = {name:goal for name,goal in goals.items() if goal["tool"] == prior["tool"]}
    else:
        if read_only_request:
            goals = {name:g for name,g in goals.items()
                     if g["tool"] not in motion_ids and all(caps.get(tool,{}).get("effect") == "read"
                                                           for tool in g["tools"] if tool not in motion_ids)}
        explicit = {name:g for name,g in goals.items() if g.get("required_outcome_pattern")
                    and re.search(g["required_outcome_pattern"], task["text"], re.I | re.S)}
        if explicit:
            # A named intermediate observation can be a step towards a stronger requested outcome.
            intermediate = {n for goal in explicit.values() for n in goal.get("supersedes",[])}
            goals = {name:g for name,g in explicit.items() if name not in intermediate}
    station_grant = None
    if len(goals) == 1 and not motion_requested and not read_only_request:
        station_grant = authorization.station_motion_grant(task["text"],next(iter(goals.values()))["tool"])
        if station_grant and history and history[0]["feedback"].get("station_grant") != station_grant["digest"]:
            station_grant = None  # A running task cannot acquire a newly installed movement grant.
        if station_grant:
            motion_ids &= set(station_grant["tools"])
            motion_requested = True
    allowed = {name for goal in goals.values() for name in goal["tools"]}
    tools = {key:caps[key] for key in allowed - motion_ids if key in caps and caps[key].get("endpoint")
             and caps[key].get("effect") in ("read", "idempotent")
             and "_soft" not in caps[key] and "_physical" not in caps[key]}
    goal_budget = next(iter(goals.values())).get("max_steps", policy["max_steps"]) if len(goals)==1 else policy["max_steps"]
    step_limit = min(64, goal_budget)
    unmet = None
    prior_denial = any(s["feedback"].get("unmet_requirement") == "body.motion" for s in history)
    motion_tools = {}
    prior_roles = history[0]["feedback"].get("required_motion_tools") if history else None
    if motion_requested and not prior_denial and not read_only_request:
        # Existing tasks cannot silently acquire additional physical roles after a reload.
        eligible_ids = motion_ids.intersection(prior_roles or ()) if history else motion_ids
        for name in eligible_ids & allowed & caps.keys():
            cap = caps[name]
            pin = cap.get("_physical")
            if verification.commissioned_motion_binding(name, cap, pin) and "reservation_id" not in pin:
                motion_tools[name] = cap
    if motion_tools:
        goals = {name:g for name,g in goals.items() if motion_tools.keys() & set(g["tools"])}
        tools.update(motion_tools)
    elif motion_requested or prior_denial or prior_roles:
        budget = policy.get("unavailable_motion_observation_budget", 0)
        if type(budget) is not int or not 1 <= budget <= 8:
            raise Refused("本站没有机体运动权限")
        unmet = "body.motion"
        reads_left = max(0, min(step_limit, budget) - len(history))
        tools = {key:cap for key,cap in tools.items() if reads_left and cap.get("effect") == "read"}
        if "unavailable_motion_observation_tools" in policy:
            permitted = feedback_tool_ids(policy, "unavailable_motion_observation_tools")
            tools = {key:cap for key,cap in tools.items() if key in permitted}
    if not unmet and (not tools or len(history) >= step_limit):
        raise Refused("本站没有可调用能力或已用尽决策预算")
    by_goal = {name:set(g["tools"]).intersection(tools) for name,g in goals.items()}
    if motion_tools:
        for name,g in goals.items():
            roles = by_goal[name] & motion_tools.keys()
            if g["tool"] not in roles and not verification.verified_feedback_motion(task["id"], history, roles):
                by_goal[name].discard(g["tool"])
        tools = {key:cap for key,cap in tools.items() if any(key in names for names in by_goal.values())}
    values_by_tool = dict(policy.get("argument_values",{}))
    unmet_inputs = {}
    placement_pending = bool(station_grant and motion_tools and not authorization.has_placement_receipt(task,station_grant))
    if placement_pending:
        pre = station_grant["requires_evidence"]
        # This site's automatic-motion grant requires a current task's identified target before use.
        observations = {pre["tool"], *station_grant.get("diagnostics",[])}
        tools = {k:v for k,v in tools.items() if k in observations and v["effect"] == "read"}
        unmet_inputs["placement"] = {"source_tool":pre["tool"],"required_fact":pre["field"]}
    elif station_grant and motion_tools and station_grant.get("placement_tools_only_before_receipt") is True:
        for name in (station_grant["requires_evidence"]["tool"], *station_grant.get("diagnostics",[])):
            tools.pop(name, None)
    for tool, parameters in policy.get("argument_receipts", {}).items():
        if tool not in tools:
            continue
        for arg, source in parameters.items():
            choices = []
            for step in reversed(history):
                if step.get("physical_pin"):
                    break  # Body-local references expire as soon as a motion is issued.
                if step["tool"] == source["tool"]:
                    result = step.get("evidence") or {}
                    rows = result.get(source["rows"])
                    if step.get("status") == "SUCCEEDED" and isinstance(rows,list):
                        choices = [r[source["field"]] for r in rows[:16] if isinstance(r, dict)
                                   and isinstance(r.get(source["field"]),str)
                                   and re.fullmatch(r"[A-Za-z0-9_.-]{1,96}",r[source["field"]])
                                   and source["requires"] in r]
                    break
            if not choices:
                tools.pop(tool, None)
                unmet_inputs[tool] = {"argument":arg, "source_tool":source["tool"],
                                      "reason":"需要当前姿态下由提供者返回的有效引用"}
                break
            values_by_tool.setdefault(tool,{})[arg] = choices
    if motion_tools:
        for name, rule in policy.get("unchanged_observation_guards", {}).items():
            if name not in tools:
                continue
            superseded = rule.get("skip_when_available")
            if superseded and superseded in tools:
                tools.pop(name)
                unmet_inputs[name] = {"superseded_by":superseded, "observed":True}
                continue
            sources = set(rule.get("sources") or [name])
            for step in reversed(history):
                if step.get("physical_pin"):
                    break
                receipt = step.get("evidence") or {}
                if step["tool"] == rule.get("recheck") and receipt.get("outcome") in rule.get("changed", []):
                    break
                if step["tool"] in sources and receipt.get("outcome") in rule["unchanged"]:
                    tools.pop(name)
                    unmet_inputs[name] = {"reason":"原姿态已取得相同观测，待新姿态或新的独立测量"}
                    break
    # Some tools can only use an existing intermediate product. The source of that
    # product owns its readiness and can invalidate it after a new measurement.
    progress_tools = set()
    for name, condition in policy.get("receipt_conditions", {}).items():
        if name not in tools:
            continue
        field = condition.get("field")
        skip_field = condition.get("skip_when_true")
        latest = None
        skip = None
        settled_motion = False
        for s in reversed(history):
            if condition.get("invalidate_on_motion") is True and s.get("physical_pin"):
                settled_motion = s.get("status")=="SUCCEEDED"
                break
            if s.get("status")=="SUCCEEDED" and s["tool"] in condition["sources"]:
                ev = s.get("evidence") or {}
                if field and latest is None and field in ev:
                    latest = ev[field]
                if skip_field and skip is None and skip_field in ev:
                    skip = ev[skip_field]
                if (not field or latest is not None) and (not skip_field or skip is not None):
                    break
        if skip is True:
            tools.pop(name)
            unmet_inputs[name] = {"superseded_by":skip_field, "observed":True}
        elif field and (latest is False or (condition.get("require_true") is True and latest is not True)):
            tools.pop(name)
            unmet_inputs[name] = {"required_fact":field, "observed":False}
        elif (latest is True and condition.get("confirmed_progress") is True
              or latest is None and settled_motion and condition.get("confirmed_after_motion") is True):
            progress_tools.add(name)
    newest_view = next((s for s in reversed(history) if s["tool"] in policy.get("view_evidence_tools", [])
                        or s.get("physical_pin")), None)
    has_spatial_views = any("navigation_context" in (step.get("evidence") or {}) for step in history)

    def summary(step):
        evidence = step.get("evidence") or {}
        # Detailed arrays, imagery and transforms belong in the operation archive.
        # Deployment labels and ROS transport addresses belong to the adapter and the audit UI.
        transport = {"environment", "robot_id", "clock_domain", "source_host", "endpoint", "mount_reference"}
        facts = {k:(v[:200] if isinstance(v,str) else v) for k,v in evidence.items()
                 if isinstance(v,(bool,int,float,str)) and k not in transport
                 and not k.endswith(("sha256", "_topic"))}
        row = {"tool":step["tool"], "args":step.get("args",{}),"facts":dict(list(facts.items())[:18])}
        current_view = not has_spatial_views or step is newest_view
        if step["tool"] in policy.get("scene_evidence_tools", []) and (not has_spatial_views or step is history[-1]):
            from edge_contracts.semantic_scene import compact_scene
            scene = evidence.get("scene_slice") or (evidence.get("observation") or {}).get("scene_slice")
            if scene is not None:
                try:
                    row["scene"] = compact_scene(scene)
                except (KeyError, TypeError, ValueError):
                    row["scene_unavailable"] = True
        if step["tool"] in policy.get("view_evidence_tools", []) and current_view:
            if "navigation_context" in evidence:
                from edge_contracts.capture_navigation import compact_context
                try:
                    row["navigation"] = compact_context(evidence["navigation_context"])
                except (KeyError, TypeError, ValueError):
                    row["navigation_unavailable"] = True
            options = evidence.get("view_options", [])
            if isinstance(options, list):
                fields = ("ref","path","board_view","board_lateral_m","clearance_margin_m","new_rgb_cells",
                          "fills_missing_side","end_heading_deg","end_xyz_m","path_margin_px",
                          "camera_pitch_deg","camera_roll_deg","helps_camera_coverage","lidar_view",
                          "intrinsic_information_gain") if has_spatial_views else (
                          "ref","target_range_m","obliquity_deg","margin_px","displacement_m","min_marker_module_px",
                          "anchor_ratio","sample_role","image_offset_px","path_margin_px")
                row["options"] = [{k:v for k,v in opt.items() if k in fields
                                   and (isinstance(v,str) and len(v)<=64 or type(v) is bool
                                        or type(v) in (float,int) and math.isfinite(v)
                                        or k=="end_xyz_m" and isinstance(v,list) and len(v)==3
                                        and all(type(x) in (float,int) and math.isfinite(x) for x in v))}
                                  for opt in options[:6] if isinstance(opt,dict)]
        return row
    recent = [summary(s) for s in history[-5:]]
    if recent:
        last = recent[-1]
        key = last["tool"]
        if (key in tools and tools[key]["effect"]=="read" and len(tools)>1 and
                last["facts"].get("outcome") and not last["facts"].get("waiting_for_operator") and
                last["facts"].get("detected") is not False):
            tools.pop(key)
    context = {"task": task["text"], "goals": {key:{"description":g["description"],
               "required_evidence":{"tool":g["tool"],"field":g["field"]}} for key,g in goals.items()},
               "tools": {key:{"description":cap["description"],"arguments":cap["parameters"]} for key,cap in tools.items()},
               "observations": recent,
               "prior_calls": len(history)}
    if unmet:
        context["unmet_requirement"] = {"capability":unmet, "available":False,
                                        "remaining_observations":reads_left,
                                        "observation_scope":"只影响动作。仍可用本轮工具核实原任务中可独立观测的部分。"}
    if motion_tools:
        context["required_motion"] = {"tools":sorted(motion_tools),
                                      "verified":verification.verified_feedback_motion(task["id"],history,motion_tools)}
    if unmet_inputs:
        context["unmet_inputs"] = unmet_inputs
    for tool, parameters in policy.get("argument_catalogs",{}).items():
        values_by_tool[tool] = {}
        for arg, source in parameters.items():
            installed = []
            for path in sorted((Path(policy_file).parent/source["directory"]).glob("*.json")):
                installed.extend(json.loads(path.read_bytes())[source["field"]])
            values_by_tool[tool][arg] = sorted(name for name in set(installed) if installed.count(name)==1)
    active_profile = planner_profile() if model_profile is None else model_profile
    reason_schema = {"type":"string","maxLength":160} if active_profile.get("protocol") == "ollama" else {"type":"string"}
    branches = []
    decline_available = unmet or not (progress_tools & tools.keys())
    for name in [*sorted(tools), *(["decline"] if decline_available else [])]:
        arg_schema = autonomous_argument_schema(tools[name]) if name in tools else {"type":"object","properties":{}}
        for arg,values in values_by_tool.get(name,{}).items():
            arg_schema["properties"][arg]["enum"]=values
        branches.append({"type":"object","properties":{"goal":{"type":"string","enum":list(goals)},
                         "tool":{"type":"string","enum":[name]},"args":arg_schema,"reason":reason_schema},
                         "required":["goal","tool","args","reason"],"additionalProperties":False})
    schema={"oneOf":branches}
    instructions = ("Choose one available tool that makes progress on the user's task based on the latest facts. "
                    "If initial state is unknown inspect it. Respect prerequisites. Reobserve when the situation "
                    "changes. A person may take minutes to respond; when waiting for external input use a suitable "
                    "observation or armed recording tool to wait, even if prior attempts did not see anything. "
                    "Once a condition is verified, do not keep checking it. Use an action that advances the requested "
                    "outcome. Multiple distinct samples require repeated acquisitions until the required count is met. "
                    "Choose decline only for an unsupported "
                    "request. Give a short reason in Chinese based on observable evidence.")
    if has_spatial_views and policy.get("view_collection_guidance") is True:
        instructions += (" If the rover has stopped at a new view or an inspection reports a recordable pose, "
                         "prefer the goal's stationary sample tool when available. Compare lateral shortfall, "
                         "new image regions, predicted tilt and margin before selecting another route.")
    if not decline_available:
        instructions += (" A provider has confirmed a ready tool: " + ", ".join(sorted(progress_tools & tools.keys())) +
                         ". Use a listed capability to continue.")
    if unmet:
        instructions = ("区分用户原任务中的动作和可独立核实的事实。unmet_requirement 表示动作受限。"
                        "若目标、传感器或执行条件仍有未知事实，且本轮工具可直接测量、能推进原任务，"
                        "请选择最有用的一项工具取得证据。根据 observations 判断哪些事实已知。"
                        "若现有观测已足够、工具无法增加相关证据，或本轮没有工具，请选择 decline，"
                        "用中文说明实测发现与缺失的能力。采集观察不授予动作权限，保留原任务的完成条件。")
    diagnostic = {}
    provider, answer = model_proposal(context,schema,"planner.station_feedback",diagnostic,trace,active_profile,
                                      model_request,prompt_text=instructions)
    if unmet and (not isinstance(answer, dict) or not isinstance(answer.get("tool"), str)
                  or answer["tool"] not in tools):
        message = "本站缺少机体运动权限，原任务受阻"
        valid_decline = (isinstance(answer, dict) and answer.get("tool") == "decline"
                         and isinstance(answer.get("goal"), str) and answer["goal"] in goals
                         and answer.get("args") == {} and isinstance(answer.get("reason"), str)
                         and answer["reason"].strip())
        if valid_decline:
            message += "。" + answer["reason"].strip()[:260]
        error = failure(Refused, message, "planner.required_capability_unavailable", "deployment",
                        {"unmet_requirement":unmet,"observations":len(history)})
        error.diagnostic = diagnostic
        raise error
    if not isinstance(answer,dict) or not isinstance(answer.get("reason"),str) or answer.get("tool") not in tools:
        raise Refused("模型未选择可用能力: "+str(answer.get("reason",""))[:260] if isinstance(answer,dict)
                      else "模型返回了无效决策")
    cap = tools[answer["tool"]]
    goal=goals.get(answer.get("goal"))
    if not goal or cap["id"] not in by_goal[answer["goal"]]:
        raise Refused("所选工具超出本次目标的登记权限")
    args=answer.get("args")
    validate_instance(autonomous_argument_schema(cap),args)
    if any(args.get(k) not in vals for k,vals in values_by_tool.get(cap["id"],{}).items()):
        raise Refused("模型参数超出站点登记范围")
    authorization.require_effect_allowed(cap, task["text"])
    if (cap["id"] not in motion_tools and cap["effect"] != "read"
            and not any(w in task["text"].lower() for w in goal.get("write_intent",[]))):
        raise Refused("用户任务未授权本工位的写入操作")
    step = {"tool":cap["id"],"args":args,"status":"PENDING","version":cap["version"],"binding":binding(cap),
            "verification":verification_plan(cap,runtime_plugins.load_policy()),
            "feedback":{"rationale":answer["reason"][:400], "goal":{k:goal[k] for k in
                        ("tool","field","equals","operator_pause_field") if k in goal}}}
    if unmet:
        step["feedback"]["unmet_requirement"] = unmet
    if station_grant:
        step["feedback"]["station_grant"] = station_grant["digest"]
        if placement_pending:
            step["feedback"]["placement_required"] = True
    if motion_tools:
        step["feedback"]["required_motion_tools"] = sorted(set(goal["tools"]) & motion_tools.keys())
        if cap["id"] in motion_tools:
            step["physical_pin"] = cap["_physical"]
            step["physical_cap"] = {k:v for k,v in cap.items() if k not in ("_physical", "_legacy_contract")}
    return provider, [*history, step]


def autonomous_argument_schema(cap):
    if "_parameter_schema" in cap:
        return cap["_parameter_schema"]
    return {"type":"object", "properties":{k:{"type":v} for k,v in cap["parameters"].items()},
            "required":list(cap["parameters"]), "additionalProperties":False}


def read_output_schema(entries, protocol, computation=None):
    def step(names, arguments):
        return {"type":"object", "properties":{"tool":{"type":"string", "enum":names}, "args":arguments},
                "required":["tool","args"], "additionalProperties":False}

    def branch(dispositions, steps):
        reason = {"type":"string", "minLength":1}
        if protocol == "ollama":
            reason["maxLength"] = 128
        return {"type":"object", "properties":{"disposition":{"type":"string", "enum":dispositions},
                "reason":reason, "steps":steps},
                "required":["disposition","reason","steps"], "additionalProperties":False}

    if protocol == "http-planner" and computation is None:
        # Keep the v1 HTTP envelope stable; deployed providers receive the
        # complete input contracts in capabilities and the kernel checks them.
        entry = step([item["id"] for item in entries], {"type":"object"})
        return branch(["ready","requires_control","requires_system_review","unsupported"],
                      {"type":"array", "items":entry, "maxItems":16})
    choices = [step([item["id"]], item["input_schema"]) for item in entries]
    ready = branch(["ready"], {"type":"array", "items":{"oneOf":choices}, "minItems":1, "maxItems":16})
    denied = branch(["requires_control","requires_system_review","unsupported"], {"type":"array", "maxItems":0})
    options = [ready, denied] if choices else [denied]
    if computation is not None:
        compose = branch(["read_compute"], {"type":"array", "maxItems":0})
        compose["properties"]["computation"] = computation
        compose["required"].append("computation")
        options.append(compose)
    return {"type":"object", "oneOf":options}


def validate_read_answer(answer, caps, text, actor):
    if (not isinstance(answer, dict) or set(answer) != {"disposition","steps","reason"}
            or not isinstance(answer["steps"], list) or not isinstance(answer["reason"], str)
            or not answer["reason"].strip()
            or answer["disposition"] not in ("ready","requires_control","requires_system_review","unsupported")):
        raise ValueError("需要 disposition、steps 和非空 reason")
    if answer["disposition"] == "requires_system_review":
        raise read_authority.system_change_review()
    if answer["disposition"] == "requires_control" or authorization.explicit_motion_request(text):
        raise failure(Refused, "任务涉及机器人控制，需要单独取得操作授权",
                      "planner.control_requires_approval", "deployment")
    if answer["disposition"] == "unsupported":
        raise failure(Refused, answer["reason"][:1200], "planner.unsupported", "planner")
    if not 1 <= len(answer["steps"]) <= 16:
        raise ValueError("ready 须提供 1–16 个步骤")
    steps = []
    for item in answer["steps"]:
        if (not isinstance(item, dict) or set(item) != {"tool","args"} or not isinstance(item["tool"], str)
                or item["tool"] not in caps or not isinstance(item["args"], dict)):
            raise ValueError("步骤只能使用本轮目录中的 tool 和对象 args")
        cap = caps[item["tool"]]
        if not read_authority.matches_actor(actor, cap, item["args"]):
            raise ValueError(f"{cap['id']} 的 actor_ref 必须取本机部署身份 {actor}")
        try:
            validate_instance(autonomous_argument_schema(cap), item["args"])
        except ValueError as exc:
            raise ValueError(f"{cap['id']} 参数与契约不一致：{exc}") from exc
        if cap.get("verbatim_argument") and item != verbatim_step(text, {cap["id"]:cap}):
            raise ValueError("该工具的输入必须与用户原文完全一致")
        if is_series(cap):
            validate_series_input(text, item["args"], cap)
        steps.append({**item, "version":cap["version"], "binding":binding(cap), "status":"PENDING"})
    return steps


def select_read_family(text, families, diagnostic, trace, profile, model_request):
    if authorization.explicit_motion_request(text):
        raise failure(Refused, "任务需要控制授权", "planner.control_requires_approval", "deployment")
    by_id = {f["id"]:f for f in families}
    if len(by_id) != len(families):
        raise failure(Refused, "站点语义契约名称重复", "planner.capability_index_invalid", "deployment")
    schema = read_plugin_families.selection_schema(by_id)
    context = {"request":text, "contracts":[{"id":f["id"], "returns":f["description"]} for f in families]}
    provider, answer = model_proposal(context, schema, "planner.select_read_family", diagnostic, trace,
                                      profile, model_request, read_plugin_families.INSTRUCTIONS)
    try:
        validate_instance(schema, answer)
        if not answer["reason"].strip():
            raise ValueError("empty selection reason")
    except (ValueError, KeyError, TypeError) as exc:
        raise failure(Refused, "模型返回的插件契约选择无效", "planner.invalid_proposal", "planner") from exc
    selected = by_id.get(answer["selected"])
    event = {"layer":"planner", "module":"planner", "provider":provider, "reason":answer["reason"],
             "artifact":artifact(answer)}
    if trace:
        trace("planner.family_selected", {**event,"selected":answer["selected"]})
    if selected and "candidates" in selected:
        choice = selected["preferred"]
        if trace:
            trace("planner.implementation_selected", {"layer":"planner", "module":"registry",
                  "contract":selected["id"], "index_digest":selected["index_digest"],
                  "candidates":selected["candidates"], "tool":choice["tool"] if choice else None,
                  "reason":"first authorized live DSH binding in site priority order"})
        if choice is None:
            raise failure(Unavailable, "该结果契约暂时没有可接收新任务的已授权实现",
                          "planner.implementation_unavailable", "registry")
    if selected and not selected["installed_tools"]:
        diagnostic.update(stage="plan_validation",family=selected["id"])
        if trace:
            trace("planner.plugin_gap", {**event,"family":selected["id"]})
        raise failure(Refused, "模型已请求开发站点授权的只读插件", "planner.plugin_design_requested", "planner")
    return selected


def autonomous_read_plan(text, caps, diagnostic, trace, model_profile, model_request, operator_task=None):
    entries = [dict(item, input_schema=autonomous_argument_schema(caps[item["id"]]),
                    binding=binding(caps[item["id"]])) for item in catalog(caps)]
    if trace:
        trace("planner.context", {"layer":"planner", "module":"registry", "authority":"deployment.read_only",
                                 "artifact":artifact(entries)})
    diagnostic["eligible_tools"] = list(caps)
    implementations = read_capability_index.catalog(caps)
    families = read_plugin_families.catalog(caps) + implementations
    missing_families = [family["id"] for family in families if not family["installed_tools"]]
    if not caps and not missing_families:
        raise failure(Refused, "没有已获部署方批准且可用的只读软件能力", "planner.read_catalog_empty", "deployment")
    profile = planner_profile() if model_profile is None else model_profile
    actor = read_authority.actor_ref()
    context = {"request":text, "authority":"deployment.read_only", "capabilities":entries}
    site = read_operators.Operators.configured()
    ordinary = caps if site is None else {k:v for k,v in caps.items() if k != site.runner}
    computation = None
    saved = operator_task[0].operator_intent(operator_task[1]) if operator_task else None
    if saved and (not site or site.runner not in caps):
        raise read_operators.reject("原任务已保存的计算工具不再可用", "operator.binding_denied")
    family = select_read_family(text, families, diagnostic, trace, profile, model_request) if families and not saved else None
    if not caps:
        raise failure(Refused, "没有获准的现场工具可执行普通任务", "planner.read_catalog_empty", "deployment")
    if family:
        ordinary = {name:caps[name] for name in family["installed_tools"]}
        context["matched_read_contract"] = {"id":family["id"], "description":family["description"]}
    elif implementations:
        managed = {row["tool"] for spec in implementations for row in spec["candidates"]}
        ordinary = {k:v for k,v in ordinary.items() if k not in managed}
    if not family and site and site.runner in caps and operator_task:
        input_schema = site.input_schema(caps[site.runner])
        computation = {"type":"object","properties":{"contract":{"type":"string","enum":list(site.contracts)},
                       "input":input_schema},"required":["contract","input"],"additionalProperties":False}
        context["numeric_computation"] = {"runner":site.runner,"input_schema":input_schema,"contracts":site.describe(),
            "instructions":"For a requested numeric result that requires combining sensor measurements, choose "
            "read_compute, the matching mathematical contract, and the input measurement. This path also reuses "
            "installed operators. A sensor measurement alone cannot deliver an additional calculation. "
            "The runtime supplies the verified program digest. Never choose the runner as a direct ready step."}
    choice = family.get("preferred") if family else None
    selected_entries = []
    for entry in entries:
        if entry["id"] in ordinary:
            inputs = read_capability_index.task_input_schema(entry["input_schema"], choice)
            selected_entries.append({**entry, "input_schema":inputs,
                                     "parameters":{k:v for k,v in entry["parameters"].items()
                                                   if k in inputs.get("properties", entry["parameters"])}})
    if family or implementations:
        context["capabilities"] = selected_entries
    schema = read_output_schema(selected_entries, profile["protocol"], computation)
    if actor is not None:
        context["actor_ref"] = actor
    for attempt in range(2):
        try:
            if saved:
                provider, answer = saved["intent"]["provider"], saved["intent"]["answer"]
            else:
                provider, answer = model_proposal(context, schema, "planner.autonomous_read", diagnostic, trace,
                                                  profile, model_request)
            diagnostic["stage"] = "plan_validation"
            if isinstance(answer, dict) and answer.get("disposition") == "read_compute" and computation:
                if authorization.explicit_motion_request(text):
                    raise failure(Refused, "任务需要控制授权", "planner.control_requires_approval", "deployment")
                if (set(answer) != {"disposition","steps","reason","computation"} or answer["steps"] != []
                        or not isinstance(answer["reason"], str) or not answer["reason"].strip()):
                    raise ValueError("read_compute requires one computation, reason and empty steps")
                gap = answer["computation"]
                validate_instance(computation, gap)
                if not read_authority.matches_actor(actor, caps[site.runner], gap["input"]):
                    raise ValueError("计算的观测源必须使用部署方的本机 actor_ref")
                name = gap["contract"]
                intent = {"provider":provider, "answer":answer, "runner_binding":binding(caps[site.runner]),
                          "contract_hash":read_operators.content_hash(site.contracts[name])}
                journal, task_id = operator_task
                if journal.operator_intent(task_id, intent)["intent"] != intent:
                    raise read_operators.reject("原任务的算子契约或绑定已改变", "operator.binding_denied")
                if not saved and trace:
                    trace("planner.decomposed", {"layer":"planner","module":"planner","provider":provider,
                          "artifact":artifact(answer),"runner":site.runner})
                def author(description, feedback):
                    # The sampler owns image selection, unit scaling and regional statistics.
                    # The author receives only the computational subtask it must implement.
                    question = {"calculation":description, "input":"An ordered vector of already measured scalar values."}
                    if feedback:
                        question["validation_feedback"] = feedback
                    _, candidate = model_proposal(question, read_operators.numeric.AUTHOR_SCHEMA,
                        "operator.author", diagnostic, trace, profile, model_request, read_operators.numeric.PROMPT)
                    return read_operators.numeric.from_author(candidate)
                invocation = site.resolve(gap, journal, task_id, trace, author, intent["runner_binding"])
                final = {"disposition":"ready", "reason":answer["reason"], "steps":[invocation]}
                steps = validate_read_answer(final, {site.runner:caps[site.runner]}, text, actor)
                steps[0]["read_operator"] = name
                answer = final
            else:
                if choice and isinstance(answer, dict) and answer.get("disposition") == "ready":
                    calls = answer.get("steps")
                    if (not isinstance(calls, list) or len(calls) != 1 or not isinstance(calls[0], dict)
                            or set(calls[0]) != {"tool","args"} or calls[0]["tool"] != choice["tool"]):
                        raise ValueError("该契约需要一个已选择的实现和一组调用参数")
                    if "graph_language" in choice:
                        from agent import read_graph_composition
                        arguments = calls[0]["args"]
                        validate_instance(selected_entries[0]["input_schema"], arguments)
                        arguments = read_graph_composition.compose(text, choice, arguments, diagnostic, trace,
                                                                  profile, model_request)
                        answer = {**answer, "steps":[{"tool":choice["tool"], "args":arguments}]}
                steps = validate_read_answer(answer, ordinary, text, actor)
            if trace:
                trace("planner.selected", {"layer":"planner", "module":"planner", "provider":provider,
                                           "artifact":artifact(answer)})
            return provider, steps
        except (ValueError, KeyError, TypeError) as exc:
            feedback = {"code":"planner.invalid_proposal", "message":str(exc)[:480]}
        except (Refused, Unavailable) as exc:
            if getattr(exc, "code", "") not in ("schema_mismatch", "model.schema_mismatch", "transport.invalid_json"):
                raise
            feedback = {"code":"planner.invalid_proposal", "message":"模型返回值不符合输出契约"}
        if attempt:
            raise failure(Refused, feedback["message"], feedback["code"], "planner")
        context = {**context, "validation_feedback":feedback}
        if trace:
            trace("planner.revision", {"layer":"planner", "module":"planner", **feedback})


def build_plan(text, caps, diagnostic, trace=None, *, model_profile=None, model_request=None):
    if trace:
        trace("planner.context",{"layer":"planner","module":"registry",
              "artifact":artifact([dict(item,binding=binding(caps[item["id"]])) for item in catalog(caps)])})
    # Coordinates and action words inside the delimited string belong to the text tool.
    exact = verbatim_step(text, caps)
    if exact:
        cap = caps[exact["tool"]]
        return "local", [{**exact, "version":cap["version"], "binding":binding(cap), "status":"PENDING"}]
    required = requested_outcomes(text,caps)
    available = {key for cap in caps.values() for key in cap.get("outcomes",{})}
    missing = required - available
    if missing:
        raise Refused("当前能力目录缺少任务要求的产物或测量："+"、".join(sorted(missing)))
    steps = local_plan(text,caps)
    provider = "local"
    if steps is None:
        # Navigation by this adapter requires explicit coordinates. Withhold a
        # capability that could never pass validation for the current request.
        points = points_in(text)
        motion = bool(points and MOVE_TERMS.search(text))
        eligible = {key:cap for key,cap in caps.items() if key!="inspect" or motion}
        if motion and not STATUS_TERMS.search(text):
            eligible.pop("status",None)
        step_limit = 16
        if list(eligible)==["inspect"]:
            step_limit = min(16,len(points))
        active_profile = planner_profile() if model_profile is None else model_profile
        schema = response_schema(eligible,points,step_limit,protocol=active_profile.get("protocol"))
        context = {"request":text,"capabilities":catalog(eligible),"requested_points":points}
        diagnostic.update(eligible_tools=list(eligible))
        try:
            provider, answer = model_proposal(context, schema, "planner.system", diagnostic, trace,
                                              active_profile, model_request)
            diagnostic.update(stage="plan_validation",proposal=json.dumps(answer,ensure_ascii=False)[:4096])
            steps=answer["steps"]
            if not steps:
                raise Refused(str(answer.get("reason") or "当前能力无法完成此任务"))
        except (ValueError,KeyError,TypeError) as exc:
            raise Refused("规划器输出缺少合法步骤") from exc
    if not isinstance(steps,list) or not 1 <= len(steps) <= 16:
        raise Refused("计划长度必须在 1–16 步内")
    validated = []
    coordinates = points_in(text)
    planned_coordinates = []
    for step in steps:
        if not isinstance(step,dict) or not isinstance(step.get("tool"),str) or step["tool"] not in caps:
            raise Refused("计划包含未注册能力")
        cap = caps[step["tool"]]
        if cap.get("verbatim_argument"):
            raise failure(Refused, "此工具需要按原文边界提供参数", "tool.verbatim_argument_required", "planner")
        args = step.get("args")
        if not isinstance(args,dict) or set(args) != set(cap["parameters"]):
            raise Refused(f"{cap['id']} 参数与能力契约不一致")
        for name,kind in cap["parameters"].items():
            value = args[name]
            if kind == "xy_list":
                if not isinstance(value,list) or not 1 <= len(value) <= 8:
                    raise Refused("每次巡检需要 1–8 个观测点")
                for pt in value:
                    if (not isinstance(pt,list) or len(pt)!=2 or
                            not all(type(v) in (float,int) and math.isfinite(v) and abs(v)<=4 for v in pt)
                            or pt not in coordinates):
                        raise Refused("巡检点必须由请求明确给出并在 [-4,4] 米内")
                planned_coordinates.extend(value)
            elif not ((kind=="number" and type(value) in (int,float) and math.isfinite(value)) or
                      (kind=="string" and isinstance(value,str)) or (kind=="boolean" and type(value) is bool)):
                raise Refused(f"{name} 类型错误")
        authorization.require_effect_allowed(cap, text)
        if is_series(cap):
            validate_series_input(text, args, cap)
        if cap["id"]=="inspect" and not MOVE_TERMS.search(text):
            raise Refused("请求未明确要求移动或巡检")
        validated.append({"tool":cap["id"],"args":args,"version":cap["version"],"binding":binding(cap),"status":"PENDING"})
    check_points=planned_coordinates or (coordinates and (MOVE_TERMS.search(text) or all(s["tool"] in BASE for s in validated)))
    if check_points and planned_coordinates!=coordinates:
        raise Refused("规划结果遗漏、重复或调整了请求中的坐标顺序")
    covered = {key for step in validated for key in caps[step["tool"]].get("outcomes",{})}
    if required-covered:
        raise Refused("计划缺少能交付所需结果的步骤："+"、".join(sorted(required-covered)))
    if all(s["tool"]=="status" for s in validated):
        if not STATUS_TERMS.search(text) and provider!="local":
            raise Refused("状态测量不足以证明原请求已完成")
        if not re.search(r"多次|两次|几次|\d+\s*次|连续|间隔|重复|repeat|twice|times|interval",text,re.I):
            validated = validated[:1]
    return provider, validated
