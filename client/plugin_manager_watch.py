"""Recover authorized local read-only capabilities from durable author and publisher evidence."""
import argparse
from contextlib import closing
import hashlib
from http.client import IncompleteRead
import json
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from event_sink import Sink
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"agent"))
import author_tool
from derived_tools import capability, evaluate, validate
from module_journal import ModuleJournal


ACTIONS = {
    "untrusted_source":"Check the native archive row and author source registration.",
    "receipt_invalid":"Check the private decision, source IDs and receipt digest.",
    "source_changed":"Inspect the original task and its terminal planner diagnosis.",
    "policy_invalid":"Repair the read-only deployment policy and validate its samples.",
    "policy_denied":"Review the requested intent and the deployment allowlist.",
    "semantic_mismatch":"Compare the field, unit and predicate with the authorized rule.",
    "cases_failed":"Check the independent reference measurements before publication.",
    "release_conflict":"Inspect the live tool version and the release journal; reconcile the change manually.",
    "task_conflict":"Check the existing task identity, trigger and pinned tool.",
    "followup_failed":"Inspect the follow-up task diagnosis and restore its required sensor or binding.",
    "followup_missing":"A previously accepted task is absent. Restore or inspect its owner journal before dispatching again.",
    "receipt_mismatch":"Inspect the executed tool, version, original sample and verified step.",
    "dependency_unavailable":"Restore access to the dependency; the supervisor will retry the same work.",
    "source_missing":"Inspect the missing task or incomplete API response before proceeding.",
}


class Review(Exception):
    pass


