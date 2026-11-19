"""Validate one factory capture, publish a revision, or read the latest scoped result."""
import argparse
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
import sys

from agent.module_journal import ModuleJournal, canonical
from .checks import assess, ident, obj

VERSION = "1.0.0"
LIMIT = 8 * 1024 * 1024
DB = "module-events.sqlite"
DDL = """
CREATE TABLE IF NOT EXISTS factory_attempt(
  seq INTEGER PRIMARY KEY, operation TEXT NOT NULL UNIQUE, robot TEXT, env TEXT,
  profile_hash TEXT NOT NULL, capture_hash TEXT NOT NULL, report TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS factory_scope ON factory_attempt(robot,env,seq DESC);
CREATE TABLE IF NOT EXISTS factory_input(hash TEXT PRIMARY KEY, body TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS factory_revision(revision TEXT PRIMARY KEY, body TEXT NOT NULL);
"""


def digest(text):
    return sha256(text.encode("utf-8")).hexdigest()


def archive(value):
    try:
        return canonical(value)
    except (ValueError, OverflowError):
        raw = json.dumps(value, sort_keys=True, allow_nan=True)
        return canonical({"input_error":"nonfinite_json","source_hash":digest(raw)})


def empty(robot_id, environment, code, detail):
    return {"schema_version":1,"provider_version":VERSION,"robot_id":robot_id,"environment":environment,
            "status":"NEEDS_CALIBRATION","revision":None,"last_verified_revision":None,
            "motion_authorized":False,"issues":[{"code":code,"source":"factory","detail":detail}]}


def verify(profile, capture, state, operation, trace_id=None):
    if not ident(operation) or trace_id is not None and not ident(trace_id):
        raise ValueError("valid operation and trace identifiers are required")
    trace_id = trace_id or operation
    p,c = archive(profile),archive(capture)
    if max(len(p.encode()),len(c.encode())) > LIMIT:
        raise ValueError("one factory input exceeds 8 MiB")
    ph,ch = digest(p),digest(c)
    issues,stats = assess(profile,capture)
    for name,body in (("profile",p),("capture",c)):
        if obj(json.loads(body)).get("input_error"):
            issues.append({"code":"input.invalid","source":name,"detail":"输入文件损坏、非有限数值或格式非法。"})
    profile = obj(profile)
    robot = profile.get("robot_id") if ident(profile.get("robot_id")) else None
    env = profile.get("environment") if profile.get("environment") in ("simulation","hardware") else None
    with closing(ModuleJournal(Path(state)/DB)) as journal:
        db = journal.db
        db.executescript(DDL)
        with db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT profile_hash,capture_hash,report FROM factory_attempt WHERE operation=?",
                             (operation,)).fetchone()
            if old:
                result=json.loads(old[2])
                if (ph,ch,trace_id) != (old[0],old[1],result["trace_id"]):
                    conflict=empty(robot,env,"operation.conflict","此操作 ID 已绑定其他输入或任务链，请使用新的 ID。")
                    event={"module":"calibration.factory","kind":"factory.operation_conflict","code":"operation.conflict",
                           "level":"warn","trace_id":trace_id,"operation_id":operation,"parent_event_id":result["event_id"],
                           "data":{"profile_sha256":ph,"capture_sha256":ch}}
                    conflict.update(operation_id=operation,trace_id=trace_id,
                                    event_id=journal.emit(event,key=digest(canonical(event))))
                    return conflict
                return result
            previous = db.execute("SELECT report FROM factory_attempt WHERE robot=? AND env=? ORDER BY seq DESC LIMIT 1",
                                  (robot,env)).fetchone()
            prior = json.loads(previous[0]) if previous else {}
            # A new attempt may fix a failed profile against the same retained capture.
            # Superseded successful evidence cannot re-qualify the assembly after an intervening check.
            if not issues and prior.get("status") == "NEEDS_CALIBRATION":
                prior_pass = db.execute(
                    "SELECT 1 FROM factory_attempt WHERE robot=? AND env=? AND capture_hash=? "
                    "AND json_extract(report, '$.status')='VERIFIED' LIMIT 1", (robot,env,ch)).fetchone()
                if prior_pass:
                    issues.append({"code":"capture.superseded","source":"capture",
                                   "detail":"上次复核未通过。请为当前装配重新取得静止观测窗口。"})
            result={"schema_version":1,"provider_version":VERSION,"robot_id":robot,"environment":env,
                    "operation_id":operation,"trace_id":trace_id,"status":"NEEDS_CALIBRATION" if issues else "VERIFIED",
                    "motion_authorized":False,"issues":issues,"statistics":stats,"revision":None,
                    "last_verified_revision":prior.get("last_verified_revision"),"profile_sha256":ph,"capture_sha256":ch}
            if not issues:
                cal={"schema_version":1,"provider_version":VERSION,"robot_id":robot,"environment":env,
                     "base_frame":profile["base_frame"],"sensors":profile["sensors"],"extrinsics":profile["extrinsics"],
                     "camera_intrinsics":profile["camera_intrinsics"],"gyro_bias_rad_s":stats["imu"]["gyro_bias_rad_s"],
                     "stationary_reference":capture["stationary_reference"],"clock_domain":capture["clock_domain"],
                     "profile_sha256":ph,"capture_sha256":ch}
                body=canonical(cal)
                revision=digest(body)
                db.execute("INSERT OR IGNORE INTO factory_revision VALUES(?,?)",(revision,body))
                result.update(calibration=cal,revision=revision,last_verified_revision=revision)
            for fingerprint,body in ((ph,p),(ch,c)):
                db.execute("INSERT OR IGNORE INTO factory_input VALUES(?,?)",(fingerprint,body))
            kind="factory.calibration_rejected" if issues else "factory.calibration_published"
            event={"module":"calibration.factory","trace_id":trace_id,"operation_id":operation,
                   "kind":kind,"code":kind,"level":"warn" if issues else "info",
                   "data":{key:result[key] for key in ("status","issues","robot_id","environment","revision",
                                                       "profile_sha256","capture_sha256","last_verified_revision")}}
            if prior.get("event_id"):
                event["parent_event_id"]=prior["event_id"]
            result["event_id"]=journal.emit(event)
            receipt=canonical(result)
            db.execute("INSERT INTO factory_attempt(operation,robot,env,profile_hash,capture_hash,report) "
                       "VALUES(?,?,?,?,?,?)",(operation,robot,env,ph,ch,receipt))
        return json.loads(receipt)


