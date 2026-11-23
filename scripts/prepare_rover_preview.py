"""Provision an immutable DSH v2 soft-process release for the rover preview image."""
import argparse
import json
from pathlib import Path
import tempfile

from prepare_soft_pilots import GIT, ROOT, obj, save, sha, verify_image
from plugin_descriptor import compute_content_digest, load_descriptor


def prepare(destination,raw_image,expected,commit):
    manifest=verify_image(raw_image,expected)
    if destination.exists() or not GIT.fullmatch(commit):
        raise ValueError("supply a new site path and the deployment source commit")
    destination.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as tmp:
        staging=Path(tmp)/"site"
        trust=staging/"trust"
        releases=staging/"releases"
        trust.mkdir(parents=True)
        releases.mkdir()
        tool=json.loads((ROOT/"perception/rover_preview/tool.json").read_bytes())
        plugin_id="planning.rover_preview"
        peer="rover-preview"
        network={"ingress":[],"egress":[peer]}
        command=["python3","-B","/opt/edge/perception/rover_preview/provider.py"]
        interface="rover_preview/call@1"
        profile={"legacy":tool,"endpoint":tool["endpoint"],"probe":"http://rover-preview:8149/health",
                 "health_expected":{"ready":True,"plugin_id":plugin_id,"plugin_version":tool["version"],"tool":tool["id"]}}
        item={"schema_version":2,"id":plugin_id,"version":tool["version"],"kind":"tool","effects":"read",
              "class":"soft","runtime":"process","entrypoint":command,"content_digest":None,
              "provenance":{"author":"edge-agent-ros","source_commit":commit,"image_digest":expected},
              "network":network,"mounts":[],"provides":[{"name":tool["id"],"type":"tool","schema_ref":interface}],
              "requires":[],"parameters":{"schema_ref":interface},
              "outcomes":obj({"status":{"type":"string"},"scan_stamp_ns":{"type":"string"},
                              "motion_authorized":{"type":"boolean","const":False}},closed=False),
              "config_schema":obj({"profile":{"type":"object","const":profile},"settings":{"type":"object"}}),
              "hot_reload":True,"drain_required":True,"device_lease":None,"stop_protocol":None,"verifier":None,
              "operation_ledger":None,"health":{"probe":"ready","ttl_ms":6000,"timeout_ms":1000},
              "resources":{"cpu":1,"memory_mib":128,"max_inflight":1,"shutdown_ms":5000},
              "policy":{"install":["release-admin"],"enable":["site-operator"],"site_approval":False}}
        (trust/"image.json").write_bytes(manifest)
        save(trust/"input.json",obj({}))
        record={"id":plugin_id,"exports":[tool["id"]],"artifact":"image.json","kind":"image","entrypoint":command,
                "class":"soft","source_commit":commit,"image_digest":expected,"network":network}
        save(trust/"catalog.json",{"schemas":{interface:"input.json"},"implementations":{expected:[record]}})
        item["content_digest"]=compute_content_digest(item,trust)
        descriptor=releases/(plugin_id+".plugin.json")
        save(descriptor,item)
        load_descriptor(descriptor,trust)
        staging.rename(destination)
    return item["content_digest"]


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--site",type=Path,required=True)
    cli.add_argument("--image-manifest",type=Path,required=True)
    cli.add_argument("--image-digest",required=True)
    cli.add_argument("--source-commit",required=True)
    args=cli.parse_args()
    print(prepare(args.site,args.image_manifest.read_bytes(),args.image_digest,args.source_commit))
