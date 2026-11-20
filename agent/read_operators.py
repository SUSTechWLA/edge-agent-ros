"""Site-authorized numeric definitions. The model can supply only bounded program data."""
import hashlib
import json
import os
from pathlib import Path
import tempfile

from edge_contracts.descriptor import NAME, SHA
from edge_contracts.errors import Refused, failure
from agent.kernel import read_authority
if __package__:
    from . import numeric_program as numeric
else:
    import numeric_program as numeric


def reject(message, code="operator.policy_invalid"):
    return failure(Refused, message, code, "read-operators")


def read_json(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing regular file")
    with path.open("rb") as stream:
        raw = stream.read(262145)
    if len(raw) > 262144:
        raise ValueError("operator record too large")
    return json.loads(raw)


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",",":"), allow_nan=False).encode()).hexdigest()


def publish_once(path, data):
    """Never replace a version. A competing publisher can win the contract's single slot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(json.dumps(data, ensure_ascii=False, allow_nan=False).encode())
            stream.flush()
            if os.name == "posix":
                os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        try:
            os.link(temp, path)
        except FileExistsError:
            return False
        if os.name == "posix":
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        return True
    finally:
        temp.unlink(missing_ok=True)


class Operators:
    def __init__(self, directory, policy):
        self.directory = directory
        self.runner = policy["runner"]
        self.contracts = policy["contracts"]

    @classmethod
    def configured(cls):
        path = os.environ.get("EDGE_READ_OPERATOR_POLICY")
        if path is None:
            return None
        try:
            doc = read_json(Path(path))
            directory = Path(os.environ["EDGE_READ_OPERATOR_DIR"])
            if (not directory.is_absolute() or not directory.is_dir() or set(doc) != {"schema_version","runner","contracts"}
                    or type(doc["schema_version"]) is not int or doc["schema_version"] != 1 or not NAME.fullmatch(doc["runner"])
                    or not isinstance(doc["contracts"], dict) or not 1 <= len(doc["contracts"]) <= 32):
                raise ValueError()
            for name, spec in doc["contracts"].items():
                if (not NAME.fullmatch(name) or not isinstance(spec, dict) or set(spec) != {"description","cases"}
                        or not isinstance(spec["description"], str) or not 1 <= len(spec["description"].strip()) <= 1200
                        or not isinstance(spec["cases"], list) or not 2 <= len(spec["cases"]) <= 64):
                    raise ValueError()
            return cls(directory, doc)
        except (KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
            raise reject("无法读取部署方的数值算子策略或目录") from exc

    def pointer(self, contract):
        # A changed specification (including verification cases) needs fresh qualification.
        return self.directory / ".contracts" / content_hash([contract, self.contracts[contract]])

    def inspect(self, contract):
        pointer = self.pointer(contract)
        if not pointer.exists():
            return None
        try:
            digest = read_json(pointer)
            if not isinstance(digest, str) or not SHA.fullmatch(digest):
                raise ValueError()
            record = read_json(self.directory / (digest[7:] + ".json"))
            if (record["operator_digest"] != digest or record["contract"] != contract
                    or numeric.digest(record["program"]) != digest[7:]
                    or record["validation"] != numeric.verify(record["program"], self.contracts[contract]["cases"])):
                raise ValueError()
            return digest
        except (KeyError, TypeError, ValueError, OSError, RecursionError) as exc:
            raise reject("已登记算子的内容或验收与现场契约不一致", "operator.record_invalid") from exc

    def describe(self):
        return [{"id":name, "description":spec["description"], "installed":self.inspect(name)}
                for name,spec in self.contracts.items()]

    def input_schema(self, cap):
        schema = cap.get("_parameter_schema", {})
        properties = schema.get("properties", {})
        if (schema.get("type") != "object" or properties.get("operator_digest", {}).get("type") != "string"
                or "operator_digest" not in schema.get("required", ())):
            raise reject("受信计算工具缺少内容摘要参数", "operator.runner_contract_invalid")
        return {**schema, "properties":{k:v for k,v in properties.items() if k != "operator_digest"},
                "required":[k for k in schema["required"] if k != "operator_digest"], "additionalProperties":False}

    def install(self, contract, program, proof):
        digest = "sha256:" + numeric.digest(program)
        target = self.directory / (digest[7:] + ".json")
        record = {"program":program,"language":numeric.LANGUAGE,"operator_digest":digest,"contract":contract,
                  "description":self.contracts[contract]["description"],"validation":proof}
        if not publish_once(target, record):
            old = read_json(target)
            if (old.get("operator_digest") != digest or numeric.digest(old.get("program")) != digest[7:]
                    or old.get("contract") != contract or old.get("validation") != proof):
                raise reject("已有内容摘要绑定了不同的契约或验收", "operator.record_conflict")
        installed = publish_once(self.pointer(contract), digest)
        return self.inspect(contract), installed

    def authorize_publication(self, name, runner_binding):
        latest = Operators.configured()
        reads = read_authority.load()
        if (not latest or not reads or reads.get(self.runner) != runner_binding or latest.runner != self.runner
                or latest.directory != self.directory or latest.contracts.get(name) != self.contracts[name]):
            raise reject("生成算子期间部署授权已改变", "operator.binding_denied")

    def resolve(self, gap, journal, task_id, trace, author, runner_binding):
        name = gap["contract"]
        spec = self.contracts[name]
        def event(kind, **fields):
            if trace:
                trace(kind, {"layer":"planner", "module":"read-operators", "contract":name, **fields})
        digest = self.inspect(name)
        if digest:
            event("operator.reused", operator_digest=digest)
        else:
            event("operator.gap", code="operator.implementation_missing")
            state = journal.operator_intent(task_id)
            program = state["program"]
            feedback = state["feedback"]
            if program is None:
                for _ in range(2):
                    attempt = journal.author_attempt(task_id)
                    event("operator.author_request", attempt=attempt)
                    candidate = None
                    try:
                        candidate = numeric.validate(author(spec["description"], feedback))
                        numeric.verify(candidate, spec["cases"])
                    except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
                        feedback = ({"code":"operator.invalid_program"} if candidate is None else
                                    {"code":"operator.independent_verification_mismatch",
                                     "prior_program":{"instructions":candidate["tokens"]}})
                        journal.operator_feedback(task_id, feedback)
                        event("operator.rejected", attempt=attempt, code=feedback["code"])
                        if attempt >= 2:
                            raise reject("候选算子未通过独立样本验收", "operator.validation_failed") from exc
                        continue
                    program = candidate
                    journal.save_operator_program(task_id, program)
                    break
            proof = numeric.verify(program, spec["cases"])
            event("operator.verified", operator_digest="sha256:" + numeric.digest(program), validation=proof)
            self.authorize_publication(name, runner_binding)
            digest, installed = self.install(name, program, proof)
            event("operator.installed" if installed else "operator.reused", operator_digest=digest)
        event("planner.resumed", operator_digest=digest)
        return {"tool":self.runner, "args":{**gap["input"],"operator_digest":digest}}


def authorize_frozen(steps):
    if "EDGE_READ_OPERATOR_POLICY" not in os.environ and not any("read_operator" in s for s in steps):
        return
    site = Operators.configured()
    for step in steps:
        marker = step.get("read_operator")
        if marker is None and (site is None or step.get("tool") != site.runner):
            continue
        args = step.get("args", {})
        if (not site or step.get("tool") != site.runner or not isinstance(marker, str) or marker not in site.contracts
                or not isinstance(args.get("operator_digest"), str) or site.inspect(marker) != args["operator_digest"]):
            raise reject("恢复的算子没有当前部署验收", "operator.binding_denied")
