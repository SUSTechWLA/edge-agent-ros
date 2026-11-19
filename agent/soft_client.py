"""Legacy soft-host client and catalog takeover policy."""
if not __package__:
    import _contract_path  # Support direct script entry points.
from edge_contracts.soft_transport import LIMIT, SoftError, enabled, encode, request, request_at


def can_take_over(previous, candidate):
    if previous != candidate.get("_legacy_contract") or previous.get("effect") != "read":
        return False
    if previous.get("derived", {}).get("schema_version") == 2 and not previous.get("endpoint"):
        return True
    name = previous["id"]
    return (name in ("read_session","evidence_schema","query_evidence") and
            previous.get("endpoint") == "http://evidence:8125/invoke?tool=" + name)


def catalog(legacy, view=None):
    if view is None:
        view = request("catalog")
    # Tombstones continue to own a migrated name after removal. Only explicitly
    # authorized v1 handoffs can shadow an installed legacy tool.
    tools = {name: cap for name, cap in legacy.items() if name not in view["claims"]}
    for cap in view["tools"]:
        previous = legacy.get(cap["id"])
        if previous is None or can_take_over(previous, cap):
            tools[cap["id"]] = cap
    return tools
