"""Run the persistent planning, dispatch and verification queues."""
from functools import partial
import os
import time

from agent.tracing import diagnosis, exchange
from edge_contracts.descriptor import binding
from edge_contracts.errors import OperationCanceled, OutcomeUnknown, Refused, Unavailable, failure
from edge_contracts.soft_transport import SoftError
from . import read_authority
from .authorization import retry_exhausted, step_timed_out
from .catalog import is_series
from .verification import finish_step, prepare_verification, save_candidate, verify_candidate


def task_plan(journal, task, trace, *, registry, planner, read_only_planner, freeze):
    # Persist the exact proposal before crossing the socket. A lost reply can
    # then replay this request without a second LLM call or an alternate route.
    candidate = journal.soft_intent(task["id"])
    authority = read_authority.load()
    pin = (task.get("trigger") or {}).get("read_only_tool")
    if candidate is None:
        if (hasattr(journal, "operator_intent") and journal.operator_intent(task["id"])
                and (authority is None or "EDGE_READ_OPERATOR_POLICY" not in os.environ)):
            raise failure(Refused, "原任务的数值算子策略已撤销", "operator.binding_denied", "deployment")
        try:
            caps = registry()
        except Unavailable:
            # Reserved local status remains usable during a soft-host outage.
            # Restrict this fallback to a direct, read-only kernel request.
            if authority is not None or task["text"].strip().lower() != "status" or (task.get("trigger") or {}).get("read_only_tool"):
                raise
            legacy = registry(legacy_only=True)
            if legacy["status"].get("endpoint"):
                raise
            caps = {"status":legacy["status"]}
        if authority is not None and pin:
            cap = caps.get(pin["id"])
            if (not cap or not read_authority.admitted(authority, pin["id"], cap, cap.get("_soft"))
                    or pin.get("binding") != binding(cap)):
                raise failure(Refused, "续办任务没有获批的只读软件绑定", "planner.read_binding_denied", "deployment")
            provider, steps = planner(task["text"], {pin["id"]:cap}, trace)
        elif pin:
            provider, steps = read_only_planner(task["text"],caps,pin)
        elif authority is not None and "EDGE_READ_OPERATOR_POLICY" in os.environ:
            provider, steps = planner(task["text"], caps, trace, operator_task=(journal, task["id"]))
        else:
            provider, steps = planner(task["text"],caps,trace)
        managed = False
        for step in steps:
            cap = caps[step["tool"]]
            for track in ("physical","soft"):
                if "_" + track in cap:
                    step[track + "_pin"] = cap["_" + track]
                    step[track + "_cap"] = {k:v for k,v in cap.items() if k not in ("_"+track,"_legacy_contract")}
                    managed = True
        read_authority.require_steps(authority, steps)
        if not managed:
            return provider, steps
        candidate = journal.soft_intent(task["id"], [provider, steps])
    read_authority.require_steps(read_authority.load(), candidate[1])
    if authority is not None and pin and any(
            s["tool"] != pin["id"] or s["binding"] != pin.get("binding") for s in candidate[1]):
        raise failure(Refused, "续办任务超出原授权的工具范围", "planner.read_binding_denied", "deployment")
    result = freeze(task["id"],candidate)
    try:
        read_authority.require_steps(read_authority.load(), result[1])
    except Refused as error:
        error.unissued_pins = [s["soft_pin"] for s in result[1] if "soft_pin" in s]
        raise
    if authority is not None:
        expected = [(s["tool"], s["args"], s["binding"], s["version"]) for s in candidate[1]]
        frozen = [(s["tool"], s["args"], s["binding"], s["version"]) for s in result[1]]
        if result[0] != candidate[0] or frozen != expected or any(
                s["soft_pin"].get("reservation_id") != f"{task['id']}/step/{i}"
                for i,s in enumerate(result[1], 1)):
            raise failure(OutcomeUnknown, "冻结结果与原任务的模型提案不一致，需要对账",
                          "planner.frozen_proposal_mismatch", "soft-runtime")
        if trace:
            trace("planner.frozen", {"layer":"planner", "module":"soft-runtime", "provider":result[0],
                  "steps":[{"tool":s["tool"], "binding":s["binding"], "operation_id":s["soft_pin"]["reservation_id"]}
                           for s in result[1]]})
    return result


