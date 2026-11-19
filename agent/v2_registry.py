"""Transactional plugin-admin state, immutable descriptors and frozen soft-plan reservations."""
from contextlib import closing, contextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid

from author_tool import public_reference
from module_journal import ModuleJournal, canonical
from planning import BASE
from plugin_descriptor import NAME, load_descriptor


class RegistryError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def database(root):
    return Path(root) / "v2" / ".history" / "module-events.sqlite"


def catalog(db):
    rows = db.execute("""SELECT r.descriptor, p.revision, p.draining, p.disposed, p.generation,
        (SELECT count(*) FROM v2_reservations AS b WHERE b.plugin_id=p.id AND b.settled=0)
        FROM v2_pointers AS p JOIN v2_releases AS r ON p.id=r.id AND p.digest=r.digest ORDER BY p.id""")
    return [{"descriptor":json.loads(raw), "revision":revision, "draining":bool(draining),
             "disposed":bool(disposed), "generation":generation, "frozen":frozen}
            for raw, revision, draining, disposed, generation, frozen in rows]


def claims(db):
    owners = {}
    for raw, in db.execute("SELECT descriptor FROM v2_releases ORDER BY id,version"):
        previous = json.loads(raw)
        owners.update((spec["name"],previous["id"]) for spec in previous["provides"])
    return owners


def immutable_version(db, item):
    seen = db.execute("SELECT digest FROM v2_releases WHERE id=? AND version=?", (item["id"],item["version"])).fetchone()
    if seen and seen[0] != item["content_digest"]:
        raise RegistryError("plugin.digest_mismatch","existing semantic version has different contents")


def admissible(item, entries, owners):
    if item["class"] != "soft":
        raise RegistryError("plugin.physical_hot_reload_forbidden", "physical releases require the deployment channel")
    policy = item["policy"]
    if "release-admin" not in policy["install"] or "site-operator" not in policy["enable"] or policy["site_approval"]:
        raise RegistryError("plugin.permission_denied", "the soft release channel cannot satisfy this site's approval requirements")
    for spec in item["provides"]:
        if spec["name"] in BASE:
            raise RegistryError("plugin.permission_denied", "the execution kernel owns " + spec["name"])
        if owners.get(spec["name"],item["id"]) != item["id"]:
            raise RegistryError("plugin.service_conflict", "export has a different historical owner: " + spec["name"])
    # A missing service leaves its consumer pending. An ambiguous service or a known
    # type mismatch cannot be made safe by waiting.
    items = [r["descriptor"] for r in entries if r["descriptor"]["id"] != item["id"]] + [item]
    providers = {}
    for descriptor in items:
        for spec in descriptor["provides"]:
            if spec["name"] in providers:
                raise RegistryError("plugin.service_conflict", "multiple packages export " + spec["name"])
            providers[spec["name"]] = (descriptor["id"], spec)
    graph = {descriptor["id"]:set() for descriptor in items}
    missing = []
    for descriptor in items:
        for spec in descriptor["requires"]:
            provider = providers.get(spec["name"])
            if provider is None:
                if descriptor["id"] == item["id"]:
                    missing.append(spec["name"])
            else:
                owner, export = provider
                if (export["type"], export["schema_ref"]) != (spec["type"], spec["schema_ref"]):
                    raise RegistryError("plugin.dependency_unavailable", "incompatible export " + spec["name"])
                graph[descriptor["id"]].add(owner)
    # Kahn's algorithm also handles arbitrarily long dependency chains.
    dependents = {owner:set() for owner in graph}
    ready = [owner for owner, deps in graph.items() if not deps]
    for consumer, deps in graph.items():
        for owner in deps:
            dependents[owner].add(consumer)
    seen = 0
    while ready:
        owner = ready.pop()
        seen += 1
        for consumer in dependents[owner]:
            graph[consumer].remove(owner)
            if not graph[consumer]:
                ready.append(consumer)
    if seen != len(graph):
        raise RegistryError("plugin.dependency_cycle", "dependencies form a cycle")
    return sorted(missing)


