"""First-party Cordis-style adapter for caller-supplied series (SDD-045)."""
from derived_tools import capability, evaluate


def apply(ctx):
    legacy = ctx.config()["profile"]["legacy"]
    definition = legacy["derived"]
    if definition.get("schema_version") != 2 or capability(definition) != legacy:
        raise ValueError("series provider needs the exact reviewed caller-series capability")

    def invoke(request):
        operation = request.get("operation_id")
        if not isinstance(operation, str) or not operation:
            raise ValueError("missing operation ID")
        evidence = evaluate(definition, request["arguments"])
        return {"operation_id":operation, "state":"SUCCEEDED",
                "evidence":{**evidence, "definition_version":legacy["version"]}}

    ctx.tools.register(legacy["id"], invoke)
