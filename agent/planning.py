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
from tracing import CODE, artifact, contract, exchange

NUMBER = r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)"
PAIR = re.compile(rf"\(\s*({NUMBER})\s*,\s*({NUMBER})\s*\)")
BASE = {
    "inspect": {"id":"inspect", "description":"依次到明确给出的平面坐标停稳并记录位置", "version":"1", "parameters":{"points":"xy_list"}, "effect":"idempotent"},
    "status": {"id":"status", "description":"读取本机当前里程计位置、朝向和速度", "version":"1", "parameters":{}, "effect":"read"},
}

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


class Unavailable(Exception):
    """Transport or provider is temporarily unavailable."""


class Refused(Exception):
    """Unfulfillable task or terminal adapter failure."""


class OutcomeUnknown(Exception):
    """A dispatched operation lacks a trustworthy terminal receipt."""


def failure(cls, message, code, origin, details=None):
    exc=cls(str(message))
    marker=re.search(r"\[code:([a-zA-Z][\w.-]{0,95})\]\s*$",str(message))
    exc.code=marker.group(1) if marker else code
    exc.origin=origin
    exc.details=details
    return exc


def remote_failure(cls, reply, fallback):
    error=reply.get("error",{})
    if not isinstance(error,dict):
        error={}
    code=error.get("code")
    code=code if isinstance(code,str) and CODE.fullmatch(code) else fallback
    return failure(cls,error.get("message") or reply.get("detail") or fallback,code,"provider",error.get("details"))


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
        err=remote_failure(cls,reply if isinstance(reply,dict) else {},"transport.http_"+str(exc.code))
        err.args=(f"adapter HTTP {exc.code}: {err}",)
        raise err from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise failure(Unavailable,f"adapter unavailable: {type(exc).__name__}","transport.unavailable","transport") from exc
    except (ValueError, UnicodeError) as exc:
        raise failure(Refused,"invalid adapter JSON","transport.invalid_json","provider") from exc


def registry():
    caps = dict(BASE)
    loaded = set()
    dirs=[Path(os.getenv("EDGE_PLUGIN_DIR", "/etc/edge/plugins"))]
    extra=os.getenv("EDGE_EXTRA_PLUGIN_DIR")
    if extra:
        dirs.append(Path(extra))
    for path in (p for directory in dirs for p in sorted(directory.glob("*.json"))):
        try:
            item = json.loads(path.read_text(encoding="utf-8-sig"))
            key = item["id"]
            parameters = item["parameters"]
            outcomes = item.get("outcomes",{})
            terms = item.get("outcome_terms",{})
            utterances=item.get("utterances",[])
            url = urlsplit(item["endpoint"])
            compatible = (key not in BASE or
                          (parameters == BASE[key]["parameters"] and item["effect"] == BASE[key]["effect"]))
            valid = (re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", key) and key not in loaded and compatible
                     and isinstance(item["version"],str) and item["version"] and isinstance(item["description"],str)
                     and url.scheme in ("http","https") and url.hostname and not url.username and not url.password
                     and item["effect"] in ("read","idempotent")
                     and isinstance(parameters,dict)
                     and isinstance(utterances,list) and all(valid_utterance(p,parameters) for p in utterances)
                     and isinstance(outcomes,dict) and all(isinstance(k,str) and isinstance(v,str) and v
                                                         for k,v in outcomes.items())
                     and isinstance(terms,dict) and all(k in outcomes and isinstance(words,list)
                          and all(isinstance(w,str) and w for w in words) for k,words in terms.items())
                     and (key == "inspect" or all(v in ("string","number","boolean") for v in parameters.values())))
            if valid:
                loaded.add(key)
                caps[key] = item
            else:
                print(f"Invalid capability manifest {path.name}", flush=True)
        except (ValueError, TypeError, KeyError, OSError) as exc:
            print(f"Invalid capability manifest {path.name}: {type(exc).__name__}", flush=True)
    return caps


def catalog(caps):
    return [{k:v for k,v in item.items() if k in ("id","version","description","parameters","effect","outcomes")}
            for item in caps.values()]


def binding(cap):
    fields = {key:cap.get(key) for key in ("id","version","parameters","effect","endpoint")}
    if cap.get("outcomes"):
        fields["outcomes"] = cap["outcomes"]
    return hashlib.sha256(json.dumps(fields,sort_keys=True).encode()).hexdigest()


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


def valid_utterance(pattern, parameters):
    if len(parameters)!=1 or next(iter(parameters.values()))!="string" or not isinstance(pattern,str):
        return False
    marker="{"+next(iter(parameters))+"}"
    return (pattern.count(marker)==1 and pattern.count("{")==pattern.count("}")==1
            and len(marker)<len(pattern)<=256)


