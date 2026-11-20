"""Validate the object-schema vocabulary used by the planner and existing tool authors."""
import math

TYPES = {"object":dict, "array":list, "string":str, "integer":int, "number":(int,float),
         "boolean":bool, "null":type(None)}
KEYS = {"type","properties","required","additionalProperties","items","oneOf","enum",
        "minItems","maxItems","minLength","maxLength","minimum","maximum","const","description","title","unit"}


def current_schema(schema, depth=0):
    if depth>24 or not isinstance(schema,dict) or set(schema)-KEYS:
        return False
    if "type" in schema and (not isinstance(schema["type"],str) or schema["type"] not in TYPES):
        return False
    for field in ("minItems","maxItems","minLength","maxLength"):
        if field in schema and (type(schema[field]) is not int or schema[field]<0):
            return False
    for field in ("minimum","maximum"):
        if field in schema and not (type(schema[field]) is int or
                                    type(schema[field]) is float and math.isfinite(schema[field])):
            return False
    if any(field in schema and not isinstance(schema[field], str) for field in ("description","title","unit")):
        return False
    if "enum" in schema and (not isinstance(schema["enum"],list) or not schema["enum"]):
        return False
    if "additionalProperties" in schema and type(schema["additionalProperties"]) is not bool:
        return False
    props = schema.get("properties",{})
    required = schema.get("required",[])
    if not isinstance(props,dict) or not isinstance(required,list) or not all(isinstance(k,str) for k in required):
        return False
    children = list(props.values())
    if "items" in schema:
        children.append(schema["items"])
    if "oneOf" in schema:
        options = schema["oneOf"]
        if not isinstance(options,list) or not 1<=len(options)<=128:
            return False
        children.extend(options)
    return all(current_schema(child,depth+1) for child in children)


def matches(value, schema, budget):
    budget[0] -= 1
    if budget[0] < 0:
        raise ValueError("schema budget")
    typ = schema.get("type")
    if typ and (not isinstance(value,TYPES[typ]) or type(value) is bool and typ in ("number","integer")):
        return False
    if "enum" in schema or "const" in schema:
        def equal(x,y):
            # JSON booleans differ from numbers, while integer and float may be equal.
            if type(x) in (int,float) and type(y) in (int,float):
                return x==y
            if isinstance(x,list) and isinstance(y,list):
                return len(x)==len(y) and all(equal(a,b) for a,b in zip(x,y))
            if isinstance(x,dict) and isinstance(y,dict):
                return x.keys()==y.keys() and all(equal(x[k],y[k]) for k in x)
            return type(x) is type(y) and x==y
        if "const" in schema and not equal(value, schema["const"]):
            return False
        if "enum" in schema and not any(equal(value,option) for option in schema["enum"]):
            return False
    if "oneOf" in schema and sum(matches(value,item,budget) for item in schema["oneOf"])!=1:
        return False
    if isinstance(value,dict):
        props=schema.get("properties",{})
        if any(k not in value for k in schema.get("required",[])):
            return False
        if schema.get("additionalProperties") is False and set(value)-props.keys():
            return False
        return all(matches(value[k],props[k],budget) for k in value if k in props)
    if isinstance(value,list):
        if not schema.get("minItems",0)<=len(value)<=schema.get("maxItems",len(value)):
            return False
        return "items" not in schema or all(matches(v,schema["items"],budget) for v in value)
    if isinstance(value,str):
        return schema.get("minLength",0)<=len(value)<=schema.get("maxLength",len(value))
    if type(value) in (int,float):
        return schema.get("minimum",value) <= value <= schema.get("maximum",value)
    return True
