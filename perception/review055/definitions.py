"""SDD-055 station exports and the public language for bounded single-input dataflow nodes."""
from perception.review055.adapters import OPS, ROOT, OUTPUT

def object_schema(properties, required=None):
    return {"type":"object", "properties":properties, "additionalProperties":False,
            "required":list(properties) if required is None else required}

NODE_ID = {"type":"string","minLength":1,"maxLength":24}
GRAPH_SCHEMA = object_schema({
    "nodes":{"type":"array","minItems":1,"maxItems":8,"items":object_schema({
        "id":NODE_ID, "op":{"type":"string","enum":list(OPS)}, "input":NODE_ID})},
    "output":NODE_ID,
})
GRAPH_LANGUAGE = {"root":ROOT, "root_type":"frame", "output_type":OUTPUT, "operations":OPS}
FIELDS = {
    "columns":{"type":"integer","minimum":1,"maximum":8,"description":"Number of image columns, left to right."},
    "distance_m":{"type":"number","minimum":0.1,"maximum":8,"description":"Inclusive optical-axis depth threshold in metres."},
}
CALL_SCHEMA = object_schema(FIELDS)
COMPOSE_SCHEMA = object_schema({**FIELDS,"graph":GRAPH_SCHEMA})
FRAME = {"type":"string","minLength":71,"maxLength":71}
STATUS = {"type":"object"}
TOOL_OUTCOME = {"type":"object","required":["frame_ref","analysis","review","implementation"],
                "properties":{"frame_ref":FRAME,"analysis":STATUS,"review":STATUS,
                              "implementation":{"type":"string"},"graph_receipts":{"type":"array"}}}

def service(pid,name,params,description,**impl):
    return {"id":"review055."+pid, "kind":"service", "name":"review."+name, "description":description,
            "parameters":params, "outcomes":STATUS, "requires":[], **impl}

def tool(pid,params,deps,description,**impl):
    return {"id":"review055."+pid, "kind":"tool", "name":"review_"+pid, "description":description,
            "parameters":params, "outcomes":TOOL_OUTCOME, "requires":deps, **impl}

INPROC = {"runtime":"inproc","module":"perception.review055.adapters"}
SPECS = [
    service("measure","measure",object_schema({"frame_ref":FRAME,"columns":FIELDS["columns"],
        "statistic":{"type":"string","enum":["p10","median"]}}),
        "Read the pinned frame and measure valid optical depth.",runtime="process",
        endpoint="http://review-worker:8080/measure",probe="http://review-worker:8080/health"),
    service("classify","classify",object_schema({"analysis":STATUS,"distance_m":FIELDS["distance_m"]}),
        "Classify near surfaces, above-threshold regions and unknown regions.",**INPROC),
    service("rank","rank",object_schema({"classified":STATUS,"order":{"type":"string","enum":["nearest_first","image_order"]}}),
        "Order measured near-surface candidates.",**INPROC),
    tool("builtin",CALL_SCHEMA,["review.measure"],"内置插件，按有效深度P10、覆盖率与距离阈值给出人工复核次序。",**INPROC),
    tool("workflow",CALL_SCHEMA,[],"站点用户工作流，按有效深度P10、覆盖率与距离阈值给出人工复核次序。",
         runtime="process", endpoint="http://review-workflow:8080/invoke",probe="http://review-workflow:8080/health"),
    tool("composed",COMPOSE_SCHEMA,["review.measure","review.classify","review.rank"],
         "Agent 自行连接已授权的测量、分类和排序原子服务，给出近距表面证据及人工复核次序。",**INPROC),
]
