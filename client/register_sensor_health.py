"""Publish or remove the optional read-only diagnostic tool in this deployment's registry."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import tempfile
from urllib.parse import urlsplit


def register(directory,endpoint,remove=False):
    url=urlsplit(endpoint)
    if (url.scheme not in {"http","https"} or not url.hostname or url.username or url.password
            or url.query or url.fragment or url.path!="/diagnose"):
        raise ValueError("expected a diagnostic service URL ending in /diagnose")
    tool=dict(id="diagnose_sensors",version="1",effect="read",accepts_trace=True,
              description="读取本机当前传感器质量、采集时间与诊断事件，解释异常及复核情况。",
              parameters={"scope":"string"},utterances=["检查{scope}传感器"],
              outcomes={"诊断摘要":"summary","查询范围":"scope"},endpoint=endpoint)
    root=Path(directory)
    root.mkdir(parents=True,exist_ok=True)
    dest=root/"sensor-health.diagnose_sensors.json"
    with (root/".sensor-health.lock").open("a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if dest.exists() and json.loads(dest.read_bytes())!=tool:
            raise ValueError("a different sensor diagnostic descriptor is already installed")
        if remove:
            dest.unlink(missing_ok=True)
            return "removed"
        if dest.is_file():
            return "unchanged"
        with tempfile.NamedTemporaryFile(mode="w",encoding="utf-8",dir=root,delete=False) as tmp:
            tmp_path=Path(tmp.name)
            try:
                json.dump(tool,tmp,ensure_ascii=False,indent=2)
                tmp.write("\n")
                tmp.flush()
                os.fsync(tmp.fileno())
                os.chmod(tmp.name,0o644)
                os.replace(tmp.name,dest)
            finally:
                tmp_path.unlink(missing_ok=True)
        return "installed"


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--directory",default="/plugins")
    cli.add_argument("--endpoint",default="http://sensor-health:8136/diagnose")
    cli.add_argument("--remove",action="store_true")
    args=cli.parse_args()
    print(json.dumps({"tool":"diagnose_sensors","action":register(args.directory,args.endpoint,args.remove)}))