def require(condition, code):
    if not condition:
        raise Review(code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def request_json(url, body=None):
    raw = json.dumps(body).encode() if body is not None else None
    req = Request(url, data=raw, headers={"Content-Type":"application/json"})
    try:
        with urlopen(req, timeout=5) as response:
            data = response.read(2_000_001)
    except HTTPError as exc:
        with exc:
            if exc.code in (409,400):
                raise Review("task_conflict") from exc
            if exc.code==404:
                raise Review("not_found") from exc
            if exc.code==403:
                raise Review("source_missing") from exc
            raise
    require(len(data)<=2_000_000, "source_missing")
    try:
        return json.loads(data)
    except (ValueError,RecursionError) as exc:
        raise Review("source_missing") from exc


def proposal_definition(event, proposals):
    data = event["data"]
    token = data["receipt_id"]
    require(isinstance(token,str) and re.fullmatch("[0-9a-f]{64}",token),"receipt_invalid")
    path = proposals/"decisions"/(token+".json")
    try:
        require(not path.is_symlink(), "receipt_invalid")
        with path.open("rb") as stream:
            raw = stream.read(2_000_001)
        require(len(raw)<=2_000_000 and digest(raw)==data["receipt_sha256"],"receipt_invalid")
        receipt = json.loads(raw)
        source = receipt["source"]
        require(receipt["status"]=="proposed" and source["task_id"]==event["task_id"]
                and source["trace_id"]==event["trace_id"] and source["event_id"]==event["parent_event_id"]
                and source["archive_id"]==data["archive_id"] and source["seq"]==data["source_seq"]
                and token==digest(author_tool.json_bytes([source["archive_id"],source["seq"]]))
                and source["reason_code"] in ("planner.not_configured","planner.capability_missing"),"receipt_invalid")
        definition = validate(receipt["definition"])
        identifier = "derived.task_"+digest(author_tool.json_bytes([event[k] for k in
                                             ("task_id","trace_id","parent_event_id")]))[:32]
        require(definition["id"]==identifier==data["tool_id"]
                and capability(definition)["version"]==data["candidate_version"],"receipt_invalid")
        return definition, source
    except (KeyError, ValueError, TypeError, FileNotFoundError, RecursionError) as exc:
        raise Review("receipt_invalid") from exc


def rule_for(path, text, definition):
    try:
        policy = author_tool.read_json(path, 524288)
        require(set(policy)=={"schema_version","rules"} and type(policy["schema_version"]) is int
                and policy["schema_version"]==1 and isinstance(policy["rules"],list),"policy_invalid")
        require(all(isinstance(r,dict) and set(r)=={"text","semantic","cases"} for r in policy["rules"]),"policy_invalid")
        rules = [r for r in policy["rules"] if r["text"]==text]
        require(rules,"policy_denied")
        require(len(rules)==1,"policy_invalid")
        rule = rules[0]
        semantic = {k:definition[k] for k in ("field","unit","comparison") if k in definition}
        require(text==definition["description"] and author_tool.json_bytes(semantic)==author_tool.json_bytes(rule["semantic"]),
                "semantic_mismatch")
        return rule
    except (KeyError,ValueError,TypeError,OSError,RecursionError) as exc:
        raise Review("policy_invalid") from exc


def ensure_release(registry, proposal, definition, cases):
    """An applied event from this request proves a retry belongs to this publication."""
    target = capability(definition)["version"]
    with tempfile.TemporaryDirectory() as temp:
        candidate, samples = Path(temp)/"candidate.json", Path(temp)/"samples.json"
        candidate.write_bytes(author_tool.json_bytes(definition))
        samples.write_bytes(author_tool.json_bytes(cases))
        try:
            author_tool.checked_definition(candidate,samples)
        except (ValueError,KeyError,TypeError,OverflowError) as exc:
            raise Review("cases_failed") from exc
        args = argparse.Namespace(action="publish",definition=candidate,cases=samples,expect="absent",
                                  request_id="auto-"+digest(proposal["event_id"].encode())[:40],
                                  registry=registry,trace_id=proposal["trace_id"],parent_event_id=proposal["event_id"])
        with author_tool.writer_lock(registry), closing(ModuleJournal(registry/".history"/"module-events.sqlite")) as journal:
            try:
                applied = author_tool.publish_managed(args,journal)
            except ValueError as exc:
                raise Review("release_conflict") from exc
            require(applied["version"]==target and author_tool.observed_version(registry,definition["id"])==target,
                    "release_conflict")
            return applied["event_id"]


def check_result(task, definition, token, trigger):
    version = capability(definition)["version"]
    require(task["id"]==token and task["trigger"]==trigger and task["trace_id"]==trigger["trace_id"]
            and task["text"]==definition["description"],"task_conflict")
    if task["state"] in ("PLANNING","RUNNING","WAITING"):
        return None
    require(task["state"]=="SUCCEEDED","followup_failed")
    require(len(task["steps"])==1,"receipt_mismatch")
    step = task["steps"][0]
    require(step.get("status")=="SUCCEEDED" and step.get("tool")==definition["id"]
            and step.get("version")==version and step.get("operation_id")==token+"/step/1", "receipt_mismatch")
    try:
        receipt = step["evidence"]
        field = definition["field"]
        sample = {k:receipt[k] for k in ("source","frame","stamp_ns")}
        sample[field] = receipt["raw_value"]
        expected = evaluate(definition, sample)
        require(receipt["definition_version"]==version and all(
            author_tool.json_bytes(receipt[k])==author_tool.json_bytes(v) for k,v in expected.items()),"receipt_mismatch")
        last = task["events"][-1]
        trace = json.loads(last["payload"])["trace"]
        require(last["kind"]=="verified" and last["task_id"]==token and last["operation_id"]==step["operation_id"]
                and author_tool.public_reference(last["event_id"]) and last["event_id"]==trace["event_id"]
                and trace["trace_id"]==task["trace_id"],"receipt_mismatch")
        return {"field":field,"unit":expected["unit"],"value":expected["value"],
                "stamp_ns":sample["stamp_ns"],"operation_id":step["operation_id"],"verified_event_id":last["event_id"]}
    except (ValueError,KeyError,TypeError,OverflowError,IndexError) as exc:
        raise Review("receipt_mismatch") from exc


class Supervisor:
    def __init__(self,url,policy,proposals,registry,state):
        self.url, self.policy = url.rstrip("/"), Path(policy)
        self.proposals, self.registry, self.state = Path(proposals), Path(registry), Path(state)
        self.state.mkdir(parents=True,exist_ok=True)
        self.lock = sqlite3.connect(self.state/"supervisor.lock.sqlite",timeout=0)
        try:
            self.lock.execute("BEGIN IMMEDIATE")
            self.sink = Sink(self.state/"input.sqlite",self.url,{"kind":"plugin.author.proposed","source":"journal:plugin.author"})
            self.journal = ModuleJournal(self.state/"module-events.sqlite")
            self.next_pulse = 0
            self.db = self.journal.db
            with self.db:
                self.db.execute("""CREATE TABLE IF NOT EXISTS supervisor_work(
                    id TEXT PRIMARY KEY, proposal TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'new', release TEXT,
                    due REAL NOT NULL DEFAULT 0, condition TEXT, attempt INTEGER NOT NULL DEFAULT 0)""")
                if "dispatch" not in {r[1] for r in self.db.execute("PRAGMA table_info(supervisor_work)")}:
                    self.db.execute("ALTER TABLE supervisor_work ADD COLUMN dispatch TEXT")
                self.db.execute("CREATE TABLE IF NOT EXISTS supervisor_input(last_row INTEGER NOT NULL)")
                if not self.db.execute("SELECT 1 FROM supervisor_input").fetchone():
                    self.db.execute("INSERT INTO supervisor_input VALUES(0)")
        except BaseException:
            self.close()
            raise

    def close(self):
        for field in ("sink","journal","lock"):
            if hasattr(self,field):
                getattr(self,field).close()

    def api(self,path,body=None):
        return request_json(self.url+path,body)

    def record(self,p,phase,code,*,release=None,evidence=None):
        source = p["event_id"]
        current,condition,attempt = self.db.execute("SELECT phase,condition,attempt FROM supervisor_work WHERE id=?",
                                                     (source,)).fetchone()
        waiting = phase=="waiting"
        attempt = attempt+1 if waiting else 0
        due = time.time()+min(30,2**min(attempt-1,5)) if waiting else 0
        if phase=="submitted":
            due=time.time()+1
        data = {"followup_task_id":"recover-"+digest(source.encode())[:40],"proposal_event_id":source}
        if code in ACTIONS:
            data["action"] = ACTIONS[code]
        if release:
            data["release_event_id"] = release
        if evidence:
            data.update(evidence)
        with self.db:
            if not waiting or condition!=code:
                self.journal.emit({"module":"plugin.supervisor","kind":"plugin.supervisor."+phase,
                    "code":"supervisor."+code,"level":"warn" if phase in ("waiting","review") else "info",
                    "task_id":p["task_id"],"trace_id":p["trace_id"],
                    "parent_event_id":data.get("verified_event_id") or release or source,"data":data})
            self.db.execute("""UPDATE supervisor_work SET phase=?,release=coalesce(?,release),due=?,condition=?,attempt=?
                               WHERE id=?""",(current if waiting else phase,release,due,code,attempt,source))

    def authorized(self,p):
        definition, source = proposal_definition(p,self.proposals)
        # With no 'after' the endpoint returns the latest entries in chronological order.
        # Older entries may be omitted; only the authoritative terminal event is required here.
        task = self.lookup(p["task_id"])
        require(task is not None,"source_missing")
        require(task["id"]==p["task_id"] and task["trace_id"]==p["trace_id"] and task["state"]=="BLOCKED"
                and task["steps"]==[] and task.get("events"),"source_changed")
        last = task["events"][-1]
        diagnostic = json.loads(last["payload"])
        require(last["kind"]=="blocked" and last["operation_id"] is None
                and last["task_id"]==p["task_id"] and last["event_id"]==source["event_id"]
                and diagnostic["trace"]["trace_id"]==p["trace_id"] and diagnostic["trace"]["event_id"]==source["event_id"]
                and diagnostic["trace"]["layer"]=="planner"
                and diagnostic["diagnostic"]["code"]==source["reason_code"],"source_changed")
        return definition, rule_for(self.policy,task["text"],definition)

    def lookup(self,key):
        try:
            return self.api("/agent/tasks?"+urlencode({"id":key,"limit":1}))
        except Review as exc:
            if str(exc)=="not_found":
                return None
            raise

    def advance(self,p,phase,release):
        require(p.get("source")=="journal:plugin.author" and p.get("module")=="plugin.author"
                and p.get("kind")=="plugin.author.proposed" and p.get("code")=="author.proposed","untrusted_source")
        if phase in ("new","approved"):
            definition, rule = self.authorized(p)
            if phase=="new":
                self.record(p,"approved","approved",evidence={"tool_id":definition["id"],"version":capability(definition)["version"]})
            release = ensure_release(self.registry,p,definition,rule["cases"])
            self.record(p,"published","published",release=release,
                        evidence={"tool_id":definition["id"],"version":capability(definition)["version"]})
            phase = "published"
        followup = "recover-"+digest(p["event_id"].encode())[:40]
        pin = {"id":p["data"]["tool_id"],"version":p["data"]["candidate_version"]}
        binding = {"tool_id":pin["id"],"version":pin["version"]}
        trigger = {"kind":"agent","source":"plugin.supervisor","id":followup,"parent_task_id":p["task_id"],
                   "parent_event_id":release,"trace_id":p["trace_id"],
                   "read_only_tool":pin}
        outcome = self.lookup(followup)
        raw = self.db.execute("SELECT dispatch FROM supervisor_work WHERE id=?",(p["event_id"],)).fetchone()[0]
        saved = json.loads(raw) if raw else None
        if outcome is None:
            require(phase!="submitted","followup_missing")
            # After a restart the deployment may have revoked the intent or changed its tests.
            # Revalidate the current policy and replay the original publisher request before POST.
            definition, rule = self.authorized(p)
            require(ensure_release(self.registry,p,definition,rule["cases"])==release,"release_conflict")
            saved = {"definition":definition,"trigger":trigger,"cases_sha256":digest(author_tool.json_bytes(rule["cases"])),
                     "rule_sha256":digest(author_tool.json_bytes(rule)),"receipt_sha256":p["data"]["receipt_sha256"]}
            with self.db:
                self.db.execute("UPDATE supervisor_work SET dispatch=? WHERE id=?",(json.dumps(saved),p["event_id"]))
            outcome = self.api("/agent/tasks",{"id":followup,"trigger":trigger,"text":definition["description"]})
        elif saved is None:
            # Old published/submitted rows did not retain the dispatch snapshot. Reconstruct
            # only from an immutable, content-addressed installed revision and the native task.
            history = self.registry/".history"/pin["id"]/(pin["version"]+".json")
            author_tool.check_id(pin["id"])
            require(re.fullmatch("[0-9a-f]{64}",pin["version"]),"receipt_mismatch")
            try:
                definition = author_tool.read_definition(history)
            except (FileNotFoundError,ValueError) as exc:
                raise Review("receipt_mismatch") from exc
            saved = {"definition":definition,"trigger":trigger,"legacy":True}
            with self.db:
                self.db.execute("UPDATE supervisor_work SET dispatch=? WHERE id=?",(json.dumps(saved),p["event_id"]))
        definition = saved["definition"]
        require(capability(definition)["version"]==pin["version"] and definition["id"]==pin["id"]
                and saved["trigger"]==trigger,"receipt_mismatch")
        require(outcome.get("id")==followup and outcome.get("trigger")==trigger and outcome.get("text")==definition["description"]
                and outcome.get("trace_id")==p["trace_id"],"task_conflict")
        if phase!="submitted":
            self.record(p,"submitted","submitted",release=release,evidence=binding)
            outcome = self.lookup(followup)
            require(outcome is not None,"followup_missing")
        if (evidence:=check_result(outcome,definition,followup,trigger)) is None:
            with self.db:
                self.db.execute("UPDATE supervisor_work SET due=? WHERE id=?",(time.time()+1,p["event_id"]))
            return
        self.record(p,"verified","verified",release=release,evidence={**binding,**evidence})

    def poll(self):
        # An independent process can report an overdue lease even while this worker has no proposals.
        # Renew once per interval, not for every event or HTTP read.
        if time.monotonic() >= self.next_pulse:
            with self.db:
                self.journal.heartbeat(180)
            self.next_pulse = time.monotonic() + 30
        more = self.sink.copy()
        cursor = self.db.execute("SELECT last_row FROM supervisor_input").fetchone()[0]
        incoming = self.sink.db.execute("SELECT rowid,event FROM events WHERE rowid>? ORDER BY rowid LIMIT 32",(cursor,)).fetchall()
        with self.db:
            for row,raw in incoming:
                p = json.loads(raw)
                if p.get("source")=="journal:plugin.author" and all(author_tool.public_reference(p.get(k))
                         for k in ("event_id","trace_id","task_id","parent_event_id")):
                    self.db.execute("INSERT OR IGNORE INTO supervisor_work(id,proposal) VALUES(?,?)",(p["event_id"],raw))
                self.db.execute("UPDATE supervisor_input SET last_row=?",(row,))
        work = self.db.execute("""SELECT proposal,phase,release FROM supervisor_work WHERE phase NOT IN ('review','verified')
                                  AND due<=? ORDER BY due,rowid LIMIT 16""",(time.time(),)).fetchall()
        for raw,phase,release in work:
            p = json.loads(raw)
            try:
                self.advance(p,phase,release)
            except Review as exc:
                self.record(p,"review",str(exc))
            except (OSError,sqlite3.OperationalError,IncompleteRead) as exc:
                self.record(p,"waiting","dependency_unavailable",evidence={"error_type":type(exc).__name__})
            except (TypeError,ValueError,KeyError,IndexError,RecursionError):
                self.record(p,"review","source_missing")
        return bool(more or len(incoming)==32 or len(work)==16)


if __name__=="__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url",required=True)
    for flag in ("policy","proposals","registry","state"):
        parser.add_argument("--"+flag,required=True,type=Path)
    parser.add_argument("--once",action="store_true")
    args=parser.parse_args()
    with closing(Supervisor(args.url,args.policy,args.proposals,args.registry,args.state)) as manager:
        while True:
            try:
                more=manager.poll()
                if args.once:
                    break
                if not more:
                    time.sleep(1)
            except (URLError,TimeoutError,ConnectionError,IncompleteRead) as exc:
                if args.once:
                    raise
                print("archive unavailable: "+type(exc).__name__,file=sys.stderr,flush=True)
                time.sleep(3)