class V2Registry:
    def __init__(self, root):
        self.journal = ModuleJournal(database(root))
        self.db = self.journal.db
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS v2_releases(
                id TEXT NOT NULL, version TEXT NOT NULL, digest TEXT NOT NULL, descriptor TEXT NOT NULL,
                PRIMARY KEY(id,version), UNIQUE(id,digest));
            CREATE TABLE IF NOT EXISTS v2_pointers(
                id TEXT PRIMARY KEY, digest TEXT, revision TEXT NOT NULL, generation INTEGER NOT NULL,
                draining INTEGER NOT NULL, disposed INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS v2_receipts(
                request_key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, result TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS v2_reservations(
                plugin_id TEXT NOT NULL, digest TEXT NOT NULL, operation_id TEXT NOT NULL, settled INTEGER NOT NULL,
                PRIMARY KEY(plugin_id,operation_id));
            CREATE TABLE IF NOT EXISTS v2_plans(task_id TEXT PRIMARY KEY, plan TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS v2_pack_policy(singleton INTEGER PRIMARY KEY, lock_digest TEXT NOT NULL, allowed TEXT NOT NULL);
        """)

    def close(self):
        self.journal.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    @contextmanager
    def _write(self):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            yield

    def entries(self):
        return catalog(self.db)

    def claimed_exports(self):
        return claims(self.db)

    def install_pack(self, items, lock_digest):
        """Activate a verified site snapshot as a single additive admission; handoffs stay explicit."""
        from argparse import Namespace
        desired={item["id"]:item for item in items}
        trace=uuid.uuid4().hex
        failure=None
        accepted=[]
        with self._write():
            before=self.entries()
            cause=self._event("requested","release.pack_requested",trace,None,{"lock":lock_digest})
            self.db.execute("SAVEPOINT pack")
            try:
                for row in before:
                    old=row["descriptor"]
                    if old["id"] not in desired or old["content_digest"]!=desired[old["id"]]["content_digest"] or row["draining"]:
                        raise RegistryError("version_conflict","perform the explicit drain and handoff before changing this pack")
                allowed={key:item["content_digest"] for key,item in desired.items()}
                self.db.execute("INSERT OR REPLACE INTO v2_pack_policy VALUES(1,?,?)",(lock_digest,canonical(allowed)))
                live={row["descriptor"]["id"] for row in before}
                for key,item in sorted(desired.items()):
                    if key in live:
                        continue
                    pointer=self.db.execute("SELECT revision FROM v2_pointers WHERE id=?",(key,)).fetchone()
                    self._apply(Namespace(action="publish",expect=pointer[0] if pointer else "absent"),key,item,trace,cause)
                    accepted.append(key)
            except RegistryError as exc:
                self.db.execute("ROLLBACK TO pack")
                failure=exc
            self.db.execute("RELEASE pack")
            status="rejected" if failure else "applied"
            event=self._event(status,failure.code if failure else "release.pack_applied",trace,cause,
                              {"lock":lock_digest,"count":0 if failure else len(accepted)})
        if failure:
            raise failure
        return {"admitted":accepted,"event_id":event}

    def lookup(self, plugin_id, digest):
        found = self.db.execute("SELECT descriptor FROM v2_releases WHERE id=? AND digest=?", (plugin_id,digest)).fetchone()
        return json.loads(found[0]) if found else None

    def _reserve(self, plugin_id, digest, operation_id):
        if not public_reference(operation_id):
            raise RegistryError("plugin.manifest_invalid", "invalid operation identity")
        bound = self.db.execute("SELECT digest,settled FROM v2_reservations WHERE plugin_id=? AND operation_id=?",
                                (plugin_id,operation_id)).fetchone()
        if bound:
            if bound != (digest,0):
                raise RegistryError("tool.binding_unavailable", "operation was settled or pinned to another release")
            return
        pointer = self.db.execute("SELECT digest,draining FROM v2_pointers WHERE id=?", (plugin_id,)).fetchone()
        if not pointer or pointer[0] != digest:
            raise RegistryError("tool.binding_unavailable", "requested release is unavailable")
        if pointer[1]:
            raise RegistryError("plugin.draining", "new reservations are closed")
        self.db.execute("INSERT INTO v2_reservations VALUES(?,?,?,0)", (plugin_id,digest,operation_id))

    def reserve(self, plugin_id, digest, operation_id):
        with self._write():
            self._reserve(plugin_id,digest,operation_id)

    def settle(self, plugin_id, digest, operation_id):
        with self._write():
            cursor = self.db.execute("""UPDATE v2_reservations SET settled=1
                WHERE plugin_id=? AND digest=? AND operation_id=?""", (plugin_id,digest,operation_id))
            if cursor.rowcount != 1:
                raise RegistryError("tool.binding_unavailable", "no matching reservation")

    def reservations(self):
        return self.db.execute("""SELECT plugin_id,digest,operation_id FROM v2_reservations
            WHERE settled=0 ORDER BY plugin_id,operation_id""").fetchall()

    def settle_operation(self, operation_id, expected):
        """Release the complete frozen dependency set after terminal evidence, including idempotent retries."""
        with self._write():
            stored = dict(self.db.execute("SELECT plugin_id,digest FROM v2_reservations WHERE operation_id=?",
                                          (operation_id,)))
            if not stored or stored != expected:
                raise RegistryError("tool.binding_unavailable", "settlement does not match the complete reservation")
            self.db.execute("UPDATE v2_reservations SET settled=1 WHERE operation_id=?", (operation_id,))

    def reservations_for(self, operation_id):
        return dict(self.db.execute("SELECT plugin_id,digest FROM v2_reservations WHERE operation_id=? AND settled=0",
                                    (operation_id,)))

    def saved_plan(self, task_id):
        row = self.db.execute("SELECT plan FROM v2_plans WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def freeze_plan(self, task_id, provider, steps, bindings):
        if not public_reference(task_id):
            raise RegistryError("plugin.manifest_invalid", "invalid task identity")
        with self._write():
            existing = self.saved_plan(task_id)
            if existing is not None:
                return existing
            plan = [provider,steps]
            for plugin_id,digest,operation_id in bindings:
                self._reserve(plugin_id,digest,operation_id)
            self.db.execute("INSERT INTO v2_plans VALUES(?,?)", (task_id,canonical(plan)))
            return plan

    def ack_disposed(self, plugin_id, revision):
        """Only the trusted host calls this after all Fiber disposers have completed."""
        with self._write():
            row = self.db.execute("""SELECT digest,version,p.disposed FROM v2_pointers AS p
                JOIN v2_releases USING(id,digest) WHERE id=? AND revision=? AND draining=1""",
                                  (plugin_id,revision)).fetchone()
            if not row or self._frozen(plugin_id):
                raise RegistryError("plugin.cleanup_incomplete", "old generation is not drained")
            if not row[2]:
                self.db.execute("UPDATE v2_pointers SET disposed=1 WHERE id=?", (plugin_id,))
                self._event("disposed","plugin.disposed",uuid.uuid4().hex,None,
                            {"plugin_id":plugin_id,"version":row[1],"content_digest":row[0],"revision":revision})

    def _frozen(self, plugin_id):
        return self.db.execute("SELECT count(*) FROM v2_reservations WHERE plugin_id=? AND settled=0",
                               (plugin_id,)).fetchone()[0]

    def _event(self, kind, code, trace_id, parent_id, data):
        return self.journal.emit({"module":"plugin.manager", "layer":"publication", "kind":"plugin.release."+kind,
                                  "code":code, "level":"warn" if kind=="rejected" else "info", "trace_id":trace_id,
                                  "parent_event_id":parent_id, "data":data})

    def _apply(self, args, name, item, trace, cause):
        action = args.action
        row = self.db.execute("SELECT digest,revision,generation,draining,disposed FROM v2_pointers WHERE id=?",
                              (name,)).fetchone()
        digest,previous,generation,draining,disposed = row or (None,"absent",0,0,0)
        old = self.lookup(name,digest)
        if previous != args.expect:
            raise RegistryError("version_conflict","registry changed; current revision is " + previous)
        frozen = self._frozen(name)
        missing = []
        if action == "publish":
            site=self.db.execute("SELECT allowed FROM v2_pack_policy WHERE singleton=1").fetchone()
            if site and json.loads(site[0]).get(name) != item["content_digest"]:
                raise RegistryError("plugin.permission_denied","release is outside the installed board profile")
            missing = admissible(item,self.entries(),self.claimed_exports())
            immutable_version(self.db,item)
            if old and (not draining or not disposed or frozen):
                raise RegistryError("plugin.cleanup_incomplete","drain and dispose the old release before updating")
            if old and not old["hot_reload"]:
                raise RegistryError("plugin.permission_denied","old release forbids code hot updates")
            self.db.execute("INSERT OR IGNORE INTO v2_releases VALUES(?,?,?,?)",
                            (name,item["version"],item["content_digest"],canonical(item)))
            digest,draining,disposed = item["content_digest"],0,0
        elif old is None:
            raise RegistryError("already_absent","no installed release")
        elif action == "drain":
            draining = 1
        elif action == "remove":
            if not draining or frozen or not disposed:
                raise RegistryError("plugin.cleanup_incomplete","drain and confirm disposal before removal")
            digest,draining,disposed = None,1,1
        else:
            raise RegistryError("plugin.manifest_invalid","unknown action")
        if action == "publish":
            generation += 1
        revision = fingerprint([name,generation,action,digest])
        self.db.execute("INSERT OR REPLACE INTO v2_pointers VALUES(?,?,?,?,?,?)",
                        (name,digest,revision,generation,draining,disposed))
        descriptor = item or old
        result = {"action":action,"id":name,"version":descriptor["version"],"content_digest":descriptor["content_digest"],
                  "previous":previous,"revision":revision,"generation":generation,"draining":bool(draining),
                  "frozen":frozen,"missing_services":missing}
        result["event_id"] = self._event("applied","release.applied",trace,cause,result)
        return result

    def change(self, args, trust_root):
        trace = getattr(args,"trace_id",None)
        parent = getattr(args,"parent_event_id",None)
        request = getattr(args,"request_id",None)
        if not public_reference(request) or any(v is not None and not public_reference(v) for v in (trace,parent)):
            raise RegistryError("release.invalid_reference","provide a public request, trace and parent reference")
        name = getattr(args,"id",None)
        item = None
        invalid = None
        expect_valid = args.expect == "absent" or (isinstance(args.expect,str) and len(args.expect)==64
                          and all(c in "0123456789abcdef" for c in args.expect))
        data = {"action":args.action,"expect":args.expect if expect_valid else "invalid","request_id":request}
        if args.action == "publish":
            try:
                item = load_descriptor(args.definition,trust_root)
                name = item["id"]
                data.update(version=item["version"],content_digest=item["content_digest"])
            except (ValueError,OSError) as exc:
                invalid = RegistryError(getattr(exc,"code","plugin.manifest_invalid"), "candidate failed validation")
                try:
                    with Path(args.definition).open("rb") as stream:
                        part = stream.read(131073)
                    data.update(input_sha256=hashlib.sha256(part).hexdigest(),prefix_only=len(part)>131072)
                except OSError:
                    data["input_unreadable"] = True
        if name is not None:
            if not isinstance(name,str) or NAME.fullmatch(name) is None:
                invalid = RegistryError("plugin.manifest_invalid","invalid plugin identifier")
                data["invalid_id_hash"] = fingerprint(name)
            else:
                data["plugin_id"] = name
        intent = fingerprint([data,trace,parent])
        trace = trace or uuid.uuid4().hex
        key = fingerprint(request)
        failure = None
        with self._write():
            prior = self.db.execute("SELECT fingerprint,result FROM v2_receipts WHERE request_key=?", (key,)).fetchone()
            if prior and prior[0] == intent:
                result = json.loads(prior[1])
                if "error" in result:
                    raise RegistryError(result["error"],result["detail"])
                return result
            audit = dict(data)
            if args.action in ("drain","remove") and name is not None and not invalid:
                old = self.db.execute("SELECT version,digest FROM v2_pointers JOIN v2_releases USING(id,digest) WHERE id=?",
                                      (name,)).fetchone()
                if old:
                    audit.update(version=old[0],content_digest=old[1])
            cause = self._event("requested","release.requested",trace,parent,audit)
            if not expect_valid:
                invalid = RegistryError("version_conflict","expect must be an installed CAS revision or absent")
            if prior:
                invalid = RegistryError("request_conflict","request identifier belongs to a different publication")
            self.db.execute("SAVEPOINT publication")
            try:
                if invalid:
                    raise invalid
                result = self._apply(args,name,item,trace,cause)
            except RegistryError as exc:
                self.db.execute("ROLLBACK TO publication")
                failure = exc
                result = {"error":exc.code,"detail":str(exc).removeprefix(exc.code+": ")}
                self._event("rejected",exc.code,trace,cause,audit)
            self.db.execute("RELEASE publication")
            if not prior:
                self.db.execute("INSERT INTO v2_receipts VALUES(?,?,?)", (key,intent,canonical(result)))
        if failure:
            raise failure
        return result


def command(args):
    if getattr(args,"cases",None) is not None:
        raise RegistryError("plugin.manifest_invalid","--cases applies to the derived definition registry")
    configured = os.environ.get("EDGE_PLUGIN_TRUST_ROOT")
    supplied = getattr(args,"trust_root",None)
    if configured and supplied and Path(configured).resolve() != Path(supplied).resolve():
        raise RegistryError("plugin.permission_denied","the configured deployment trust root is authoritative")
    trust_root = configured or supplied
    if not trust_root:
        raise RegistryError("plugin.permission_denied","configure the site trust root")
    if args.action in ("check","validate"):
        item = load_descriptor(args.definition,trust_root)
        result = {key:item[key] for key in ("id","version","class","content_digest")}
        if args.action == "check":
            path = database(args.root)
            current,entries,owners = "absent",[],{}
            if path.exists():
                with closing(sqlite3.connect(path.resolve().as_uri()+"?mode=ro",uri=True)) as db:
                    db.execute("BEGIN")
                    entries = catalog(db)
                    owners = claims(db)
                    row = db.execute("SELECT revision FROM v2_pointers WHERE id=?", (item["id"],)).fetchone()
                    current = row[0] if row else "absent"
                    immutable_version(db,item)
            if args.expect is not None and args.expect != current:
                raise RegistryError("version_conflict","registry changed; current revision is " + current)
            result.update(revision=current,missing_services=admissible(item,entries,owners))
        return result
    with V2Registry(args.root) as registry:
        return registry.change(args,trust_root)