def match_utterances(request,caps):
    matches=[]
    for cap in caps.values():
        for pattern in cap.get("utterances",[]):
            name=next(iter(cap["parameters"]))
            prefix,suffix=normalized(pattern).split("{"+name+"}")
            if request.startswith(prefix) and request.endswith(suffix):
                arg=request[len(prefix):len(request)-len(suffix)].strip()
                if arg:
                    step={"tool":cap["id"],"args":{name:arg}}
                    if step not in matches:
                        matches.append(step)
    if len(matches)>1:
        raise Refused("多个能力可匹配这条指令，请明确子目标")
    return matches or None


def local_plan(text, caps):
    request = normalized(text)
    if re.fullmatch(r"(?:请)?(?:报告|查询|查看|读取|告诉我)?(?:一下)?(?:机器人|本机)?(?:当前)?(?:的位置|位置|状态|位姿)|status",request,re.I):
        return [{"tool":"status","args":{}}]
    for item in caps.values():
        if item["id"] not in BASE and not item["parameters"] and request in (item["id"],item["description"]):
            return [{"tool":item["id"],"args":{}}]
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
    return match_utterances(request,caps)


def plan(text, caps, trace=None):
    diagnostic = {"stage":"capability_check"}
    try:
        return build_plan(text,caps,diagnostic,trace)
    except (Refused,Unavailable) as exc:
        exc.diagnostic = diagnostic
        raise


def planner_profile():
    """Read once per planning attempt. Invalid explicit configuration fails closed."""
    path=Path(os.getenv("EDGE_CONTRACT_DIR","/etc/edge/contracts"))/"planner-profile.json"
    try:
        with path.open("rb") as stream:
            raw=stream.read(16385)
        if len(raw)>16384:
            raise ValueError()
        profile=json.loads(raw)
        if not isinstance(profile,dict) or set(profile)-{"schema_version","version","protocol","url","model","options"}:
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
            if not isinstance(options,dict) or set(options)-{"temperature","num_ctx"}:
                raise ValueError()
            temp=options.get("temperature",0)
            ctx=options.get("num_ctx",4096)
            if type(temp) not in (int,float) or not 0<=temp<=2 or type(ctx) is not int or not 128<=ctx<=65536:
                raise ValueError()
            profile["options"]={"temperature":temp,"num_ctx":ctx}
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


def build_plan(text, caps, diagnostic, trace=None):
    if trace:
        trace("planner.context",{"layer":"planner","module":"registry",
              "artifact":artifact([dict(item,binding=binding(caps[item["id"]])) for item in catalog(caps)])})
    required = requested_outcomes(text,caps)
    available = {key for cap in caps.values() for key in cap.get("outcomes",{})}
    missing = required - available
    if missing:
        raise Refused("当前能力目录缺少任务要求的产物或测量："+"、".join(sorted(missing)))
    steps = local_plan(text,caps)
    provider = "local"
    if steps is None:
        profile=planner_profile()
        url=profile["url"]
        plugin=profile["protocol"]=="http-planner"
        revision={"version":profile["version"],"sha256":hashlib.sha256(
            json.dumps(profile,sort_keys=True,ensure_ascii=False).encode()).hexdigest()}
        if not url:
            raise failure(Refused,"本地策略无法解析这项请求。可输入明确坐标巡检、查询状态，或连接模型规划器并注册所需能力。",
                          "planner.not_configured","planner")
        model = profile.get("model")
        provider = f"ollama/{model}"
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
        diagnostic.update(stage="planner",provider="http-planner" if plugin else provider,
                          eligible_tools=list(eligible))
        try:
            if plugin:
                provider = "http-planner"
                body={"schema_version":1,**context,"output_schema":schema}
                answer=exchange(trace,"model","planner",body,lambda:http_json(url,body,90),
                                meta={"provider":provider,"protocol":"planner.http.v1","profile":revision})
            else:
                prompt=contract("prompts","planner.system")
                body={"model":model,"stream":False,"format":schema,"options":profile["options"],
                      "messages":[{"role":"system","content":prompt["text"]},
                                  {"role":"user","content":json.dumps(context,ensure_ascii=False)}]}
                reply=exchange(trace,"model","planner",body,lambda:http_json(url.rstrip("/")+"/api/chat",body,90),
                               meta={"provider":provider,"protocol":"ollama.chat","profile":revision,
                                     "prompt":{k:prompt[k] for k in ("id","version","sha256")}})
                answer=json.loads(reply["message"]["content"], parse_constant=finite_number, parse_float=finite_number)
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
        if cap["effect"]!="read" and re.search(r"不要|禁止|别去|别动|不能|不得|不许|\b(?:not|never|unless|if)\b|如果|除非",text,re.I):
            raise Refused("含否定或条件的执行请求需要明确后再派发")
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
