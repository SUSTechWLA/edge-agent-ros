"""Summarize local public exports without importing their producers."""
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path


def _field(record, key, expected):
    value = record.get(key)
    if type(value) is not expected:
        raise ValueError(f"{key} must be {expected.__name__}")
    return value


def _invalid_constant(value):
    raise ValueError(f"invalid JSON number {value}")


def _object(raw):
    value = json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _console(stream, digest, owner_id):
    kinds, levels = Counter(), Counter()
    first = last = None
    without_id = matched = 0
    for line, raw in enumerate(stream, 1):
        digest.update(raw)
        try:
            event = _object(raw)
            seq = _field(event, "seq", int)
            if seq < 1 or last is not None and seq <= last:
                raise ValueError("export sequence must be positive and increasing")
            kinds[_field(event, "kind", str)] += 1
            levels[_field(event, "level", str)] += 1
            task_id = event.get("task_id")
            if task_id is None:
                without_id += 1
            elif not isinstance(task_id, str):
                raise ValueError("task_id must be text or null")
            elif task_id == owner_id:
                matched += 1
            if first is None:
                first = seq
            last = seq
        except (ValueError, RecursionError) as error:
            raise ValueError(f"line {line}: {error}") from None
    return {"events": kinds.total(), "first_seq": first, "last_seq": last,
            "without_task_id": without_id, "explicit_owner_matches": matched if owner_id is not None else None,
            "by_kind": dict(sorted(kinds.items())), "by_level": dict(sorted(levels.items()))}


def _task(record):
    return {"task_id": _field(record, "id", str), "state": _field(record, "state", str),
            "steps": len(_field(record, "steps", list)), "events_in_page": len(_field(record, "events", list)),
            "events_has_more": _field(record, "events_has_more", bool)}


def _evidence(receipt):
    state = _field(receipt, "state", str)
    result = {"operation_id": _field(receipt, "operation_id", str), "query_state": state}
    if state == "BLOCKED":
        result["error_code"] = _field(_field(receipt, "error", dict), "code", str)
        return result
    if state != "SUCCEEDED":
        raise ValueError("unexpected query_evidence receipt state")
    evidence = _field(receipt, "evidence", dict)
    coverage = _field(evidence, "coverage", dict)
    columns, rows = _field(evidence, "columns", list), _field(evidence, "rows", list)
    if any(not isinstance(row, list) or len(row) != len(columns) for row in rows):
        raise ValueError("query row does not match columns")
    result.update(task_id=_field(coverage, "task_id", str), archive_id=_field(coverage, "archive_id", str),
                  loaded_events=_field(coverage, "loaded_events", int), returned_rows=len(rows),
                  has_more=_field(coverage, "has_more", bool), truncated=_field(evidence, "truncated", bool),
                  retention_gap_possible=_field(coverage, "retention_gap_possible", bool))
    return result


def _recording(record):
    counts = _field(record, "counts", dict)
    if any(type(count) is not int or count < 0 for count in counts.values()):
        raise ValueError("counts must be nonnegative integers")
    missing = _field(record, "missing_sources", list)
    if any(not isinstance(source, str) for source in missing):
        raise ValueError("missing_sources must contain source names")
    return {"task_id": _field(record, "task_id", str), "recording_id": _field(record, "recording_id", str),
            "status": _field(record, "status", str), "reported_task_status": record.get("task_status"),
            "samples_by_source": counts, "missing_sources": missing}


def analyze(*, console=None, task=None, evidence=None, recording=None):
    """Read up to one file of each public format. Absent sources return None."""
    inputs = {"task": task, "console": console, "evidence": evidence, "recording": recording}
    if all(path is None for path in inputs.values()):
        raise ValueError("supply at least one exported file")
    readers = {"task": _task, "evidence": _evidence, "recording": _recording}
    report = {}
    for name, path in inputs.items():
        if path is None:
            report[name] = None
            continue
        digest = sha256()
        try:
            with Path(path).open("rb") as stream:
                if name == "console":
                    owner = report["task"]
                    data = _console(stream, digest, owner["task_id"] if owner else None)
                else:
                    raw = stream.read()
                    digest.update(raw)
                    data = readers[name](_object(raw))
        except (ValueError, OSError, RecursionError) as error:
            raise ValueError(f"{name} ({path}): {error}") from None
        report[name] = {**data, "input_sha256": digest.hexdigest()}
    task_ids = {data["task_id"] for data in report.values() if data and "task_id" in data}
    if len(task_ids) > 1:
        raise ValueError("task, evidence and recording exports identify different tasks")
    return report


def format_text(report):
    """Render a human summary of the same report returned by analyze()."""
    output = []
    task = report["task"]
    if task:
        more = "，另有分页" if task["events_has_more"] else ""
        output.append(f"任务 {task['task_id']} 的所有者状态为 {task['state']}，"
                      f"记录了 {task['steps']} 个步骤，本页有 {task['events_in_page']} 条事件{more}。")
    else:
        output.append("未提供任务导出，所有者状态未知。")
    console = report["console"]
    if console:
        levels = "、".join(f"{name} {n}" for name, n in console["by_level"].items())
        output.append(f"Console 导出含 {console['events']} 条事件" + (f"（{levels}）。" if levels else "。"))
        if task:
            output.append(f"其中 {console['explicit_owner_matches']} 条明确标记该任务 ID，"
                          f"{console['without_task_id']} 条未标记任务 ID。")
    else:
        output.append("未提供 Console 事件导出。")
    evidence = report["evidence"]
    if evidence is None:
        output.append("未提供 Evidence 查询回执。")
    elif "error_code" in evidence:
        output.append(f"Evidence 查询 {evidence['query_state']}，错误码 {evidence['error_code']}。")
    else:
        output.append(f"Evidence 查询 {evidence['query_state']}，加载 {evidence['loaded_events']} 条归档事件，"
                      f"返回 {evidence['returned_rows']} 行。")
        if evidence["has_more"] or evidence["truncated"] or evidence["retention_gap_possible"]:
            output.append("Evidence 的查询窗口、返回行或归档保留范围有限，完整性标记见 JSON。")
    recording = report["recording"]
    if recording:
        missing = "、".join(recording["missing_sources"]) or "无"
        samples = sum(recording["samples_by_source"].values())
        output.append(f"录制 {recording['recording_id']} 的状态为 {recording['status']}，"
                      f"捕获 {samples} 份样本，未收到样本的来源为{missing}。")
    else:
        output.append("未提供 Recorder 录制清单。")
    return "\n".join(output)
