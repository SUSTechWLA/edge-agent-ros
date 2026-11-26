"""Compile a per-task read-only graph against a site-supplied, typed operation catalog."""
import re
from edge_contracts.descriptor import validate_instance
from edge_contracts.errors import Refused, failure


INSTRUCTIONS = (
    "Design a small executable graph for the user's requested outputs. Choose operations only from the catalog "
    "according to their stated semantics. Use the requested statistic, missing-data behavior and ranking. "
    "Start with the supplied root input and use the actual output from an earlier node as each subsequent "
    "input. Node ids must be unique. The output is the id of the final node and every node must contribute "
    "to it. Return only the graph. The graph contains no routes, device access or execution controls."
)


def graph_schema(operations):
    node_id = {"type":"string", "minLength":1, "maxLength":24}
    node = {"type":"object", "required":["id","op","input"], "additionalProperties":False,
            "properties":{"id":node_id, "op":{"type":"string","enum":sorted(operations)},
                          "input":node_id}}
    return {"type":"object","required":["nodes","output"],"additionalProperties":False,
            "properties":{"nodes":{"type":"array","minItems":1,"maxItems":8,"items":node},"output":node_id}}


def validate_graph(graph, language):
    ops = language["operations"]
    root = language["root"]
    validate_instance(graph_schema(ops), graph)
    types = {root:language["root_type"]}
    parents = {}
    for node in graph["nodes"]:
        name, source, op = node["id"], node["input"], ops[node["op"]]
        if (not re.fullmatch("[a-z][a-z0-9_]{0,23}",name) or name in types
                or source not in types or types[source] != op["input_type"]):
            raise ValueError("a node has a duplicate id, unavailable input or incompatible type")
        parents[name] = source
        types[name] = op["output_type"]
    out = graph["output"]
    if out not in parents or types[out] != language["output_type"]:
        raise ValueError("the graph does not return the requested output type")
    used = set()
    while out != root:
        used.add(out)
        out = parents[out]
    if used != parents.keys():
        raise ValueError("the graph contains unused nodes")


def compose(text, entry, args, diagnostic, trace, model_profile, model_request):
    from agent.planning import model_proposal
    language = entry["graph_language"]
    ops = language["operations"]
    context = {"request":text,"arguments":args, "root":language["root"], "root_type":language["root_type"],
               "required_output_type":language["output_type"],
               "operations":{name:{k:op[k] for k in ("description","input_type","output_type")}
                             for name,op in ops.items()}}
    schema = graph_schema(ops)
    # Validation feedback is bounded; a bad proposal never becomes a tool call.
    for attempt in (1,2):
        provider, graph = model_proposal(context,schema,"planner.compose_read_graph",diagnostic,trace,
                                        model_profile,model_request,INSTRUCTIONS)
        try:
            validate_graph(graph,language)
        except (ValueError,KeyError,TypeError) as exc:
            reason = str(exc)[:240]
            if trace:
                trace("planner.graph_rejected",{"layer":"planner","module":"planner","attempt":attempt,
                                                "provider":provider,"reason":reason})
            context["previous_validation_error"] = reason
            if attempt == 2:
                raise failure(Refused,"模型组合的只读工具图未通过契约校验","planner.invalid_proposal","planner") from exc
        else:
            if trace:
                trace("planner.graph_composed",{"layer":"planner","module":"planner","provider":provider,
                                                "attempt":attempt,"graph":graph})
            return {**args,"graph":graph}