def worker(journal, robot, planning, verifying=False, *, keep_running, plan_task, registry, services):
    while keep_running():
        t=journal.due(planning, verifying)
        if not t:
            time.sleep(0.2)
            continue
        op=f"{t['id']}/step/{t['cursor']+1}"
        def trace(kind, fields):
            journal.exchange_event(t,kind,fields,None if planning else op)
        try:
            if planning:
                t["provider"],t["steps"] = plan_task(journal,t,trace)
                t["retries"]=0
                journal.save(t,"RUNNING","已验证计划",kind="planned")
                continue
            step=t["steps"][t["cursor"]]
            read_authority.authorize_dispatch(t["steps"][t["cursor"]:])
            if verifying:
                result = verify_candidate(journal, t, step, op, trace, services)
                finish_step(journal, t, step, op, result)
                continue
            if "physical_pin" in step:
                cap = {**step["physical_cap"], "_physical":step["physical_pin"]}
            elif "soft_pin" in step:
                cap = {**step["soft_cap"], "_soft":step["soft_pin"]}
            else:
                cap=registry(legacy_only=True).get(step["tool"])
            if "binding" not in step and step["tool"] not in ("inspect","status"):
                raise failure(OutcomeUnknown,"旧步骤未保存提供者绑定，请按原操作 ID 核对旧提供者的记录",
                              "tool.binding_unverified","registry")
            if (not cap or cap["version"]!=step["version"] or
                    ("binding" in step and binding(cap)!=step["binding"]) or
                    ("binding" not in step and step["tool"] in ("inspect","status") and cap.get("endpoint"))):
                raise failure(Unavailable,"所需能力未挂载，或提供者与已保存的计划不一致","tool.binding_unavailable","registry")
            prepare_verification(step, cap, services)
            if step["status"]=="PENDING":
                step.update(status="RUNNING",operation_id=op,started_at=time.time())
                if step["tool"]=="inspect" and not cap.get("endpoint"):
                    step["ros_binding"]="prepared"
                journal.save(t,"RUNNING","已持久化步骤意图",kind="dispatch",op=op)
            context=journal.provider_context(t,op) if cap.get("accepts_trace") is True else None
            provider = ("physical-controller" if "physical_pin" in step else "soft-runtime" if "soft_pin" in step
                        else "http-adapter" if cap.get("endpoint") else "software" if is_series(cap) else "ros")
            result=exchange(trace,"tool",step["tool"],{"operation_id":op,"arguments":step["args"],"version":step["version"]},
                            lambda:robot.run(step,op,cap,trace,context),
                            meta={"provider":provider,"binding":binding(cap)},
                            commit_candidate=partial(save_candidate, journal, t, step, op)
                            if step["verification"]["required"] else None)
            if result is None:
                if step_timed_out(step):
                    journal.save(t,"RECOVERY_REQUIRED","等待原操作终态超时，请对账",kind="timeout",op=op)
                else:
                    journal.save(t,"WAITING","等待原操作回执",delay=.6)
                continue
            if step["status"] == "VERIFYING":
                continue
            finish_step(journal, t, step, op, result)
        except (Unavailable, services.PluginUnavailable, services.PolicyError, SoftError) as exc:
            diagnostic=event_diagnosis(exc,planning,"tool.transport_unavailable")
            if isinstance(exc,SoftError) and not planning and exc.code in (
                    "tool.outcome_unknown","tool.receipt_invalid","plugin.config_invalid"):
                journal.save(t,"RECOVERY_REQUIRED",str(exc),kind="error",op=op,diagnostic=diagnostic)
                continue
            if planning and diagnostic.get("provider"):
                t["provider"]=diagnostic["provider"]
            t["retries"]+=1
            exhausted=retry_exhausted(t["retries"])
            journal.save(t,("BLOCKED" if planning else "RECOVERY_REQUIRED") if exhausted else
                         ("PLANNING" if planning else "WAITING"),str(exc),delay=min(30,2**min(t["retries"]-1,5)),
                         kind="retry_exhausted" if exhausted else "retry",op=None if planning else op,diagnostic=diagnostic)
        except Refused as exc:
            diagnostic=event_diagnosis(exc,planning,"tool.rejected")
            if planning and diagnostic.get("provider"):
                t["provider"]=diagnostic["provider"]
            cleanup = {"unissued_pins":exc.unissued_pins} if planning and hasattr(exc,"unissued_pins") else {}
            journal.save(t,"BLOCKED",str(exc),kind="blocked",op=None if planning else op,diagnostic=diagnostic,**cleanup)
        except OutcomeUnknown as exc:
            journal.save(t,"RECOVERY_REQUIRED",str(exc),kind="error",op=op,diagnostic=event_diagnosis(exc,planning,"tool.receipt_invalid"))
        except OperationCanceled as exc:
            journal.save(t,"CANCELED",str(exc),kind="canceled",op=op,diagnostic=event_diagnosis(exc,planning,"tool.canceled"))
        except Exception as exc:
            journal.save(t,"RECOVERY_REQUIRED",f"{type(exc).__name__}: {exc}",kind="error",op=op,
                         diagnostic=event_diagnosis(exc,planning,"agent.unexpected_error"))


def event_diagnosis(exc,planning,fallback):
    extra=getattr(exc,"diagnostic",{})
    if planning:
        fallback={"capability_check":"planner.capability_missing","plan_validation":"planner.invalid_proposal",
                  "planner":"planner.provider_failed"}.get(extra.get("stage"),fallback)
    return {**extra,**diagnosis(exc,fallback,"planner" if planning else "agent")}
