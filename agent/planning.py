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

from edge_contracts.descriptor import validate_instance
from agent import read_operators
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


def response_schema(caps, points, step_limit):
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
    return {"type":"object","properties":{"steps":{"type":"array","maxItems":step_limit,"items":{"oneOf":variants}},
            "reason":{"type":"string"}},"required":["steps","reason"],"additionalProperties":False}


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
        if not isinstance(profile,dict) or set(profile)-{"schema_version","version","protocol","url","model","options","timeout_s"}:
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
        elif protocol!="http-planner" or "model" in profile or "options" in profile:
            raise ValueError()
        return profile
    except FileNotFoundError:
        plugin=os.getenv("EDGE_PLANNER_URL")
        return {"version":"env","protocol":"http-planner" if plugin else "ollama",
                "url":plugin or os.getenv("EDGE_OLLAMA_URL"),"model":os.getenv("EDGE_OLLAMA_MODEL","qwen2.5:1.5b"),
                "options":{"temperature":0,"num_ctx":4096}}
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
    module = "operator-author" if prompt_text is not None else "planner"
    timeout = profile.get("timeout_s", 90)
    if plugin:
        body = {"schema_version":1, **context, "output_schema":schema}
        if prompt_key != "planner.system":
            body["instructions"] = prompt_text or contract("prompts", prompt_key)["text"]
        answer = exchange(trace, "model", module, body, lambda:request(url, body, timeout),
                          meta={"provider":provider, "protocol":"planner.http.v1", "profile":revision})
    else:
        prompt = (contract("prompts", prompt_key) if prompt_text is None else
                  {"id":prompt_key,"text":prompt_text,"version":read_operators.numeric.LANGUAGE,
                   "sha256":hashlib.sha256(prompt_text.encode()).hexdigest()})
        body = {"model":profile["model"], "stream":False, "format":schema, "options":profile["options"],
                "messages":[{"role":"system", "content":prompt["text"]},
                            {"role":"user", "content":json.dumps(context, ensure_ascii=False)}]}
        reply = exchange(trace, "model", module, body, lambda:request(url.rstrip("/") + "/api/chat", body, timeout),
                         meta={"provider":provider, "protocol":"ollama.chat", "profile":revision,
                               "prompt":{k:prompt[k] for k in ("id","version","sha256")}})
        answer = json.loads(reply["message"]["content"], parse_constant=finite_number, parse_float=finite_number)
    return provider, answer


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
        return branch(["ready","requires_control","unsupported"], {"type":"array", "items":entry, "maxItems":16})
    choices = [step([item["id"]], item["input_schema"]) for item in entries]
    ready = branch(["ready"], {"type":"array", "items":{"oneOf":choices}, "minItems":1, "maxItems":16})
    denied = branch(["requires_control","unsupported"], {"type":"array", "maxItems":0})
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
            or not answer["reason"].strip() or answer["disposition"] not in ("ready","requires_control","unsupported")):
        raise ValueError("需要 disposition、steps 和非空 reason")
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


def autonomous_read_plan(text, caps, diagnostic, trace, model_profile, model_request, operator_task=None):
    entries = [dict(item, input_schema=autonomous_argument_schema(caps[item["id"]]),
                    binding=binding(caps[item["id"]])) for item in catalog(caps)]
    if trace:
        trace("planner.context", {"layer":"planner", "module":"registry", "authority":"deployment.read_only",
                                 "artifact":artifact(entries)})
    diagnostic["eligible_tools"] = list(caps)
    if not caps:
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
    if site and site.runner in caps and operator_task:
        input_schema = site.input_schema(caps[site.runner])
        computation = {"type":"object","properties":{"contract":{"type":"string","enum":list(site.contracts)},
                       "input":input_schema},"required":["contract","input"],"additionalProperties":False}
        context["numeric_computation"] = {"runner":site.runner,"input_schema":input_schema,"contracts":site.describe(),
            "instructions":"For a requested numeric result that requires combining sensor measurements, choose "
            "read_compute, the matching mathematical contract, and the input measurement. This path also reuses "
            "installed operators. A sensor measurement alone cannot deliver an additional calculation. "
            "The runtime supplies the verified program digest. Never choose the runner as a direct ready step."}
    schema = read_output_schema([c for c in entries if c["id"] in ordinary], profile["protocol"], computation)
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
                    question = {"request":text,"calculation":description}
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
        schema = response_schema(eligible,points,step_limit)
        context = {"request":text,"capabilities":catalog(eligible),"requested_points":points}
        diagnostic.update(eligible_tools=list(eligible))
        try:
            provider, answer = model_proposal(context, schema, "planner.system", diagnostic, trace,
                                              model_profile, model_request)
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
