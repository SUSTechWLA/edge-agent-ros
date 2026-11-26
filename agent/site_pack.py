"""Resolve a board profile against verified release artifacts and admit its soft releases as one transaction."""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

from plugin_descriptor import NAME, SEMVER, _read, canonical, load_descriptor
from soft_host import settings_for
from v2_registry import V2Registry, fingerprint


def require(condition, detail):
    if not condition:
        raise ValueError(detail)


def fields(value, required, optional=()):
    require(isinstance(value,dict) and set(required)<=value.keys()<=set(required)|set(optional), "invalid fields")


def names(value):
    require(isinstance(value,list) and all(isinstance(k,str) and NAME.fullmatch(k) for k in value)
            and len(set(value))==len(value),"expected distinct plugin or pack IDs")
    return value


def policy(value):
    fields(value,("classes","effects","site_approval"))
    require(set(names(value["classes"])) <= {"soft","physical"} and
            set(names(value["effects"])) <= {"read","idempotent","actuation"} and
            type(value["site_approval"]) is bool,"unsupported pack policy")
    return value


def tighten(base, override):
    policy(base)
    if override is None:
        return base
    policy(override)
    require(set(override["classes"])<=set(base["classes"]) and set(override["effects"])<=set(base["effects"]) and
            (override["site_approval"] or not base["site_approval"]),"overlay would broaden policy")
    return override


def save(path, value):
    path.write_bytes(canonical(value)+b"\n")


def checked_settings(value):
    require(isinstance(value,dict) and all(isinstance(k,str) and NAME.fullmatch(k) and isinstance(v,dict)
            for k,v in value.items()),"settings must be indexed by plugin ID")
    return value


def member(base, *parts):
    base=base.resolve(strict=True)
    target=base.joinpath(*parts)
    require(target.resolve(strict=True)==target and target.is_relative_to(base),"expected immutable regular site path")
    return target


def copy_trust(source,target):
    require(all(not p.is_symlink() and (p.is_dir() or p.is_file()) for p in source.rglob("*")),"trust tree contains special files")
    shutil.copytree(source,target)


def physical_descriptor(site):
    from physical_controller import load_deployment
    item=load_descriptor(member(site,"camera.plugin.json"),member(site,"trust"))
    load_deployment(site,item["provenance"]["image_digest"])
    return item,fingerprint(_read(member(site,"deployment.json")))


def build_lock(packs_dir, profile_path, site, override, output, physical=None):
    profile=_read(Path(profile_path))
    fields(profile,("schema_version","id","packs","settings","policy"))
    require(type(profile["schema_version"]) is int and profile["schema_version"]==1,"invalid profile version")
    names([profile["id"]])
    names(profile["packs"])
    settings=checked_settings(profile["settings"])
    site_policy=policy(profile["policy"])
    disabled=set()
    overrides={}
    if override is not None:
        overlay=_read(Path(override))
        fields(overlay,(),("disable","settings","policy"))
        disabled=set(names(overlay.get("disable",[])))
        overrides=checked_settings(overlay.get("settings",{}))
        site_policy=tighten(site_policy,overlay.get("policy"))
    site=Path(site).resolve(strict=True)
    root=Path(packs_dir)
    output=Path(output).absolute()
    require(not output.exists() and not output.is_relative_to(site),"output must be a new site outside the input site")
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix=".pack-") as tmp:
        result=Path(tmp)/"site"
        result.mkdir()
        trust=member(site,"trust")
        copy_trust(trust,result/"trust")
        (result/"releases").mkdir()
        (result/"config").mkdir()
        selected={}
        known=set()
        packs={}
        for pack_id in sorted(profile["packs"]):
            pack=_read(member(root,pack_id,"pack.json"))
            fields(pack,("schema_version","id","plugins","policy"))
            require(type(pack["schema_version"]) is int and pack["schema_version"]==1 and pack["id"]==pack_id,"invalid pack identity")
            pack_policy=policy(pack["policy"])
            require(isinstance(pack["plugins"],list),"pack.plugins must be an array")
            packs[pack_id]=fingerprint(pack)
            for spec in pack["plugins"]:
                fields(spec,("id","version","config"),("content_digest","class"))
                key=names([spec["id"]])[0]
                require(key not in known,"duplicate plugin in selected packs: "+key)
                known.add(key)
                require(isinstance(spec["config"],dict) and isinstance(spec["version"],str) and SEMVER.fullmatch(spec["version"]),
                        "invalid pack release")
                if key in disabled:
                    continue
                deployment_hash=None
                if spec.get("class")=="physical":
                    require(physical is not None and key=="camera.capture","supply the independently approved camera site")
                    camera_site=result/"physical"
                    camera_site.mkdir()
                    for path in ("camera.plugin.json","deployment.json"):
                        shutil.copyfile(member(Path(physical),path),camera_site/path)
                    copy_trust(member(Path(physical),"trust"),camera_site/"trust")
                    item,deployment_hash=physical_descriptor(camera_site)
                else:
                    require(spec.get("class","soft")=="soft","invalid requested class")
                    item=load_descriptor(member(site,"releases",key+".plugin.json"),result/"trust")
                    require(item["class"]=="soft","physical release requires independent deployment approval")
                require(item["id"]==key and item["version"]==spec["version"] and
                        spec.get("content_digest",item["content_digest"])==item["content_digest"],"requested release identity unavailable: "+key)
                require(all(item["class"] in p["classes"] and item["effects"] in p["effects"] for p in (site_policy,pack_policy)),
                        "policy excludes "+key)
                config={**spec["config"],**settings.get(key,{}),**overrides.get(key,{})}
                require(item["class"]=="soft" or not config,"device settings require explicit deployment handoff")
                role_extra=pack_policy["site_approval"] or site_policy["site_approval"]
                config_path = result/"config"/(key+".json")
                save(config_path,config)
                # Hash the persisted JSON. Canonical storage writes integral floats as integers.
                config = _read(config_path)
                if deployment_hash is None:
                    save(result/"releases"/(key+".plugin.json"),item)
                    settings_for(item,result)
                selected[key]={"version":item["version"],"content_digest":item["content_digest"],"class":item["class"],
                               "effects":item["effects"],"config_sha256":fingerprint(config),
                               "site_approval":role_extra or item["policy"]["site_approval"]}
                if deployment_hash is not None:
                    selected[key]["deployment_sha256"]=deployment_hash
        require((set(settings)|disabled|set(overrides))<=known,"unknown plugin in profile/overlay")
        lock={"schema_version":1,"profile":profile["id"],"packs":packs,"plugins":dict(sorted(selected.items())),"policy":site_policy}
        save(result/"pack.lock.json",lock)
        verify(result, fingerprint(lock))
        result.rename(output)
    return fingerprint(lock)


