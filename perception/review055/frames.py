"""Open the read-only, content-addressed pair selected by the recording owner."""
import hashlib
import json
import math
import re
from pathlib import Path

from edge_contracts.spatial_facts import digest_ref, identifier, require
from perception.spatial_facts.frame_query import channel_samples, reduce_samples
from perception.spatial_facts.stationary_fusion import depth_raster, stamp
from perception.spatial_facts.visual_grounding import rgb_raster


DIMENSIONS = ("width", "height")
IDENTITY = ("actor_ref", "frame_id", "clock_id", "rgb_source_id", "depth_source_id")
DIGESTS = ("rgb_ref", "depth_ref", "calibration_ref")
TIMES = ("rgb_stamp_ns", "depth_stamp_ns")

def bytes_for(path, expected, limit):
    require(digest_ref(expected), "review.invalid_digest")
    require(not path.is_symlink(), "review.invalid_frame_path")
    with path.open("rb") as stream:
        data=stream.read(limit+1)
    require(0<len(data)<=limit and hashlib.sha256(data).hexdigest()==expected[7:],"review.artifact_mismatch")
    return data

def frozen_frame(directory, ref):
    directory=Path(directory)
    raw=bytes_for(directory/"frame.json",ref,32*1024)
    record=json.loads(raw)
    require(isinstance(record,dict) and type(record.get("schema_version")) is int and record["schema_version"]==1,
            "review.invalid_frame_manifest")
    require(all(identifier(record.get(k)) for k in IDENTITY) and all(digest_ref(record.get(k)) for k in DIGESTS)
            and record["rgb_source_id"]!=record["depth_source_id"] and record["rgb_ref"]!=record["depth_ref"],
            "review.invalid_source_identity")
    require(all(type(record.get(k)) is int for k in DIMENSIONS) and 8<=record["width"]<=640 and
            8<=record["height"]<=480, "review.invalid_dimensions")
    scale=record.get("depth_scale_m")
    require(type(scale) in (float,int) and math.isfinite(scale) and 0<scale<=.1,"review.invalid_scale")
    times=[stamp(record.get(k)) for k in TIMES]
    require(abs(times[0]-times[1])<=50_000_000,"review.unsynchronized")
    colors=rgb_raster(bytes_for(directory/(record["rgb_ref"][7:]+".png"),record["rgb_ref"],1024*1024))
    depth=depth_raster(bytes_for(directory/(record["depth_ref"][7:]+".png"),record["depth_ref"],4*1024*1024))
    require(colors[1:3]==depth[1:3]==(record["width"],record["height"]),"review.geometry_mismatch")
    fields=(*IDENTITY,*DIGESTS,*DIMENSIONS,"depth_scale_m")
    info={k:record[k] for k in fields}
    info.update(frame_ref=ref,basis="sealed_rgbd_replay",rgb_stamp_ns=str(times[0]),depth_stamp_ns=str(times[1]))
    return info,depth

def measure(info, raster, columns, statistic):
    require(type(columns) is int and 1<=columns<=8 and statistic in ("p10","median"),"review.bad_query")
    width,height=raster[1:3]
    regions=[]
    for i in range(columns):
        bounds=[i*width//columns,0,(i+1)*width//columns,height]
        pixels=(bounds[2]-bounds[0])*height
        samples=list(channel_samples(raster,bounds,"depth"))
        value=reduce_samples(samples,"quantile",.1 if statistic=="p10" else .5,pixels,info["depth_scale_m"])
        regions.append({"bounds_xyxy":bounds,"pixels":pixels,"valid_pixels":len(samples),"value":value})
    return {**info,"grid":[1,columns],"statistic":statistic,"unit":"m","channel":"depth","regions":regions}
