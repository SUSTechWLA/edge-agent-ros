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


class PhysicalError(ValueError):
    def __init__(self, code, detail):
        self.code = code
        super().__init__(detail)


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

    def install(self, deployment):
        """The deployment CLI has already authenticated both immutable artifacts and site approval."""
        pin = deployment["pin"]
        resource = pin["resource_id"]
        with self.write():
            existing = self.db.execute("SELECT deployment,phase FROM physical_device WHERE resource=?", (resource,)).fetchone()
            if existing and json.loads(existing[0]) == deployment and existing[1] == "ACTIVE":
                return self.device(resource)
            require(not existing or existing[1] == "STOPPED", "plugin.physical_hot_reload_forbidden",
                    "finish drain, independent stop, lease release and confirmed process exit")
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
            self.event(resource,"installed")
        return self.device(resource)

    def _match(self, pin):
        device = self.device(pin["resource_id"])
        expected = dict(device["deployment"]["pin"])
        if "reservation_id" in pin:
            expected["reservation_id"] = pin["reservation_id"]
        require(canonical(pin) == canonical(expected), "tool.binding_unavailable", "original deployment binding unavailable")
        return device

    def freeze_plan(self, task, provider, steps):
        require(public_reference(task), "plugin.manifest_invalid", "invalid task identity")
        with self.write():
            prior = self.db.execute("SELECT plan FROM physical_plans WHERE task=?", (task,)).fetchone()
            if prior:
                return json.loads(prior[0])
            for index, step in enumerate(steps, 1):
                if "physical_pin" not in step:
                    continue
                pin = dict(step["physical_pin"])
                require("reservation_id" not in pin,"tool.binding_unavailable","new plan cannot reuse another operation's pin")
                device = self._match(pin)
                require(device["phase"] == "ACTIVE", "plugin.draining", "physical admission closed")
                require(step["tool"] == pin["name"] and step["version"] == pin["version"] and
                        step["binding"] == pin["binding"] and step["args"] == {},
                        "tool.binding_unavailable", "capture operation differs from catalog")
                op = f"{task}/step/{index}"
                require(public_reference(op), "plugin.manifest_invalid", "invalid operation identity")
                pin["reservation_id"] = op
                step["physical_pin"] = pin
                self.db.execute("INSERT INTO physical_ops(op,resource,pin,args,state) VALUES(?,?,?,?,'RESERVED')",
                                (op,device["resource_id"],canonical(pin),canonical(step["args"])))
                self.event(device["resource_id"],"reserved",op)
            plan = [provider,steps]
            self.db.execute("INSERT INTO physical_plans VALUES(?,?)",(task,canonical(plan)))
        return plan

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
                    self.db.execute("UPDATE physical_ops SET state='CLAIMED' WHERE op=?", (op,))
                    self.event(resource,"capture.claimed",op)
                    return dict(self.status(op),kind="capture")
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
            require(device["phase"] in ("RELEASED","STOPPED") and owner and
                    re.fullmatch("[a-f0-9]{12,64}",owner) and re.fullmatch("[a-f0-9]{64}",inspect.get("Id",""))
                    and config.get("Hostname")==owner and inspect.get("Image")==device["deployment"]["image_config_digest"]
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
