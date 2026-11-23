"""Construct a textured surface in the optical frame of one registered RGB-D exposure."""
from array import array
import json
import math
from pathlib import Path
import struct
import sys
import time
import zlib

try:
    from .scene import checked_blob, REF
except ImportError:
    from scene import checked_blob, REF


COLOR = "d435.color.image"
DEPTH = "d435.aligned.depth"
INFO = "d435.color.info"
DEPTH_SCALE_M = .001
MAX_AGE_NS = 1_500_000_000


def _read(path,limit):
    with path.open("rb") as stream:
        raw=stream.read(limit+1)
    if len(raw)>limit:
        raise ValueError("oversized RGB-D artifact")
    return raw


def _stored(root,ref,suffix,limit):
    if not isinstance(ref,str) or not REF.fullmatch(ref):
        raise ValueError("invalid RGB-D reference")
    return checked_blob(_read(root/"artifacts"/(ref[7:]+suffix),limit),ref,limit)


def _raster(png,depth):
    """Decode precisely the noninterlaced filter-0 profile emitted by the ROS readers."""
    if png[:8]!=b"\x89PNG\r\n\x1a\n":
        raise ValueError("invalid RGB-D PNG")
    chunks = {}
    offset = 8
    while offset<len(png):
        if offset+12>len(png):
            raise ValueError("incomplete PNG chunk")
        length,kind = struct.unpack_from(">I4s",png,offset)
        finish = offset+8+length
        if (kind not in (b"IHDR",b"IDAT",b"IEND") or kind in chunks or finish+4>len(png)
                or zlib.crc32(png[offset+4:finish])!=struct.unpack_from(">I",png,finish)[0]):
            raise ValueError("unsupported PNG chunks")
        chunks[kind]=png[offset+8:finish]
        offset=finish+4
    if list(chunks)!=[b"IHDR",b"IDAT",b"IEND"] or len(chunks[b"IHDR"])!=13 or chunks[b"IEND"]:
        raise ValueError("invalid PNG structure")
    width,height,bits,kind,comp,filt,interlace=struct.unpack(">IIBBBBB",chunks[b"IHDR"])
    if (not 1<=width<=1920 or not 1<=height<=1080 or
            (bits,kind,comp,filt,interlace)!=((16,0,0,0,0) if depth else (8,2,0,0,0))):
        raise ValueError("unexpected RGB-D raster profile")
    row=width*(2 if depth else 3)
    limit=(row+1)*height
    decoder=zlib.decompressobj()
    raster=decoder.decompress(chunks[b"IDAT"],limit+1)
    if not decoder.eof or decoder.unused_data or len(raster)!=limit or any(raster[::row+1]):
        raise ValueError("invalid RGB-D decompression or row filter")
    raw=b"".join(raster[k+1:k+1+row] for k in range(0,len(raster),row+1))
    if depth:
        depths=array("H")
        depths.frombytes(raw)
        if sys.byteorder=="little":
            depths.byteswap()
        return width,height,depths
    return width,height,raw


def _intrinsics(info,width,height,frame):
    k=info["k"]
    if (info["type"]!="sensor_msgs/msg/CameraInfo" or info["frame_id"]!=frame or
            info["width"]!=width or info["height"]!=height or info["distortion_model"]!="plumb_bob" or
            len(k)!=9 or not all(math.isfinite(v) for v in k) or k[0]<=0 or k[4]<=0 or
            [k[1],k[3],k[6],k[7],k[8]]!=[0,0,0,0,1] or not info.get("d") or
            any(v!=0 for v in info["d"]) or info.get("binning_x",0) not in (0,1) or
            info.get("binning_y",0) not in (0,1) or
            any(info.get("roi",{}).get(k,0) for k in ("x_offset","y_offset","width","height"))):
        raise ValueError("unsupported or unregistered RGB-D intrinsics")
    return k[0],k[4],k[2],k[5]


