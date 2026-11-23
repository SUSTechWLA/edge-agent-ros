"""Trusted DSH adapters for obstacle review. Each graph edge is a frozen service call."""
import hashlib
import json
import math
import re

ROOT = "frame"
OUTPUT = "review"
OPS = {
    "measure_p10":{"input_type":"frame","output_type":"regions","service":"review.measure",
                   "description":"Compute the 10th percentile of valid optical depths per column."},
    "measure_median":{"input_type":"frame","output_type":"regions","service":"review.measure",
                      "description":"Compute the median, the 50th percentile, per column."},
    "classify_near":{"input_type":"regions","output_type":"classified","service":"review.classify",
                     "description":"Mark coverage below 60% unknown, then mark depths at or below the requested threshold."},
    "rank_nearest":{"input_type":"classified","output_type":"review","service":"review.rank",
                    "description":"List near-surface columns in order of increasing optical depth."},
    "rank_image":{"input_type":"classified","output_type":"review","service":"review.rank",
                  "description":"List near-surface columns from left to right, independent of distance."},
}

def require(ok, code):
    if not ok:
        raise ValueError("review."+code)

def digest(obj):
    data=json.dumps(obj,sort_keys=True,separators=(",",":"),ensure_ascii=False,allow_nan=False).encode()
    return "sha256:"+hashlib.sha256(data).hexdigest()

def check_args(args, composed=False):
    keys={"columns","distance_m","graph"} if composed else {"columns","distance_m"}
    require(isinstance(args,dict) and set(args)==keys,"invalid_arguments")
    require(type(args["columns"]) is int and 1<=args["columns"]<=8,"invalid_columns")
    require(type(args["distance_m"]) in (int,float) and .1<=args["distance_m"]<=8,"invalid_distance")

def validate_graph(graph):
    require(isinstance(graph,dict) and set(graph)=={"nodes","output"} and
            isinstance(graph["nodes"],list) and 1<=len(graph["nodes"])<=8,"invalid_graph")
    types={ROOT:"frame"}
    parents={}
    for node in graph["nodes"]:
        require(isinstance(node,dict) and set(node)=={"id","input","op"},"invalid_node")
        name,src,op=node["id"],node["input"],node["op"]
        require(isinstance(name,str) and re.fullmatch("[a-z][a-z0-9_]{0,23}",name) and name not in types,
                "invalid_node_id")
        require(isinstance(op,str) and op in OPS and isinstance(src,str) and src in types and
                OPS[op]["input_type"]==types[src],"incompatible_node")
        parents[name]=src
        types[name]=OPS[op]["output_type"]
    out=graph["output"]
    require(isinstance(out,str) and types.get(out)==OUTPUT,"invalid_graph_output")
    used=set()
    while out!=ROOT:
        used.add(out)
        out=parents[out]
    require(len(used)==len(parents),"unused_node")
    return graph

def measured(analysis, frame, columns, statistic):
    require(isinstance(analysis,dict) and analysis.get("frame_ref")==frame and analysis.get("grid")==[1,columns]
            and analysis.get("channel")=="depth" and analysis.get("unit")=="m"
            and analysis.get("statistic")==statistic and len(analysis.get("regions",[]))==columns,
            "measurement_mismatch")
    width,height=analysis["width"],analysis["height"]
    require(type(width) is int and type(height) is int and columns<=width<=640 and 1<=height<=480,"bad_dimensions")
    for i,region in enumerate(analysis["regions"]):
        left,right=i*width//columns,(i+1)*width//columns
        require(region.get("bounds_xyxy")==[left,0,right,height] and region.get("pixels")==height*(right-left),
                "bad_geometry")
        count,value=region["valid_pixels"],region["value"]
        require(type(count) is int and 0<=count<=region["pixels"] and
                ((count==0 and value is None) or
                 (count>0 and type(value) in (int,float) and math.isfinite(value) and value>0)),"bad_measurement")
    return analysis

def classify(analysis, distance):
    regions=[]
    for index, region in enumerate(analysis["regions"],1):
        count,total,depth=region["valid_pixels"],region["pixels"],region["value"]
        if count*5<total*3 or depth is None:
            state="unknown"
        elif depth<=distance or math.isclose(depth,distance,abs_tol=1e-12,rel_tol=0):
            state="near_surface"
        else:
            state="above_threshold"
        regions.append({"column":index,"state":state,"depth_m":depth,"pixels":total,
                        "valid_pixels":count,"valid_fraction":count/total})
    return {"statistic":analysis["statistic"],"distance_m":distance,"column_count":len(regions),
            "scope":"camera_optical_depth_only","regions":regions,
            "unknown_columns":[r["column"] for r in regions if r["state"]=="unknown"]}

def ranked(classified, order):
    require(order in ("nearest_first","image_order"),"bad_order")
    candidates=[r for r in classified["regions"] if r["state"]=="near_surface"]
    if order=="nearest_first":
        candidates.sort(key=lambda region:(region["depth_m"],region["column"]))
    return {**classified,"order":order,"review_order":[r["column"] for r in candidates]}

