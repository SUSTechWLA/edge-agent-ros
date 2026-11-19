"""Provision the read-only site approval for the SDD-045 P2 camera deployment."""
import argparse
import json
from pathlib import Path
import tempfile

from prepare_soft_pilots import GIT, local_image, obj, save, sha, verify_image
from plugin_descriptor import compute_content_digest, load_descriptor


def prepare_site(site, manifest, commit, version="1.0.0", *, source_id="camera.front",
                 topic="/physical/camera/image", ros_domain_id=161):
    if not GIT.fullmatch(commit):
        raise ValueError("a full deployment source commit is required")
    image = sha(manifest)
    verify_image(manifest, image)
    site = Path(site)
    if site.exists():
        raise ValueError("select a new site path; deployed approval is immutable")
    site.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=site.parent) as tmp:
        stage = Path(tmp) / "site"
        trust = stage / "trust"
        trust.mkdir(parents=True)
        (trust / "image.json").write_bytes(manifest)
        params = obj({})
        save(trust / "capture.json", params)
        camera = ["python3","-B","/opt/edge/perception/camera/physical.py"]
        verifier = ["python3","-B","/opt/edge/perception/camera/physical_verifier.py"]
        item = {"schema_version":2,"id":"camera.capture","version":version,"content_digest":None,
            "class":"physical","kind":"tool","runtime":"process","effects":"read",
            "entrypoint":camera,"provenance":{"author":"edge-agent-ros","source_commit":commit,"image_digest":image},
            "parameters":params,"outcomes":obj({},closed=False),"config_schema":params,
            "provides":[{"type":"tool","name":"capture_camera_frame","schema_ref":"camera/capture@1"}],"requires":[],
            "hot_reload":False,"drain_required":True,"device_lease":"camera.rgb",
            "stop_protocol":"camera.independent_quiescence","verifier":"camera.verify","operation_ledger":"camera.operations",
            "mounts":[{"source":"camera-artifacts","target":"/captures","read_only":False}],
            "network":{"ingress":[],"egress":["camera.source"]},"resources":{"cpu":1,"memory_mib":256,"max_inflight":1,"shutdown_ms":5000},
            "health":{"probe":"camera.ready","ttl_ms":8000,"timeout_ms":1000},
            "policy":{"install":["deployment"],"enable":["deployment"],"site_approval":True}}
        record = {"id":item["id"],"exports":["capture_camera_frame"],"artifact":"image.json","kind":"image",
                  "entrypoint":camera,"class":"physical","source_commit":commit,"image_digest":image,
                  "mounts":item["mounts"],"network":item["network"]}
        save(trust / "catalog.json",{"implementations":{image:[record]},"schemas":{"camera/capture@1":"capture.json"}})
        item["content_digest"] = compute_content_digest(item, trust)
        save(stage / "camera.plugin.json",item)
        load_descriptor(stage / "camera.plugin.json",trust)
        save(stage / "deployment.json",{"resource_id":"camera.rgb","provider_id":"camera.adapter",
            "descriptor_digest":item["content_digest"],"deployment_commit":commit,"image_digest":image,
            "image_config_digest":json.loads(manifest)["config"]["digest"],
            "sensor":{"source_id":source_id,"topic":topic,"ros_domain_id":ros_domain_id},
            "verifier":{"id":"camera.verify","image_digest":image,"entrypoint":verifier},
            "lease_ms":15000,"min_quiet_ns":400000000})
        stage.rename(site)
    return item["content_digest"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site",type=Path,required=True)
    parser.add_argument("--source-commit",required=True)
    parser.add_argument("--version",default="1.0.0")
    parser.add_argument("--topic",default="/physical/camera/image")
    parser.add_argument("--source-id",default="camera.front")
    parser.add_argument("--ros-domain-id",type=int,default=161)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--image")
    source.add_argument("--image-manifest",type=Path)
    parser.add_argument("--image-digest")
    args = parser.parse_args()
    if bool(args.image_manifest) != bool(args.image_digest):
        parser.error("pass platform manifest and its expected digest together")
    manifest = verify_image(args.image_manifest.read_bytes(),args.image_digest) if args.image_manifest else local_image(args.image)
    print(json.dumps({"content_digest":prepare_site(args.site,manifest,args.source_commit,args.version,
        source_id=args.source_id,topic=args.topic,ros_domain_id=args.ros_domain_id)}))


if __name__ == "__main__":
    main()
