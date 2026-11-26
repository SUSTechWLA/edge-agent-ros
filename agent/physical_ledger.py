"""SDD-045 P2. Single-board deployment, device fence, original operation and independent proof."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import re
import time
import uuid

from author_tool import public_reference
from module_journal import ModuleJournal, canonical
from plugin_descriptor import SHA
from edge_contracts.soft_transport import proposal_digest


class PhysicalError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


class InvalidPhysicalArguments(PhysicalError):
    """The same immutable site and arguments cannot become admissible on retry."""


def require(ok, code, detail):
    if not ok:
        raise PhysicalError(code, detail)


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def number(value, minimum=1):
    return type(value) is int and value >= minimum


def ref(value):
    return public_reference(value) and not value.startswith("/") and ".." not in value.split("/")


class PhysicalLedger:
    """Mutations use BEGIN IMMEDIATE; only the trusted local controller can open this file."""

    claim_kind = "capture"
    # True only when every actor poll carries a binding derived from the actor's own code (see ViewLedger).
    actors_bind_code = False

    def check_arguments(self, device, arguments):
        if arguments != {}:
            raise InvalidPhysicalArguments("tool.binding_unavailable", "camera capture takes no arguments")

    def __init__(self, root, boot_id, clock=time.monotonic_ns):
        require(public_reference(boot_id), "plugin.fence_rejected", "host boot identity required")
        self.boot = boot_id
        self.clock = clock
        self.journal = ModuleJournal(Path(root) / "physical.sqlite")
        self.db = self.journal.db
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS physical_device(resource TEXT PRIMARY KEY,epoch INTEGER NOT NULL,boot TEXT NOT NULL,
                phase TEXT NOT NULL,deployment TEXT NOT NULL,camera TEXT,verifier TEXT,stop_proof TEXT,exit_proof TEXT);
            CREATE TABLE IF NOT EXISTS physical_plans(task TEXT PRIMARY KEY,plan TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS physical_step_admissions(task TEXT NOT NULL,step_index INTEGER NOT NULL,
                op TEXT NOT NULL UNIQUE,proposal TEXT NOT NULL,receipt TEXT NOT NULL,PRIMARY KEY(task,step_index));
            CREATE TABLE IF NOT EXISTS physical_denials(task TEXT PRIMARY KEY,proposal TEXT NOT NULL,reason TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS physical_ops(op TEXT PRIMARY KEY,resource TEXT NOT NULL,pin TEXT NOT NULL,args TEXT NOT NULL,
                state TEXT NOT NULL,lease TEXT,candidate TEXT,proof TEXT,reason TEXT,settled INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS physical_work ON physical_ops(resource,state);
            CREATE TABLE IF NOT EXISTS physical_versions(id TEXT NOT NULL,version TEXT NOT NULL,digest TEXT NOT NULL,
                PRIMARY KEY(id,version));
            CREATE TABLE IF NOT EXISTS physical_config(resource TEXT PRIMARY KEY,interval_ms INTEGER NOT NULL,
                accepted TEXT NOT NULL,observed TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS physical_stop_requests(resource TEXT NOT NULL,epoch INTEGER NOT NULL,
                boot TEXT NOT NULL,challenge TEXT NOT NULL,issued_ns INTEGER NOT NULL,
                PRIMARY KEY(resource,epoch,boot));
        """)
        with self.write():
            for resource, epoch in self.db.execute("SELECT resource,epoch FROM physical_device WHERE boot<>?", (boot_id,)).fetchall():
                self.db.execute("""UPDATE physical_device SET boot=?,epoch=epoch+1,stop_proof=NULL,
                    phase=CASE WHEN phase IN ('STOPPED','RELEASED') THEN phase ELSE 'DRAINING' END
                    WHERE resource=?""", (boot_id,resource))
                # Reboot invalidates ownership. A previously frozen but undispatched
                # step cannot open the newly fenced device while an old outcome is unknown.
                for op,state in self.db.execute("SELECT op,state FROM physical_ops WHERE resource=? AND state IN ('RESERVED','OFFERED','CLAIMED','CANDIDATE')",
                                           (resource,)).fetchall():
                    self._unknown(op, "plugin.boot_changed_before_dispatch" if state == "RESERVED" else "plugin.boot_changed")
                self.event(resource, "fence.boot_changed", epoch=epoch+1)

    @contextmanager
    def write(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def close(self):
        self.journal.close()

    def device(self, resource):
        row = self.db.execute("SELECT epoch,boot,phase,deployment,camera,verifier,stop_proof FROM physical_device WHERE resource=?",
                              (resource,)).fetchone()
        require(row, "plugin.service_missing", "device is not installed")
        return dict(resource_id=resource,epoch=row[0],boot_id=row[1],phase=row[2],deployment=json.loads(row[3]),
                    camera_instance=row[4],verifier_instance=row[5],stop_proof=json.loads(row[6]) if row[6] else None)

    def event(self, resource, kind, op=None, **data):
        device = self.device(resource)
        pin = device["deployment"]["pin"]
        owner = self.status(op)["pin"] if op else pin
        self.journal.emit({"module":"physical.controller","layer":"plugin","kind":"plugin.physical." + kind,
            "code":"plugin.physical." + kind,"trace_id":op or owner["plugin_id"],"operation_id":op,
            "task_id":op.rpartition("/step/")[0] if op else None,"data":{
                **{k:owner[k] for k in ("plugin_id","version","content_digest","binding")},
                "resource_id":resource,"epoch":device["epoch"],"boot_id":self.boot,**data}})

    def install(self, deployment, *, owner_locked=False):
        """The deployment CLI has already authenticated both immutable artifacts and site approval.

        owner_locked means the caller holds the exclusive deployment lock, so no controller serves the
        earlier deployment and no actor can reach this ledger through it."""
        pin = deployment["pin"]
        resource = pin["resource_id"]
        with self.write():
            existing = self.db.execute("SELECT deployment,phase FROM physical_device WHERE resource=?", (resource,)).fetchone()
            if existing and json.loads(existing[0]) == deployment and existing[1] == "ACTIVE":
                return self.device(resource)
            cold = bool(existing) and existing[1] == "DRAINING" and self._boot_fenced(resource)
            if cold and json.loads(existing[0]) == deployment:
                return self._readmit_after_boot(resource)
            unsettled = bool(existing) and self.db.execute(
                "SELECT 1 FROM physical_ops WHERE resource=? AND settled=0 LIMIT 1", (resource,)).fetchone()
            # After a reboot fence no earlier process survives. A different, separately approved deployment is a
            # boot-time installation, not a hot replacement, but only once every earlier operation is settled.
            # Within one boot the same holds for an idle device whose actors prove their code identity on every
            # poll: actors of the earlier code are refused by scope, and no operation holds the device.
            idle = (bool(existing) and owner_locked and self.actors_bind_code and existing[1] != "STOPPED"
                    and json.loads(existing[0]) != deployment)
            require(not existing or existing[1] == "STOPPED" or ((cold or idle) and not unsettled),
                    "plugin.physical_hot_reload_forbidden",
                    "finish drain, independent stop, lease release and confirmed process exit")
            replaced = json.loads(existing[0])["pin"] if existing else None
            previous = self.db.execute("SELECT digest FROM physical_versions WHERE id=? AND version=?",
                                       (pin["plugin_id"],pin["version"])).fetchone()
            require(not previous or previous[0] == pin["content_digest"], "version_conflict",
                    "same version already identifies different contents")
            self.db.execute("INSERT OR IGNORE INTO physical_versions VALUES(?,?,?)",
                            (pin["plugin_id"],pin["version"],pin["content_digest"]))
            self.db.execute("""INSERT INTO physical_device VALUES(?,1,?,'ACTIVE',?,NULL,NULL,NULL,NULL)
                ON CONFLICT(resource) DO UPDATE SET epoch=epoch+1,boot=excluded.boot,phase='ACTIVE',
                deployment=excluded.deployment,camera=NULL,verifier=NULL,stop_proof=NULL,exit_proof=NULL""",
                (resource,self.boot,canonical(deployment)))
            handoff = {}
            if existing and existing[1] != "STOPPED":
                handoff = {"handoff":"boot_fence" if cold else "idle_upgrade",
                           "previous_version":replaced["version"],"previous_content_digest":replaced["content_digest"]}
            self.event(resource,"installed",**handoff)
        return self.device(resource)

    # Device-level lifecycle facts in the ledger's own journal, written in the same transaction as the phase.
    LIFECYCLE = ("installed","draining","stop.confirmed","lease.released","process.stopped","fence.readmitted")

    def _boot_fenced(self, resource):
        """True when DRAINING came only from kernel reboots of an ACTIVE device, not from its deployer.

        A reboot ended every process of the earlier incarnation, so nothing of it can still be in flight."""
        kinds = ("fence.boot_changed",) + self.LIFECYCLE
        rows = self.db.execute("""SELECT json_extract(event,'$.kind') FROM module_bus_events
            WHERE json_extract(event,'$.data.resource_id')=? AND json_extract(event,'$.kind') IN ({})
            ORDER BY seq DESC""".format(",".join("?"*len(kinds))),
            (resource,*("plugin.physical."+k for k in kinds))).fetchall()
        reboots = 0
        for kind, in rows:
            if kind == "plugin.physical.fence.boot_changed":
                reboots += 1
                continue
            if kind not in ("plugin.physical.installed","plugin.physical.fence.readmitted"):
                return False  # The deployer drained, stopped or released the device: keep the handoff protocol.
            break
        else:
            return False
        live = self.db.execute("""SELECT 1 FROM physical_ops WHERE resource=? AND
            state IN ('RESERVED','OFFERED','CLAIMED','CANDIDATE') LIMIT 1""", (resource,)).fetchone()
        return reboots > 0 and not live

    def _readmit_after_boot(self, resource):
        """Re-admit the same immutable deployment under a new epoch after a reboot fence.

        Operations from the earlier boot keep RECOVERY_REQUIRED and their original lease epoch, so they cannot
        claim the new incarnation and still need reconciliation by their task owner. Process slots are cleared
        because the earlier camera/executor and verifier processes did not survive the reboot."""
        self.db.execute("""UPDATE physical_device SET epoch=epoch+1,phase='ACTIVE',camera=NULL,verifier=NULL,
            stop_proof=NULL,exit_proof=NULL WHERE resource=?""", (resource,))
        unresolved = self.db.execute("SELECT COUNT(*) FROM physical_ops WHERE resource=? AND state='RECOVERY_REQUIRED'",
                                     (resource,)).fetchone()[0]
        self.event(resource, "fence.readmitted", unresolved_operations=unresolved)
        return self.device(resource)

    def _match(self, pin):
        device = self.device(pin["resource_id"])
        expected = dict(device["deployment"]["pin"])
        if "reservation_id" in pin:
            expected["reservation_id"] = pin["reservation_id"]
        require(canonical(pin) == canonical(expected), "tool.binding_unavailable", "original deployment binding unavailable")
        return device

    def _reserve_step(self, task, index, step):
        pin = dict(step["physical_pin"])
        require("reservation_id" not in pin,"tool.binding_unavailable","new plan cannot reuse another operation's pin")
        device = self._match(pin)
        require(device["phase"] == "ACTIVE", "plugin.draining", "physical admission closed")
        require(step["tool"] == pin["name"] and step["version"] == pin["version"] and step["binding"] == pin["binding"],
                "tool.binding_unavailable", "physical operation differs from catalog")
        self.check_arguments(device, step["args"])
        op = f"{task}/step/{index}"
        require(public_reference(op), "plugin.manifest_invalid", "invalid operation identity")
        pin["reservation_id"] = op
        step["physical_pin"] = pin
        self.db.execute("INSERT INTO physical_ops(op,resource,pin,args,state) VALUES(?,?,?,?,'RESERVED')",
                        (op,device["resource_id"],canonical(pin),canonical(step["args"])))
        self.event(device["resource_id"],"reserved",op)
        return op

    def _require_full_plan_mode(self, task):
        prior = self.db.execute("SELECT 1 FROM physical_step_admissions WHERE task=? LIMIT 1", (task,)).fetchone()
        require(not prior, "tool.binding_unavailable", "task already uses incremental physical admission")

    def freeze_plan(self, task, provider, steps):
        require(public_reference(task), "plugin.manifest_invalid", "invalid task identity")
        with self.write():
            prior = self.db.execute("SELECT plan FROM physical_plans WHERE task=?", (task,)).fetchone()
            if prior:
                return json.loads(prior[0])
            self._require_full_plan_mode(task)
            self.check_not_denied(task)
            # Later validation can roll the entire transaction back; preserve the
            # submitted proposal for the caller's exact refusal/retry comparison.
            steps = json.loads(canonical(steps))
            for index, step in enumerate(steps, 1):
                if "physical_pin" in step:
                    self._reserve_step(task, index, step)
            plan = [provider,steps]
            self.db.execute("INSERT INTO physical_plans VALUES(?,?)",(task,canonical(plan)))
        return plan

    def freeze_step(self, task, index, provider, step, available_tools):
        """The task owner can interleave software steps; every physical proposal has its own immutable receipt."""
        require(public_reference(task) and public_reference(provider) and number(index) and index <= 2**31-1,
                "plugin.manifest_invalid", "invalid incremental task, provider or step index")
        require(isinstance(step, dict) and {"tool","version","binding","physical_pin","args"} <= step.keys()
                and isinstance(step["physical_pin"], dict) and isinstance(step["args"], dict)
                and step.get("status","PENDING") == "PENDING" and "soft_pin" not in step and "soft_cap" not in step,
                "tool.binding_unavailable", "expected a single physical proposal")
        proposal = proposal_digest(provider, [step])
        with self.write():
            self.check_not_denied(task)
            full_plan = self.db.execute("SELECT 1 FROM physical_plans WHERE task=?", (task,)).fetchone()
            require(not full_plan, "tool.binding_unavailable", "task already admitted a full physical plan")
            prior = self.db.execute("SELECT proposal,receipt FROM physical_step_admissions WHERE task=? AND step_index=?",
                                    (task,index)).fetchone()
            if prior:
                require(prior[0] == proposal, "tool.binding_unavailable", "step already bound to another proposal")
                return json.loads(prior[1])
            last_index, settled = self.db.execute("""
                SELECT MAX(s.step_index),MIN(o.settled) FROM physical_step_admissions s
                JOIN physical_ops o ON o.op=s.op WHERE s.task=?""", (task,)).fetchone()
            if last_index is not None:
                require(index > last_index, "tool.binding_unavailable", "cannot insert an earlier physical step")
                require(settled, "plugin.lease_conflict", "previous physical operation awaits settlement")
            require(available_tools, "plugin.dependency_unavailable", "provider or independent verifier unavailable")
            cap = next((c for c in available_tools if c["_physical"] == step["physical_pin"]), None)
            require(cap is not None, "tool.binding_unavailable", "proposed physical binding is unavailable")
            if "physical_cap" in step:
                require(step["physical_cap"] == {k:v for k,v in cap.items() if k != "_physical"},
                        "tool.binding_unavailable", "physical capability differs from advertised contract")
            frozen = json.loads(canonical(step))
            op = self._reserve_step(task, index, frozen)
            receipt = [provider,frozen]
            self.db.execute("INSERT INTO physical_step_admissions VALUES(?,?,?,?,?)",
                            (task,index,op,proposal,canonical(receipt)))
        return receipt

    def check_not_denied(self, task):
        prior = self.db.execute("SELECT reason FROM physical_denials WHERE task=?", (task,)).fetchone()
        if prior:
            raise PhysicalError(prior[0], "original physical admission was permanently denied")

    def original_plan(self, plan):
        proposal = json.loads(canonical(plan))
        for step in proposal[1]:
            if "physical_pin" in step:
                step["physical_pin"].pop("reservation_id", None)
        return proposal

    def reconcile_denial(self, task, provider, steps, deployment):
        """Seal a proven immutable argument failure, or recover a concurrently admitted plan."""
        require(public_reference(task), "plugin.manifest_invalid", "invalid task identity")
        proposal = proposal_digest(provider, steps)
        with self.write():
            self._require_full_plan_mode(task)
            admitted = self.db.execute("SELECT plan FROM physical_plans WHERE task=?", (task,)).fetchone()
            if admitted:
                original = json.loads(admitted[0])
                require(self.original_plan(original) == [provider,steps], "tool.binding_unavailable",
                        "another proposal already admitted")
                return {"admission":"accepted", "plan":original}
            prior = self.db.execute("SELECT proposal,reason FROM physical_denials WHERE task=?", (task,)).fetchone()
            if prior:
                require(prior[0] == proposal, "tool.binding_unavailable", "another proposal already sealed")
                reason = prior[1]
            else:
                local = [step for step in steps if "physical_pin" in step]
                require(local and all(step["physical_pin"] == deployment["pin"] for step in local),
                        "tool.binding_unavailable", "refusal must come from every original physical binding")
                device = self._match(deployment["pin"])
                reason = None
                for step in local:
                    try:
                        self.check_arguments(device, step["args"])
                    except InvalidPhysicalArguments as error:
                        reason = error.code
                        break
                require(reason, "plugin.admission_unconfirmed", "no permanent argument refusal on this deployment")
                self.db.execute("INSERT INTO physical_denials VALUES(?,?,?)", (task,proposal,reason))
                self.event(device["resource_id"], "admission.denied", task_id=task, reason=reason, proposal=proposal)
            return {"admission":"denied", "task":task, "reason":reason, "proposal":proposal}

    def status(self, op):
        item = self.db.execute("SELECT pin,args,state,lease,candidate,proof,reason,settled FROM physical_ops WHERE op=?",
                               (op,)).fetchone()
        require(item,"tool.binding_unavailable","unknown physical operation")
        return dict(operation_id=op,pin=json.loads(item[0]),args=json.loads(item[1]),state=item[2],
                    lease=json.loads(item[3]) if item[3] else None,candidate=json.loads(item[4]) if item[4] else None,
                    proof=json.loads(item[5]) if item[5] else None,reason=item[6],settled=bool(item[7]))

    def _unknown(self, op, code):
        row = self.status(op)
        if row["state"] in ("RESERVED","OFFERED","CLAIMED","CANDIDATE"):
            self.db.execute("UPDATE physical_ops SET state='RECOVERY_REQUIRED',reason=? WHERE op=?", (code,op))
            self.event(row["pin"]["resource_id"],"recovery_required",op,reason=code)

    def expire(self):
        with self.write():
            for op, raw in self.db.execute("SELECT op,lease FROM physical_ops WHERE state IN ('OFFERED','CLAIMED','CANDIDATE')").fetchall():
                if json.loads(raw)["valid_until_ns"] <= self.clock():
                    self._unknown(op,"plugin.lease_expired")

    def begin(self, pin, wire):
        op = pin["reservation_id"]
        self.expire()
        with self.write():
            row = self.status(op)
            require(canonical(row["pin"]) == canonical(pin) and wire.get("operation_id") == op and wire.get("arguments") == row["args"] and
                    wire.get("version") == pin["version"] and set(wire)=={"operation_id","arguments","version"},
                    "tool.binding_unavailable", "operation differs from original plan")
            if row["state"] != "RESERVED":
                return row
            device = self._match(pin)
            resource = device["resource_id"]
            require(not row["settled"] and device["phase"] in ("ACTIVE","DRAINING"), "plugin.draining", "no new device work")
            self.check_arguments(device, row["args"])
            busy = self.db.execute("""SELECT op FROM physical_ops WHERE resource=? AND state IN
                ('OFFERED','CLAIMED','CANDIDATE','RECOVERY_REQUIRED')""", (resource,)).fetchall()
            # Unresolved older operations continue to exist after a proven physical
            # handoff; they cannot claim the next controller incarnation.
            for other, in busy:
                lease = self.status(other)["lease"]
                require(lease is None or lease["epoch"] != device["epoch"], "plugin.lease_conflict", "resource has outstanding work")
            lease = {k:pin[k] for k in ("plugin_id","version","content_digest","provider_id","resource_id")}
            lease.update(epoch=device["epoch"],boot_id=self.boot,operation_id=op,
                         valid_until_ns=self.clock() + device["deployment"]["lease_ms"]*1_000_000)
            self.db.execute("UPDATE physical_ops SET state='OFFERED',lease=? WHERE op=?", (canonical(lease),op))
            self.event(resource,"lease.acquired",op)
        return self.status(op)

    def assert_active(self, lease):
        row = self.status(lease["operation_id"])
        device = self._match(row["pin"])
        require(row["state"] == "CLAIMED" and canonical(row["lease"]) == canonical(lease) and lease["boot_id"] == self.boot
                and lease["epoch"] == device["epoch"] and self.clock() < lease["valid_until_ns"]
                and device["phase"] in ("ACTIVE","DRAINING"), "plugin.fence_rejected", "operation fence is stale")
        return True

    def poll(self, resource, role, instance):
        require(role in ("camera","verifier") and isinstance(instance,str) and
                re.fullmatch("[a-zA-Z0-9_.-]{1,128}",instance), "plugin.permission_denied", "authenticated instance required")
        self.expire()
        with self.write():
            device = self.device(resource)
            pin = device["deployment"]["pin"]
            present = device[role+"_instance"]
            require(not present or present == instance, "plugin.fence_rejected", "different process incarnation")
            other = device["verifier_instance" if role == "camera" else "camera_instance"]
            require(instance != other,"plugin.verifier_invalid","device and verifier must have separate processes")
            if not present:
                self.db.execute("UPDATE physical_device SET " + role + "=? WHERE resource=?", (instance,resource))
                self.event(resource,role+".attached")
            if device["phase"] in ("RELEASED","STOPPED"):
                return {"kind":"released","resource_id":resource,"epoch":device["epoch"]}
            if role == "camera":
                selected = self.db.execute("SELECT op FROM physical_ops WHERE resource=? AND state='OFFERED' ORDER BY rowid LIMIT 1",
                                           (resource,)).fetchone()
                if selected:
                    op = selected[0]
                    self.check_arguments(device, self.status(op)["args"])
                    self.db.execute("UPDATE physical_ops SET state='CLAIMED' WHERE op=?", (op,))
                    self.event(resource,self.claim_kind + ".claimed",op)
                    return dict(self.status(op),kind=self.claim_kind)
            else:
                selected = self.db.execute("""SELECT op FROM physical_ops WHERE resource=? AND candidate IS NOT NULL AND
                    proof IS NULL AND state IN ('CANDIDATE','RECOVERY_REQUIRED')
                    AND json_extract(lease,'$.epoch')=? ORDER BY rowid LIMIT 1""", (resource,device["epoch"])).fetchone()
                if selected:
                    return dict(self.status(selected[0]),kind="candidate")
            if device["phase"] in ("DRAINING","QUIESCENT"):
                unresolved = self.db.execute("""SELECT 1 FROM physical_ops WHERE resource=? AND
                    (state IN ('RESERVED','OFFERED','CLAIMED','CANDIDATE') OR (state IN ('SUCCEEDED','FAILED') AND settled=0))
                    LIMIT 1""",(resource,)).fetchone()
                if not unresolved:
                    if not (device["camera_instance"] and device["verifier_instance"]):
                        return None
                    key = (resource,device["epoch"],self.boot)
                    request = self.db.execute("SELECT challenge FROM physical_stop_requests WHERE resource=? AND epoch=? AND boot=?",key).fetchone()
                    if request is None:
                        request = (uuid.uuid4().hex,)
                        self.db.execute("INSERT INTO physical_stop_requests VALUES(?,?,?,?,?)", (*key,request[0],self.clock()))
                        self.event(resource,"stop.requested",stop_id=request[0])
                    return dict(kind="stop",resource_id=resource,plugin_id=pin["plugin_id"],boot_id=self.boot,stop_id=request[0],
                                epoch=device["epoch"],camera_instance=device["camera_instance"],verifier_instance=device["verifier_instance"])
        return None

    def replace_instance(self, resource, role, previous, instance):
        """Hand an idle process slot to a restarted process after the controller stopped hearing the old one.

        Requests are fenced by the attached instance ID, so the earlier process cannot report or claim work
        afterwards. Work already offered, claimed or awaiting verification keeps its original owner until its
        lease expires and it becomes RECOVERY_REQUIRED."""
        require(role in ("camera","verifier") and isinstance(instance,str) and
                re.fullmatch("[a-zA-Z0-9_.-]{1,128}",instance), "plugin.permission_denied", "authenticated instance required")
        self.expire()
        with self.write():
            device = self.device(resource)
            require(device[role+"_instance"] == previous and previous != instance, "plugin.fence_rejected",
                    "different process incarnation")
            other = device["verifier_instance" if role == "camera" else "camera_instance"]
            require(instance != other, "plugin.verifier_invalid", "device and verifier must have separate processes")
            live = self.db.execute("""SELECT 1 FROM physical_ops WHERE resource=? AND
                state IN ('OFFERED','CLAIMED','CANDIDATE') LIMIT 1""", (resource,)).fetchone()
            require(not live, "plugin.fence_rejected", "different process incarnation holds unresolved work")
            self.db.execute("UPDATE physical_device SET " + role + "=? WHERE resource=?", (instance,resource))
            self.event(resource, role + ".replaced", previous_instance=previous, instance=instance)
        return self.device(resource)

    def candidate(self, lease, measurement):
        op = lease["operation_id"]
        with self.write():
            row = self.status(op)
            if row["candidate"] is not None:
                require(canonical(row["lease"]) == canonical(lease) and canonical(row["candidate"]) == canonical(measurement),
                        "plugin.fence_rejected","candidate changed")
                return row
            self.assert_active(lease)
            artifact = f"frames/{hashlib.sha256(op.encode()).hexdigest()}-{lease['epoch']}.cdr"
            require(set(measurement) == {"artifact_ref","artifact_sha256","frame_id","source_id","stamp_ns"}
                    and measurement["artifact_ref"] == artifact and SHA.fullmatch(measurement["artifact_sha256"])
                    and ref(measurement["frame_id"]) and measurement["source_id"] == row["pin"]["sensor"]["source_id"]
                    and number(measurement["stamp_ns"]),
                    "tool.receipt_invalid","invalid captured measurement")
            self.db.execute("UPDATE physical_ops SET state='CANDIDATE',candidate=? WHERE op=?", (canonical(measurement),op))
            self.event(lease["resource_id"],"capture.candidate",op,artifact_sha256=measurement["artifact_sha256"])
        return self.status(op)

    def unknown(self, lease, code):
        op = lease["operation_id"]
        require(public_reference(code),"tool.receipt_invalid","invalid fault code")
        with self.write():
            require(canonical(lease) == canonical(self.status(op)["lease"]),"plugin.fence_rejected","fault has stale lease")
            self._unknown(op, code)
        return self.status(op)

    def verify(self, proof, *, source):
        op = proof["operation_id"]
        with self.write():
            row = self.status(op)
            pin, lease, sample = row["pin"],row["lease"],row["candidate"]
            device = self._match(pin)
            require(source == pin["verifier_id"] and device["verifier_instance"] and
                    device["verifier_instance"] != device["camera_instance"], "plugin.verifier_invalid","wrong verifier")
            if canonical(row["proof"]) == canonical(proof):
                return row
            require(row["state"] in ("CANDIDATE","RECOVERY_REQUIRED") and sample and lease and
                    set(proof) == {"operation_id","resource_id","epoch","frame_id","stamp_ns","source_id","artifact_sha256",
                                   "verdict","evidence_ref"} and proof["resource_id"] == device["resource_id"]
                    and number(proof["epoch"]) and proof["epoch"] == lease["epoch"] == device["epoch"]
                    and number(proof["stamp_ns"]) and all(proof[k] == sample[k] for k in ("frame_id","stamp_ns","source_id","artifact_sha256"))
                    and proof["verdict"] in ("pass","fail") and ref(proof["evidence_ref"]),
                    "plugin.verifier_invalid","independent receipt does not bind the original sample")
            state = "SUCCEEDED" if proof["verdict"] == "pass" else "FAILED"
            self.db.execute("UPDATE physical_ops SET state=?,proof=?,reason=NULL WHERE op=?", (state,canonical(proof),op))
            self.event(device["resource_id"],"verification."+proof["verdict"],op,evidence_ref=proof["evidence_ref"])
        return self.status(op)

    def settle(self, pin):
        with self.write():
            row = self.status(pin["reservation_id"])
            require(canonical(row["pin"]) == canonical(pin) and row["state"] in ("SUCCEEDED","FAILED","RESERVED","CANCELED"),
                    "plugin.handoff_unconfirmed","cannot settle unknown or dispatched operation")
            if not row["settled"]:
                self.db.execute("""UPDATE physical_ops SET settled=1,state=CASE WHEN state='RESERVED'
                    THEN 'CANCELED' ELSE state END WHERE op=?""",(row["operation_id"],))
                self.event(pin["resource_id"],"settled",row["operation_id"])
        return True

    def drain(self, resource):
        with self.write():
            if self.device(resource)["phase"] == "ACTIVE":
                self.db.execute("UPDATE physical_device SET phase='DRAINING' WHERE resource=?", (resource,))
                self.event(resource,"draining")
        return self.device(resource)

    def confirm_stopped(self, resource, proof, *, source):
        with self.write():
            device = self.device(resource)
            pin = device["deployment"]["pin"]
            require(source == pin["verifier_id"] and device["verifier_instance"] and device["camera_instance"] and
                    device["camera_instance"] != device["verifier_instance"],"plugin.verifier_invalid","wrong stop witness")
            if isinstance(proof,dict) and canonical(device["stop_proof"]) == canonical(proof):
                return device
            pending = self.db.execute("""SELECT 1 FROM physical_ops WHERE resource=? AND settled=0 AND
                state<>'RECOVERY_REQUIRED' LIMIT 1""", (resource,)).fetchone()
            request = self.db.execute("SELECT challenge,issued_ns FROM physical_stop_requests WHERE resource=? AND epoch=? AND boot=?",
                                      (resource,device["epoch"],self.boot)).fetchone()
            require(request and not pending and device["phase"] == "DRAINING" and proof.get("resource_id") == resource and
                    proof.get("plugin_id") == pin["plugin_id"] and proof.get("boot_id") == self.boot and
                    proof.get("stop_id") == request[0] and proof.get("camera_instance") == device["camera_instance"] and
                    proof.get("verifier_instance") == device["verifier_instance"] and
                    number(proof.get("epoch")) and proof["epoch"] == device["epoch"] and proof.get("verdict") == "stopped"
                    and type(proof.get("subscription_count")) is int and proof["subscription_count"] == 0
                    and proof.get("source_alive") is True and number(proof.get("observations"),2)
                    and number(proof.get("window_ns"),device["deployment"]["min_quiet_ns"])
                    and proof["window_ns"] <= self.clock()-request[1]
                    and ref(proof.get("evidence_ref")), "plugin.stop_unconfirmed","quiescence unproven")
            self.db.execute("UPDATE physical_device SET phase='QUIESCENT',stop_proof=? WHERE resource=?",
                            (canonical(proof), resource))
            self.event(resource,"stop.confirmed",evidence_ref=proof["evidence_ref"])
        return self.device(resource)

    def release(self, resource):
        with self.write():
            phase = self.device(resource)["phase"]
            require(phase in ("QUIESCENT","RELEASED","STOPPED"),"plugin.stop_unconfirmed","independent stop proof required")
            if phase == "QUIESCENT":
                self.db.execute("UPDATE physical_device SET phase='RELEASED',epoch=epoch+1 WHERE resource=?", (resource,))
                self.event(resource,"lease.released")
        return self.device(resource)

    def process_stopped(self, resource, inspect):
        with self.write():
            device = self.device(resource)
            owner = device["camera_instance"]
            config = inspect.get("Config",{})
            argv = (config.get("Entrypoint") or []) + (config.get("Cmd") or [])
            if argv[:1]==["/usr/local/bin/edge-entrypoint"]:
                argv = argv[1:]
            # containerd's .Image may identify the multi-architecture index. The
            # engine supplies the actual platform manifest separately. If present
            # it must match; an index, tag or label cannot override a mismatch.
            manifest = inspect.get("ImageManifestDescriptor")
            if manifest is None:
                image_matches = inspect.get("Image") == device["deployment"]["image_config_digest"]
            else:
                image_matches = isinstance(manifest,dict) and manifest.get("digest")==device["deployment"]["pin"]["image_digest"] and (
                    manifest.get("mediaType") in ("application/vnd.oci.image.manifest.v1+json",
                                                  "application/vnd.docker.distribution.manifest.v2+json"))
            require(device["phase"] in ("RELEASED","STOPPED") and owner and
                    re.fullmatch("[a-f0-9]{12,64}",owner) and re.fullmatch("[a-f0-9]{64}",inspect.get("Id",""))
                    and config.get("Hostname")==owner and image_matches
                    and argv==device["deployment"]["camera_argv"]
                    and inspect["Id"].startswith(owner) and inspect.get("State",{}).get("Running") is False
                    and inspect["State"].get("Status") in ("exited","dead"),
                    "plugin.handoff_unconfirmed","deployment inspection did not confirm original container exit")
            if device["phase"] == "RELEASED":
                self.db.execute("UPDATE physical_device SET phase='STOPPED',exit_proof=? WHERE resource=?",
                                (digest(inspect),resource))
                self.event(resource,"process.stopped",container=owner)
        return self.device(resource)

    def diagnostic_config(self, resource, raw):
        observed = "sha256:" + hashlib.sha256(raw).hexdigest()
        with self.write():
            old = self.db.execute("SELECT interval_ms,accepted,observed FROM physical_config WHERE resource=?", (resource,)).fetchone()
            previous, previous_sha, last = old or (60000,None,None)
            if last == observed:
                return previous
            try:
                config = json.loads(raw)
                valid = len(raw) <= 4096 and isinstance(config,dict) and set(config) == {"diagnostic_interval_ms"} and number(
                    config["diagnostic_interval_ms"],1000) and config["diagnostic_interval_ms"] <= 3600000
            except (ValueError,RecursionError):
                valid = False
            interval = config["diagnostic_interval_ms"] if valid else previous
            accepted = observed if valid else previous_sha or digest({"diagnostic_interval_ms":previous})
            self.db.execute("INSERT OR REPLACE INTO physical_config VALUES(?,?,?,?)", (resource,interval,accepted,observed))
            self.event(resource,"config.applied" if valid else "config.rejected", previous_digest=previous_sha,
                       observed_digest=observed, accepted_digest=accepted, diagnostic_interval_ms=interval)
            return interval
