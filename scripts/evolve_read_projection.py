"""Wait for one specified capability gap, qualify one projection and resume its original task."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen


NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
SHA = re.compile(r"sha256:[0-9a-f]{64}")
FLAG = re.compile(r"[a-z][a-z0-9-]{0,40}")
MOUNT_DEST = re.compile(r"/inputs/[a-zA-Z0-9][a-zA-Z0-9._-]*(?:/[a-zA-Z0-9][a-zA-Z0-9._-]*){0,2}")


def require(ok, message):
    if not ok:
        raise ValueError(message)


def hash_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def encoded(obj):
    return (json.dumps(obj, ensure_ascii=False, allow_nan=False, sort_keys=True) + "\n").encode()


def read(path, limit=1024*1024):
    require(path.is_file() and not path.is_symlink(), "expected regular evidence file")
    with path.open("rb") as stream:
        data = stream.read(limit+1)
    require(0 < len(data) <= limit, "invalid evidence size")
    return data


def persist_once(path, data):
    if path.exists():
        require(read(path) == data, "existing evidence differs: " + path.name)
        return
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        pending = Path(tmp.name)
        try:
            os.fchmod(tmp.fileno(),0o640)
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
            os.link(pending, path)
        finally:
            pending.unlink(missing_ok=True)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def mount(source, target, *, volume=False, writable=False):
    source = str(source)
    require(not any(c in source+target for c in ',\n\r"'), "unsupported mount path")
    if volume:
        require(NAME.fullmatch(source), "invalid Docker volume name")
    return ["--mount", "type=" + ("volume" if volume else "bind") + ",source=" + source + ",target=" + target +
            (",volume-nocopy" if volume else "") + ("" if writable else ",readonly")]


def docker(*argv, timeout=100):
    result = subprocess.run(["docker", *map(str,argv)], stdout=subprocess.PIPE, timeout=timeout, check=True)
    require(len(result.stdout) <= 1024*1024, "container output exceeds limit")
    return result.stdout


def wait_task(task, port, timeout):
    url = "http://127.0.0.1:" + str(port) + "/agent/tasks?" + urlencode({"id":task})
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=min(3.,max(.05,deadline-time.monotonic()))) as response:
                raw = response.read(1024*1024+1)
            require(len(raw) <= 1024*1024, "task response exceeds limit")
            row = json.loads(raw)
            require(row.get("id") == task, "agent task identity mismatch")
            if row.get("state") == "BLOCKED":
                return row
            require(row.get("state") not in {"SUCCEEDED","FAILED","RECOVERY_REQUIRED","CANCELED","CANCELLED"},
                    "original task ended without a capability gap")
        except HTTPError as error:
            if error.code not in (404,503):
                raise
        except (TimeoutError, URLError):
            pass
        time.sleep(min(.3,max(0.,deadline-time.monotonic())))
    raise ValueError("wait for original BLOCKED task timed out")


def settings(args):
    cfg = json.loads(read(args.site_env,32768))
    require(isinstance(cfg,dict) and set(cfg) <= {"validator","validator_sha256","profile","actor","facts_network",
            "mounts","arguments","inputs","validator_timeout"} and all(k in cfg for k in
            ("validator","profile","actor","facts_network","mounts","arguments")), "invalid trusted validator configuration")
    require(all(isinstance(cfg[k],str) and NAME.fullmatch(cfg[k]) for k in ("actor","facts_network")) and
            isinstance(cfg["mounts"],dict) and 0<len(cfg["mounts"])<=12 and isinstance(cfg["arguments"],dict)
            and len(cfg["arguments"]) <= 16, "invalid trusted validator bindings")
    require(all(MOUNT_DEST.fullmatch(k) and isinstance(v,str) and NAME.fullmatch(v) for k,v in cfg["mounts"].items())
            and len(set(cfg["mounts"].values()))==len(cfg["mounts"]) and args.registry_volume not in cfg["mounts"].values(),
            "validator must use separate named read-only input volumes")
    require(all(not b.startswith(a+"/") for a in cfg["mounts"] for b in cfg["mounts"]), "overlapping validator mounts")
    reserved={"candidate-digest","backend-digest","profile","actor","timeout","inputs"}
    require(all(FLAG.fullmatch(k) and k not in reserved and isinstance(v,str) and NAME.fullmatch(v)
                for k,v in cfg["arguments"].items()) and len(set(cfg["arguments"].values()))==len(cfg["arguments"]),
            "invalid validator argument binding")
    require(cfg.get("inputs","/inputs") == "/inputs" and type(cfg.get("validator_timeout",30)) is int
            and 1<=cfg.get("validator_timeout",30)<=120, "invalid validation limits")
    validator = (args.source_root/cfg["validator"]).resolve(strict=True)
    require(validator.is_relative_to(args.source_root) and validator.suffix==".py", "verifier must be in trusted source checkout")
    profile = (args.site_env.parent/cfg["profile"]).resolve(strict=True)
    vhash, phash = hash_bytes(read(validator)), hash_bytes(read(profile,32768))
    require(cfg.get("validator_sha256",vhash)==vhash, "verifier code digest changed")
    return cfg,validator,profile,vhash,phash


def evidence_events(out, task):
    path=out/"events.jsonl"
    if not path.exists():
        return {}
    rows=[json.loads(line) for line in read(path).splitlines()]
    require(all(row.get("task_id")==task for row in rows),"output belongs to another original task")
    return {row["phase"]:row for row in rows}


def validation_checkpoint(out, events, perform):
    checkpoint=out/"qualification.checkpoint.json"
    canonical=out/"qualification.json"
    if checkpoint.exists():
        saved=json.loads(read(checkpoint))
        raw=saved["content"].encode()
        require(saved["sha256"]==hash_bytes(raw) and saved["file"] in ("qualification.json","qualification-revalidated.json"),
                "independent validation checkpoint changed")
    else:
        already=events.get("candidate_verified")
        if already:
            raw=read(canonical)
            require(hash_bytes(raw)==already["validation_digest"],"previous independent validation changed")
            name=canonical.name
        else:
            raw=perform()
            name="qualification-revalidated.json" if canonical.exists() and read(canonical)!=raw else canonical.name
        saved={"content":raw.decode(),"sha256":hash_bytes(raw),"file":name}
        persist_once(checkpoint,encoded(saved))
    path=out/saved["file"]
    persist_once(path,raw)
    return path,saved["sha256"]


def run(args):
    args.source_root=args.source_root.resolve(strict=True)
    ready,approved=(p.resolve(strict=True) for p in (args.ready_site,args.approved_site))
    out=args.out.resolve()
    require(ready.is_dir() and approved.is_dir() and ready!=approved and not out.is_relative_to(ready)
            and not out.is_relative_to(approved) and out!=Path.home() and
            all(not p.is_relative_to(out) for p in (args.source_root,ready,approved)),"invalid site or evidence directories")
    require(NAME.fullmatch(args.task) and NAME.fullmatch(args.registry_volume) and 1<=args.port<=65535
            and 1<=args.timeout<=3600,"invalid task, port, registry or wait limit")
    locks=[n.removeprefix("sha256:") for n in (args.ready_lock,args.approved_lock)]
    require(all(re.fullmatch(r"[0-9a-f]{64}",n) for n in locks) and locks[0]!=locks[1], "invalid site locks")
    cfg,validator,profile,vhash,phash=settings(args)
    supervisor=args.source_root/"client/soft_projection_evolution.py"
    supervisor_sha=hash_bytes(read(supervisor))
    image=docker("image","inspect","--format","{{.Id}}",args.image).decode().strip()
    require(SHA.fullmatch(image),"invalid local image ID")
    internal=docker("network","inspect","--format","{{.Internal}}",cfg["facts_network"]).decode().strip()
    require(internal=="true","validator requires a private internal facts network")
    for vol in (args.registry_volume,*cfg["mounts"].values()):
        require(docker("volume","inspect","--format","{{.Name}}",vol).decode().strip()==vol, "expected an existing volume")
    out.mkdir(mode=0o770,parents=True,exist_ok=True)
    require(out.stat().st_uid==os.geteuid(),"evidence directory must belong to the site user")
    gid=os.getegid()
    if out.stat().st_gid!=gid:
        os.chown(out,-1,gid)
    out.chmod(0o770)
    import fcntl
    with (out/"trigger.lock").open("a") as guard:
        fcntl.flock(guard.fileno(),fcntl.LOCK_EX)
        events=evidence_events(out,args.task)
        context={"original_task":args.task,"ready_site":str(ready),"approved_site":str(approved),
                 "ready_lock":locks[0],"approved_lock":locks[1],"image":image,"registry_volume":args.registry_volume,
                 "agent_port":args.port,"supervisor_sha256":supervisor_sha,"validator_sha256":vhash,
                 "profile_sha256":phash,"site_env_sha256":hash_bytes(encoded(cfg))}
        persist_once(out/"trigger-context.json",encoded(context))
        original=wait_task(args.task,args.port,args.timeout)
        require(not original.get("steps"),"original gap already dispatched an operation")
        if not (out/"gap-task.json").exists():
            persist_once(out/"gap-task.json",encoded(original))
        else:
            saved=json.loads(read(out/"gap-task.json"))
            require(saved.get("id")==args.task and saved.get("trace_id")==original.get("trace_id") and
                    saved.get("text")==original.get("text"),"previous gap receipt differs")
        common=["run","--rm","--pull","never","--read-only","--cap-drop","ALL","--group-add",str(gid),"--security-opt","no-new-privileges",
                "--pids-limit","32","--memory","192m","--entrypoint","python3"]
        review=["--network","host","--env","EDGE_EVOLUTION_AGENT_PORT="+str(args.port),
                *mount(supervisor,"/opt/edge/client/soft_projection_evolution.py"),
                *mount(ready,"/ready"),*mount(approved,"/approved"),*mount(out,"/out",writable=True)]
        cmd=["-B","-c","import os,runpy,sys; os.umask(0o022); sys.argv.pop(0); runpy.run_path(sys.argv[0],run_name='__main__')",
             "/opt/edge/client/soft_projection_evolution.py"]
        arguments=["--task",args.task,"--ready-site","/ready","--approved-site","/approved","--ready-lock",locks[0],
                   "--approved-lock",locks[1],"--out","/out"]
        raw=docker(*common,*review,image,*cmd,"propose",*arguments)
        proposal=json.loads(raw)
        require(proposal==json.loads(read(out/"candidate.json")) and SHA.fullmatch(proposal["candidate_digest"])
                and NAME.fullmatch(proposal["service"]) and isinstance(proposal["arguments"],dict),
                "supervisor candidate identity mismatch")
        provider=proposal.get("provider",proposal["service"])
        require(NAME.fullmatch(provider),"invalid admitted provider ID")
        service=json.loads(read(approved/"config"/(provider+".json")))
        backend=service["backend_digest"]
        require(SHA.fullmatch(backend) and service["profile_digest"]==phash,
                "validator profile differs from preauthorized service settings")
        def qualify():
            mounts=[*mount(validator,"/gate/verify.py"), *mount(profile,"/config/perception.json")]
            for dest,vol in sorted(cfg["mounts"].items()):
                mounts.extend(mount(vol,dest,volume=True))
            params=proposal["arguments"]
            require(set(params)==set(cfg["arguments"].values()),"validator cannot cover all proposed arguments")
            flags=[]
            for flag,key in sorted(cfg["arguments"].items()):
                value=params[key]
                require(isinstance(value,str) and 0<len(value)<=1024 and "\x00" not in value,"unsupported candidate argument")
                flags.append("--"+flag+"="+value)
            limit=cfg.get("validator_timeout",30)
            try:
                report=docker(*common,"--network",cfg["facts_network"],*mounts,image,"-B","/gate/verify.py",
                              "--candidate-digest",proposal["candidate_digest"],"--backend-digest",backend,"--actor",cfg["actor"],
                              "--profile","/config/perception.json","--inputs","/inputs","--timeout",str(limit),*flags,timeout=limit+8)
            except subprocess.CalledProcessError as error:
                failed=error.stdout or b""
                if 0<len(failed)<=1024*1024:
                    persist_once(out/("qualification-rejected-"+hash_bytes(failed)[7:]+".json"),failed)
                raise ValueError("independent verification failed; projection remains unapplied") from error
            value=json.loads(report)
            actual=value.get("arguments",{key:value.get(flag.replace("-","_")) for flag,key in cfg["arguments"].items()})
            require(value.get("passed") is True and value.get("schema_version")==1 and value.get("checks")
                    and value.get("candidate_digest")==proposal["candidate_digest"] and value.get("tool_id")==proposal["tool"]
                    and value.get("backend_digest")==backend and value.get("profile_digest")==phash
                    and value.get("actor_ref")==cfg["actor"]
                    and actual==params,
                    "independent qualification belongs to a different request or release")
            return report
        path,digest=validation_checkpoint(out,events,qualify)
        if path.stat().st_uid==os.geteuid():
            if path.stat().st_gid!=gid:
                os.chown(path,-1,gid)
            path.chmod(0o640)
        print(docker(*common,*review,*mount(args.registry_volume,"/registry",volume=True,writable=True),
                     image,*cmd,"apply",*arguments,"--registry","/registry","--validation","/out/"+path.name,
                     "--validation-digest",digest,timeout=100).decode().strip())


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    for name in ("source-root","ready-site","approved-site","out"):
        cli.add_argument("--"+name,type=Path,required=True)
    cli.add_argument("--site-env",type=Path,default=os.environ.get("EDGE_EVOLUTION_VALIDATOR_CONFIG"),
                     help="trusted JSON containing private facts network, actor, validator path, profile, mounts and argument mapping")
    for name in ("task","ready-lock","approved-lock","registry-volume","image"):
        cli.add_argument("--"+name,required=True)
    cli.add_argument("--port",type=int,default=os.environ.get("EDGE_EVOLUTION_AGENT_PORT",8794))
    cli.add_argument("--timeout",type=int,default=60,help="seconds to wait for the original task to reach BLOCKED")
    args=cli.parse_args()
    if not args.site_env:
        cli.error("set --site-env or EDGE_EVOLUTION_VALIDATOR_CONFIG")
    try:
        run(args)
    except (OSError,subprocess.SubprocessError,KeyError,TypeError,ValueError) as exc:
        print("projection trigger: "+str(exc),file=sys.stderr)
        return 1
    return 0


if __name__=="__main__":
    sys.exit(main())
