"""Local deployment trust for the rover adapter. Bench commissioning is opt-in and separate from task admission."""
import hashlib
import json
import os
from pathlib import Path
import re

from physical_ledger import ref, require
from plugin_descriptor import SHA


def sha(raw):
    return "sha256:"+hashlib.sha256(raw).hexdigest()


def load_profile(site,profile,platform_digest,domain):
    require(isinstance(profile,str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json",profile),
            "plugin.config_invalid","expected a local site profile filename")
    raw=(site/profile).read_bytes()
    cfg=json.loads(raw)
    profile_digest=sha(raw)
    bench_only=os.getenv("EDGE_ROVER_REQUIRE_BENCH","0")
    require(bench_only in ("0","1"),"plugin.config_invalid","invalid deployment role")
    if cfg.get("deployment")=="isolated_gazebo":
        ros=cfg.get("ros",{})
        require(bench_only=="0" and profile=="sim.json" and not os.getenv("EDGE_ROVER_BENCH_APPROVAL")
                and cfg.get("resource_id")=="sim.lingao.base" and cfg.get("profile_id")=="lingao.archive.planar.v1"
                and cfg.get("report_calibration")=="lingao_sim_planar_laser_v1" and not ros.get("stop_witness")
                and ros.get("witness")=="/ground_truth" and ros.get("witness_frame")=="world",
                "plugin.permission_denied","simulation site must use the archived model and simulated scan calibration")
        return cfg,profile_digest,None
    resource=cfg.get("resource_id","")
    ros=cfg.get("ros",{})
    require(cfg.get("deployment")=="commissioned_rover_bench" and profile!="sim.json"
            and isinstance(resource,str) and re.fullmatch(r"hardware\.[a-z0-9][a-z0-9_.-]{1,69}",resource)
            and isinstance(cfg.get("profile_id"),str) and ref(cfg["profile_id"]) and "archive" not in cfg["profile_id"].lower()
            and isinstance(cfg.get("report_calibration"),str)
            and not re.search(r"(?:^|[_.-])sim(?:ulation)?(?:$|[_.-])",cfg["report_calibration"],re.I)
            and ref(ros.get("stop_frame")) and ros.get("witness") not in (None,"/ground_truth")
            and ros.get("stop_witness") and ros["stop_witness"] not in ("/cmd_vel","/ground_truth"),
            "plugin.permission_denied","bench requires a commissioned body, calibration and independent pose and braking observations")
    path=os.environ.get("EDGE_ROVER_BENCH_APPROVAL")
    expected=os.environ.get("EDGE_ROVER_BENCH_APPROVAL_SHA256","")
    require(path and Path(path).is_absolute() and SHA.fullmatch(expected),
            "plugin.permission_denied","bench motion is disabled without operator commissioning")
    approval=Path(path).resolve()
    require(approval.is_absolute() and not approval.is_relative_to(site.resolve()),"plugin.permission_denied",
            "commissioning trust must be mounted independently of the proposed site")
    review_bytes=approval.read_bytes()
    require(sha(review_bytes)==expected,"plugin.digest_mismatch","commissioning digest differs from deployment approval")
    review=json.loads(review_bytes)
    scope={"resource_id":resource,"ros_domain_id":domain,"platform_digest":platform_digest,"profile_digest":profile_digest}
    require(review.get("schema_version")==1 and review.get("decision")=="approved_for_bench_motion"
            and ref(review.get("reviewer")) and review.get("scope")==scope,
            "plugin.permission_denied","approval does not cover this body, ROS domain, manifest and configuration")
    artifacts=review.get("evidence",{})
    require(isinstance(artifacts,dict) and set(artifacts)=={"as_built_footprint","sensor_calibration","independent_pose","braking"},
            "plugin.permission_denied","commissioning requires body, calibration, independent pose and brake records")
    for kind,record in artifacts.items():
        require(isinstance(record,dict) and isinstance(record.get("file"),str) and SHA.fullmatch(record.get("sha256",""))
                and re.fullmatch(r"[a-zA-Z0-9_-]+\.json",record["file"]),"plugin.config_invalid","invalid commissioning record")
        path=(approval.parent/record["file"]).resolve()
        require(path.parent==approval.parent,"plugin.permission_denied","invalid commissioning record path")
        evidence=path.read_bytes()
        require(sha(evidence)==record["sha256"],"plugin.digest_mismatch",f"{kind} evidence digest mismatch")
        observation=json.loads(evidence)
        source={"as_built_footprint":cfg["profile_id"],"sensor_calibration":cfg["report_calibration"],
                "independent_pose":ros["witness"],"braking":ros["stop_witness"]}[kind]
        require(observation.get("kind")==kind and observation.get("resource_id")==resource
                and observation.get("reference")==source and observation.get("passed") is True,
                "plugin.permission_denied",f"{kind} record does not attest the approved source on this body")
    return cfg,profile_digest,expected
