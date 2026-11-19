"""Verify the concrete board image, Compose entrypoints and effective network/volume permissions before bench admission."""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/"agent"))
from physical_controller import load_deployment
from site_pack import require, verify


def docker(*args):
    return subprocess.check_output(["docker",*args],text=True,encoding="utf8").strip()


def board_identity(image, revision):
    info=json.loads(docker("image","inspect",image))[0]
    require(info["Config"]["Labels"]["org.opencontainers.image.revision"]==revision,"board image/source mismatch")
    platform="/".join(v for v in (info["Os"],info["Architecture"],info.get("Variant")) if v)
    selected=json.loads(docker("image","inspect","--platform",platform,info["Id"]))[0]
    manifest=selected["Descriptor"]
    require("manifest.v" in manifest["mediaType"] and "list" not in manifest["mediaType"],"not an immutable platform manifest")
    return info["Id"],manifest["digest"]


def contracts(track,site,lock,platform_digest,revision):
    _,items=verify(site,lock)
    if track=="physical":
        approved=items.get("camera.capture")
        require(approved and approved["class"]=="physical","profile does not select the physical camera")
        deployment=load_deployment(site/"physical",platform_digest)["pin"]
        require(deployment["deployment_commit"]==revision and deployment["content_digest"]==approved["content_digest"],
                "physical site differs from approved release")
        return {"physical-controller":(["python3","-B","/opt/edge/agent/physical_controller.py"],"none"),
                "physical-camera":(["/usr/local/bin/edge-entrypoint","python3","-B","/opt/edge/perception/camera/physical.py"],"physical-sensors"),
                "physical-verifier":(["/usr/local/bin/edge-entrypoint","python3","-B","/opt/edge/perception/camera/physical_verifier.py"],"physical-sensors")}
    peers={"soft-evidence":"evidence","soft-session":"session"}
    result={"soft-host":(["python3","-B","/opt/edge/agent/soft_host.py"],"soft-data"),
            "soft-gateway":(["python3","-B","/opt/edge/agent/soft_gateway.py"],"soft-data"),
            "soft-facts":(["python3","-B","/opt/edge/agent/soft_gateway.py","--board-facts"],"service:console"),
            "evidence":(["python3","/opt/edge/evidence/provider.py"],"soft-data"),
            "session":(["python3","/opt/edge/sessions_plugin/provider.py"],"soft-data")}
    for item in items.values():
        provenance=item["provenance"]
        require(provenance["image_digest"]==platform_digest and provenance["source_commit"]==revision,
                "site release differs from board image: "+item["id"])
        if item["class"]=="soft" and item["runtime"]=="process":
            names=item["network"]["egress"]
            require(len(names)==1 and names[0] in peers and item["entrypoint"]==result[peers[names[0]]][0],
                    "no approved board process matches "+item["id"])
    return result


def camera_scope(config, deployment):
    scope=deployment["sensor"]
    expected={"EDGE_CAMERA_SOURCE":scope["source_id"],"EDGE_CAMERA_TOPIC":scope["topic"],"ROS_DOMAIN_ID":str(scope["ros_domain_id"])}
    for key in ("physical-camera","physical-verifier"):
        values=config["services"][key]["environment"]
        require(all(values.get(k)==v for k,v in expected.items()),"sensor/source/domain differs from approved deployment: "+key)


MOUNTS={
    "soft-host":{"/site":False,"/registry":True,"/sockets/agent":True,"/sockets/session":True,"/journal":True},
    "soft-facts":{"/facts":True},"soft-gateway":{"/facts":False},
    "evidence":{"/var/lib/edge-evidence/evidence.query":True},"session":{"/var/lib/edge-session/session.default":True},
    "physical-controller":{"/site":False,"/ledger":True,"/sockets/agent":True,"/sockets/camera":True,"/sockets/verifier":True},
    "physical-camera":{"/sockets/camera":False,"/captures":True},
    "physical-verifier":{"/sockets/verifier":False,"/captures":False,"/witness":False},
}