def verify(site, expected):
    site=Path(site)
    lock=_read(member(site,"pack.lock.json"))
    require(fingerprint(lock)==expected,"resolved pack digest differs from expected fingerprint")
    fields(lock,("schema_version","profile","packs","plugins","policy"))
    require(type(lock["schema_version"]) is int and lock["schema_version"]==1,"invalid lock version")
    track=policy(lock["policy"])
    actual={}
    owners={}
    for key,spec in lock["plugins"].items():
        names([key])
        fields(spec,("version","content_digest","class","effects","config_sha256","site_approval"),("deployment_sha256",))
        if spec["class"]=="physical":
            item,dig=physical_descriptor(member(site,"physical"))
            require(spec.get("deployment_sha256")==dig,"physical deployment/witness changed after resolution")
        else:
            item=load_descriptor(member(site,"releases",key+".plugin.json"),member(site,"trust"))
        require(item["id"]==key and all(item[k]==spec[k] for k in ("version","content_digest","class","effects")) and
                spec["class"] in track["classes"] and spec["effects"] in track["effects"],"invalid locked release "+key)
        config=_read(member(site,"config",key+".json"))
        require(fingerprint(config)==spec["config_sha256"],"settings changed after resolution")
        if spec["class"]=="soft":
            settings_for(item,site)
        for cap in item["provides"]:
            require(cap["name"] not in owners,"ambiguous export "+cap["name"])
            owners[cap["name"]]=(key,cap["type"],cap["schema_ref"])
        actual[key]=item
    graph={}
    for key,item in actual.items():
        graph[key]=set()
        for cap in item["requires"]:
            provider=owners.get(cap["name"])
            require(provider and provider[1:]==(cap["type"],cap["schema_ref"]),"missing or incompatible dependency")
            require(item["class"]!="soft" or actual[provider[0]]["class"]=="soft","soft release cannot acquire physical authority")
            graph[key].add(provider[0])
    while graph:
        ready={key for key,deps in graph.items() if not deps}
        require(ready,"cyclic pack dependencies")
        graph={key:deps-ready for key,deps in graph.items() if key not in ready}
    return lock,actual


def main():
    cli=argparse.ArgumentParser(description=__doc__)
    commands=cli.add_subparsers(dest="action",required=True)
    build=commands.add_parser("resolve")
    for flag in ("packs","profile","site","out"):
        build.add_argument("--"+flag,type=Path,required=True)
    build.add_argument("--override",type=Path)
    build.add_argument("--physical-site",type=Path)
    for action in ("verify","activate"):
        cmd=commands.add_parser(action)
        cmd.add_argument("--site",type=Path,required=True)
        cmd.add_argument("--expect-lock",required=True)
        if action=="activate":
            cmd.add_argument("--root",type=Path,required=True)
    args=cli.parse_args()
    try:
        if args.action=="resolve":
            value={"lock":build_lock(args.packs,args.profile,args.site,args.override,args.out,args.physical_site)}
        else:
            lock,items=verify(args.site,args.expect_lock)
            value={"lock":args.expect_lock,"profile":lock["profile"],"plugins":list(items)}
            if args.action=="activate":
                require(not any(spec["site_approval"] and spec["class"]=="soft" for spec in lock["plugins"].values()),
                        "manual site approval required; automated soft admission denied")
                configured=os.environ.get("EDGE_PLUGIN_TRUST_ROOT")
                require(not configured or Path(configured).resolve()==(args.site/"trust").resolve(),"site not authorized")
                with V2Registry(args.root) as registry:
                    value.update(registry.install_pack([i for i in items.values() if i["class"]=="soft"],args.expect_lock))
                value["requires_deployment"]=[k for k,i in items.items() if i["class"]=="physical"]
        print(json.dumps(value,ensure_ascii=False))
    except (ValueError,OSError,KeyError,TypeError) as exc:
        cli.exit(1,str(exc)+"\n")


if __name__=="__main__":
    main()