def build_rgbd(color_png,depth_png,info,frame,*,stride=8):
    width,height,rgb=_raster(color_png,False)
    dw,dh,depth=_raster(depth_png,True)
    if (width,height)!=(dw,dh) or type(stride) is not int or not 1<=stride<=128:
        raise ValueError("unregistered RGB-D raster sizes or invalid sampling stride")
    fx,fy,cx,cy=_intrinsics(info,width,height,frame)
    xs,ys=range(0,width,stride),range(0,height,stride)
    vertices=[]
    for y in ys:
        for x in xs:
            i=y*width+x
            z=depth[i]*DEPTH_SCALE_M
            if .15<=z<=6.:
                vertices.append([round((x-cx)*z/fx,4), round((y-cy)*z/fy,4), round(z,4),
                                 *rgb[i*3:i*3+3]])
            else:
                vertices.append(None)
    if not any(vertices):
        raise ValueError("RGB-D frame has no in-range depth samples")
    triangles=[]
    for iy,y in enumerate(ys[:-1]):
        for ix,x in enumerate(xs[:-1]):
            a=iy*len(xs)+ix
            b,c,d=a+1,a+len(xs),a+len(xs)+1
            if any(vertices[i] is None for i in (a,b,c,d)):
                continue
            # Full-resolution depth over the entire tile guards against interpolating across holes
            # or silhouettes hidden between decimated vertices.
            lo,hi=65535,0
            for py in range(y,y+stride+1):
                row=depth[py*width+x:py*width+x+stride+1]
                lo,hi=min(lo,min(row)),max(hi,max(row))
            if lo>=150 and hi<=6000 and hi-lo<=35+.025*lo:
                triangles.extend(([a,c,b],[b,c,d]))
    return {"schema_version":1,"frame_id":frame,"axes":"x_right_y_down_z_forward",
            "registration":"camera_only","image_width":width,"image_height":height,"stride_px":stride,
            "grid_width":len(xs),"grid_height":len(ys),"vertices":vertices,"triangles":triangles}


def _index(directory,source_id):
    data=json.loads(_read(directory/"index.json",512_000))
    if data["source_id"]!=source_id or not 0<len(data["frames"])<=1024:
        raise ValueError("missing or mismatched RGB-D frame index")
    frames={int(f["stamp_ns"]):f for f in data["frames"]}
    if len(frames)!=len(data["frames"]):
        raise ValueError("ambiguous RGB-D source time")
    return data["actor_ref"],frames


def latest_scene(rgb_dir=Path("/rgb"),depth_dir=Path("/depth"),info_dir=Path("/camera-info"),*,archive=None):
    """Capture a fresh, hash-bound frame pair; the two source timestamps must match exactly."""
    ca,colors=_index(rgb_dir,COLOR)
    da,depths=_index(depth_dir,DEPTH)
    common=colors.keys()&depths.keys()
    if ca!=da or not common:
        raise ValueError("no same-exposure aligned RGB-D frames")
    stamp=max(common)
    if not -50_000_000<=time.time_ns()-stamp<=MAX_AGE_NS:
        raise ValueError("latest aligned RGB-D pair is stale")
    color,depth=colors[stamp],depths[stamp]
    frame,calref=color["frame_id"],color["calibration_ref"]
    if (color["media_type"]!=depth["media_type"] or color["media_type"]!="image/png" or
            color["clock_id"]!="unix" or depth["clock_id"]!="unix" or
            frame!=depth["frame_id"] or frame!="camera_color_optical_frame" or calref!=depth["calibration_ref"]):
        raise ValueError("RGB and depth have different calibration, frame or clock")
    calibration_batch=json.loads(_read(info_dir/"observations.json",32_768))
    samples=calibration_batch["samples"]
    if (calibration_batch.get("actor_ref")!=ca or len(samples)!=1 or samples[0]["source_id"]!=INFO or
            samples[0]["artifact_ref"]!=calref or samples[0]["calibration_ref"]!=calref or
            samples[0]["clock_id"]!="unix" or samples[0]["frame_id"]!=frame):
        raise ValueError("active CameraInfo does not match registered pair")
    info_raw=_stored(info_dir,calref,".json",16_384)
    rgb_raw=_stored(rgb_dir,color["artifact_ref"],".png",1_048_576)
    depth_raw=_stored(depth_dir,depth["artifact_ref"],".png",4_194_304)
    scene=build_rgbd(rgb_raw,depth_raw,json.loads(info_raw),frame)
    if archive:
        archive(color["artifact_ref"],".png",rgb_raw)
        archive(depth["artifact_ref"],".png",depth_raw)
        archive(calref,".json",info_raw)
        scene["texture_url"]="/camera/"+color["artifact_ref"][7:]+".png"
    scene.update(stamp_ns=str(stamp),skew_ms=0,source_evidence={
        name:{"source_id":sid,"artifact_ref":sample["artifact_ref"],"frame_id":frame,"stamp_ns":str(sample["stamp_ns"])}
        for name,sid,sample in (("color",COLOR,color),("depth",DEPTH,depth),("calibration",INFO,samples[0]))})
    scene["source_evidence"].update(depth_scale_m=DEPTH_SCALE_M,
        depth_scale_ref="realsense-ros/4.58.4/fix_depth_scale",clock_id="unix",actor_ref=ca)
    return scene