def check_compose(config, expected, identity, site):
    for name,(entry,net) in expected.items():
        svc=config["services"][name]
        require(svc["image"]==identity and svc["entrypoint"]==entry,"image/entrypoint: "+name)
        require((svc.get("command") or [])==(["serve"] if name=="physical-controller" else []),"unexpected command: "+name)
        require(svc.get("read_only") and not svc.get("privileged") and "ALL" in svc.get("cap_drop",[]) and
                not any(svc.get(key) for key in ("devices","device_cgroup_rules","cap_add","ports","gpus","pid","ipc")),
                "container privileges exceed bench profile: "+name)
        mounts=svc.get("volumes",[])
        require({m["target"]:not m.get("read_only",False) for m in mounts}==MOUNTS[name],"unexpected mount: "+name)
        for m in mounts:
            if m["target"]=="/site":
                require(m["type"]=="bind" and Path(m["source"]).resolve()==site.resolve(),"site does not match approved directory")
            else:
                require(m["type"]=="volume","only named data and socket volumes are allowed")
        if net=="none" or net.startswith("service:"):
            require(svc.get("network_mode")==net and not svc.get("networks"),"unexpected namespace: "+name)
        else:
            require(set(svc.get("networks",{}))=={net} and not svc.get("network_mode")
                    and config["networks"][net].get("internal") is True,"network isolation: "+name)


def inspect_running(config, expected, identity, compose):
    for name,(entry,net) in expected.items():
        cid=docker(*compose,"ps","-q",name)
        require(bool(cid),"container not running: "+name)
        actual=json.loads(docker("container","inspect",cid))[0]
        host=actual["HostConfig"]
        require(actual["State"]["Running"] and actual["Config"]["Entrypoint"]==entry and actual["Image"]==identity
                and (actual["Config"].get("Cmd") or [])==(["serve"] if name=="physical-controller" else [])
                and host["ReadonlyRootfs"] and not host["Privileged"] and not host.get("CapAdd")
                and "ALL" in host["CapDrop"] and not host.get("Devices") and not host.get("DeviceRequests")
                and not host.get("DeviceCgroupRules") and not host.get("PortBindings"),"live identity/permissions: "+name)
        effective=dict(v.split("=",1) for v in actual["Config"]["Env"] if "=" in v)
        wanted=config["services"][name].get("environment",{})
        for key in ("EDGE_CAMERA_SOURCE","EDGE_CAMERA_TOPIC","ROS_DOMAIN_ID","EDGE_PHYSICAL_PLATFORM_DIGEST",
                    "EDGE_SOFT_PLATFORM_DIGEST","EDGE_SOFT_FACT_SOCKET"):
            if key in wanted:
                require(effective.get(key)==wanted[key],"live deployment scope differs: "+name+"/"+key)
        if net=="service:console":
            require(host["NetworkMode"]=="container:"+docker(*compose,"ps","-q","console"),"unexpected Console namespace")
        elif net=="none":
            require(host["NetworkMode"]=="none","network must be disabled")
        else:
            actual_net=config["networks"][net]["name"]
            require(set(actual["NetworkSettings"]["Networks"])=={actual_net}
                    and json.loads(docker("network","inspect",actual_net))[0]["Internal"],"live network: "+name)
        approved={v["target"]:(v["type"],not v.get("read_only",False)) for v in config["services"][name].get("volumes",[])}
        mounts={m["Destination"]:(m["Type"],m["RW"]) for m in actual["Mounts"] if m["Type"]!="tmpfs"}
        require(mounts==approved,"live mount permissions: "+name)


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--track",choices=["soft","physical"],required=True)
    cli.add_argument("--image",required=True)
    cli.add_argument("--source",required=True)
    cli.add_argument("--site",type=Path,required=True)
    cli.add_argument("--lock",required=True)
    cli.add_argument("--running",action="store_true")
    cli.add_argument("-f",action="append",required=True)
    args=cli.parse_args()
    compose=["compose",*(v for f in args.f for v in ("-f",f))]
    identity,manifest=board_identity(args.image,args.source)
    expected=contracts(args.track,args.site,args.lock,manifest,args.source)
    config=json.loads(docker(*compose,"config","--format","json"))
    mounted=args.site/"physical" if args.track=="physical" else args.site
    check_compose(config,expected,identity,mounted)
    if args.track=="physical":
        camera_scope(config,load_deployment(mounted,manifest)["pin"])
    if args.running:
        inspect_running(config,expected,identity,compose)
    print(json.dumps({"image":identity,"platform_digest":manifest,"source_commit":args.source,
                      "track":args.track,"checked":sorted(expected),"running_inspected":args.running}))


if __name__=="__main__":
    main()