def verify_goal(analysis, review, columns, limit, frame):
    """Verify the requested outcome against the pinned measurement, including missing coverage."""
    measured(analysis,frame,columns,"p10")
    require(isinstance(review,dict) and review.get("column_count")==columns and review.get("distance_m")==limit
            and review.get("statistic")=="p10" and review.get("scope")=="camera_optical_depth_only"
            and review.get("order")=="nearest_first" and isinstance(review.get("regions"),list)
            and len(review["regions"])==columns,"goal_not_satisfied")
    candidates=[]
    unknown=[]
    for index,(row,observation) in enumerate(zip(analysis["regions"],review["regions"]),1):
        value,count,total=row["value"],row["valid_pixels"],row["pixels"]
        fraction=count/total
        require(isinstance(observation,dict) and observation.get("column")==index and observation.get("pixels")==total
                and observation.get("valid_pixels")==count and observation.get("depth_m")==value
                and observation.get("valid_fraction")==fraction,"invalid_region_evidence")
        if fraction<.6 or value is None:
            unknown.append(index)
            expected="unknown"
        elif value-limit<=1e-12:
            expected="near_surface"
            candidates.append((value,index))
        else:
            expected="above_threshold"
        require(observation.get("state")==expected,"wrong_classification")
    require(review.get("unknown_columns")==unknown and review.get("review_order")==[i for _,i in sorted(candidates)],
            "goal_not_satisfied")

async def called(service, payload):
    response=await service(payload)
    require(isinstance(response,(list,tuple)) and len(response)==2 and response[0]==200
            and isinstance(response[1],dict),"service_unavailable")
    return response[1]

async def evaluate_graph(graph, args, frame, services):
    graph=validate_graph(graph)
    values={ROOT:{"frame_ref":frame,"columns":args["columns"]}}
    receipts=[]
    analysis=None
    for node in graph["nodes"]:
        op=node["op"]
        definition=OPS[op]
        service=definition["service"]
        previous=values[node["input"]]
        if service=="review.measure":
            stat="p10" if op=="measure_p10" else "median"
            payload={**previous,"statistic":stat}
            result=await called(services[service],payload)
            analysis=measured(result,frame,args["columns"],stat)
        elif service=="review.classify":
            payload={"analysis":previous,"distance_m":args["distance_m"]}
            result=await called(services[service],payload)
        else:
            payload={"classified":previous,"order":"nearest_first" if op=="rank_nearest" else "image_order"}
            result=await called(services[service],payload)
        values[node["id"]]=result
        receipts.append({**node,"service":service,"input_type":definition["input_type"],"output_type":definition["output_type"],
                         "input_sha256":digest(payload),"output_sha256":digest(result)})
    review=values[graph["output"]]
    # The exported Tool promises increasing P10 distance. Other recipes are valid for different outcomes.
    verify_goal(analysis,review,args["columns"],args["distance_m"],frame)
    return {"frame_ref":frame,"implementation":"agent_graph","analysis":analysis,"review":review,
            "graph":graph,"graph_receipts":receipts,"graph_sha256":digest(graph)}

def success_or_blocked(operation, evidence=None, reason=None):
    if reason is not None:
        return {"operation_id":operation,"state":"BLOCKED","error":{"code":reason,"message":reason}}
    return {"operation_id":operation,"state":"SUCCEEDED","evidence":evidence}

def apply(ctx):
    config=ctx.config()
    profile=config["profile"]
    name=profile.get("service") or profile["legacy"]["id"]
    if name=="review.classify":
        async def region_handler(request):
            return [200,classify(request["analysis"],request["distance_m"])]
        ctx.provide(name,region_handler)
        return
    if name=="review.rank":
        async def rank_handler(request):
            return [200,ranked(request["classified"],request["order"])]
        ctx.provide(name,rank_handler)
        return

    require(name in ("review_builtin","review_composed"),"unknown_export")
    composed=name=="review_composed"
    needed=("review.measure","review.classify","review.rank") if composed else ("review.measure",)
    services={key:ctx.require(key) for key in needed}
    frame=config["settings"]["frame_ref"]
    async def invoke(wire):
        try:
            args=wire["arguments"]
            check_args(args,composed)
            if composed:
                evidence=await evaluate_graph(args["graph"],args,frame,services)
            else:
                payload={"frame_ref":frame,"columns":args["columns"],"statistic":"p10"}
                raw=await called(services["review.measure"],payload)
                analysis=measured(raw,frame,args["columns"],"p10")
                review=ranked(classify(analysis,args["distance_m"]),"nearest_first")
                verify_goal(analysis,review,args["columns"],args["distance_m"],frame)
                evidence={"frame_ref":frame,"implementation":"builtin","analysis":analysis,"review":review}
            return success_or_blocked(wire["operation_id"],evidence=evidence)
        except (KeyError,ValueError,TypeError) as error:
            code=str(error) if str(error).startswith("review.") else "review.invalid_receipt"
            return success_or_blocked(wire["operation_id"],reason=code)
    ctx.tools.register(name,invoke)