def status(state, robot_id, environment=None):
    path=Path(state)/DB
    if not ident(robot_id) or environment not in ("simulation","hardware",None):
        raise ValueError("invalid factory scope")
    if path.is_file():
        uri=path.resolve().as_uri()+"?mode=ro"
        with closing(sqlite3.connect(uri,uri=True)) as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='factory_attempt'").fetchone():
                return empty(robot_id,environment,"factory.unverified","尚未建立工厂验收记录。")
            scopes=db.execute("SELECT DISTINCT env FROM factory_attempt WHERE robot=? AND env IS NOT NULL",
                              (robot_id,)).fetchall()
            if environment is None and len(scopes)>1:
                return empty(robot_id,None,"environment.ambiguous","请在查询中指定 hardware 或 simulation。")
            if environment is None and scopes:
                environment=scopes[0][0]
            row=db.execute("SELECT report FROM factory_attempt WHERE robot=? AND env=? ORDER BY seq DESC LIMIT 1",
                           (robot_id,environment)).fetchone()
            if row:
                return json.loads(row[0])
    return empty(robot_id,environment,"factory.unverified","尚未找到此机体、此环境的工厂验收记录。")


def read_input(path):
    with path.open("rb") as f:
        raw=f.read(LIMIT+1)
    if len(raw)>LIMIT:
        raise ValueError("input exceeds 8 MiB")
    def reject_constant(value):
        raise ValueError("non-finite JSON constant")
    try:
        return json.loads(raw,parse_constant=reject_constant)
    except (UnicodeError,ValueError,RecursionError):
        return {"input_error":"invalid_json","source_hash":sha256(raw).hexdigest()}


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    sub=cli.add_subparsers(dest="action",required=True)
    run=sub.add_parser("verify")
    run.add_argument("--profile",type=Path,required=True)
    run.add_argument("--capture",type=Path,required=True)
    run.add_argument("--operation",required=True)
    run.add_argument("--trace-id")
    show=sub.add_parser("status")
    show.add_argument("--robot-id",required=True)
    show.add_argument("--environment",choices=("simulation","hardware"))
    for command in (run,show):
        command.add_argument("--state",type=Path,required=True)
    args=cli.parse_args()
    try:
        if args.action=="verify":
            profile=read_input(args.profile)
            try:
                capture=read_input(args.capture)
            except (OSError,ValueError) as exc:
                capture={"input_error":"capture.unreadable","acquisition_errors":{"station":str(exc)}}
            result=verify(profile,capture,args.state,args.operation,args.trace_id)
        else:
            result=status(args.state,args.robot_id,args.environment)
    except (OSError,sqlite3.Error,ValueError,RecursionError) as error:
        result=empty(None,None,"factory.io",str(error))
    # JSON transport must remain portable when Windows stdout uses a legacy codepage.
    print(json.dumps(result,ensure_ascii=True,allow_nan=False))
    return 0 if result["status"]=="VERIFIED" else 2


if __name__=="__main__":
    sys.exit(main())
