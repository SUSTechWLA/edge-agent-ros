"""Wire format for a bound v1 operation and its candidate result."""
import hashlib

from .errors import OperationCanceled, OutcomeUnknown, Refused, failure, remote_failure


def request(operation_id, step, context=None):
    body = {"operation_id":operation_id,"arguments":step["args"],"version":step["version"]}
    if context is not None:
        body["trace"] = context
    return body


def candidate(result, op, step, cap):
    if not isinstance(result,dict) or result.get("operation_id")!=op:
        raise failure(OutcomeUnknown,"插件回执缺少原始 operation_id","tool.receipt_invalid","provider")
    if result.get("state")=="RUNNING":
        return None
    if result.get("state")=="CANCELED":
        raise OperationCanceled(str(result.get("detail","操作已取消")))
    if result.get("state") in ("FAILED","BLOCKED"):
        raise remote_failure(Refused,result,"tool.provider_failed")
    if result.get("state")!="SUCCEEDED" or not result.get("evidence"):
        raise remote_failure(OutcomeUnknown,result,"tool.receipt_invalid")
    evidence = result["evidence"]
    if cap.get("verbatim_argument"):
        text, = step["args"].values()
        if (not isinstance(evidence,dict) or evidence.get("value") is None or
                evidence.get("definition_version") != step["version"] or
                evidence.get("input_sha256") != hashlib.sha256(text.encode("utf-8")).hexdigest()):
            raise failure(OutcomeUnknown,"文本工具回执与原文、绑定版本或结果字段不符",
                          "tool.receipt_invalid","provider")
    for outcome, field in cap.get("outcomes",{}).items():
        if not isinstance(evidence,dict) or field not in evidence or evidence[field] in (None,""):
            raise OutcomeUnknown(f"插件缺少 {outcome} 要求的证据字段 {field}")
    return evidence
