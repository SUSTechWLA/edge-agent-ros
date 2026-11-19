"""Bounded quality observations. A source timestamp and frame travel with each measurement."""
import json
import math
import struct


def indices(size, count):
    return [i*(size-1)//max(1,min(count,size)-1) for i in range(min(count,size))]


def measure_image(msg, depth=False):
    formats={"rgb8":3,"bgr8":3,"rgba8":4,"bgra8":4,"mono8":1}
    scale={"16UC1":("H",2),"32FC1":("f",4)}
    if depth and msg.encoding not in scale or not depth and msg.encoding not in formats:
        raise ValueError("unsupported_encoding")
    channels=scale[msg.encoding][1] if depth else formats[msg.encoding]
    if min(msg.width,msg.height)<1 or msg.step<msg.width*channels or len(msg.data)<msg.height*msg.step:
        raise ValueError("invalid_image_layout")
    rows=indices(msg.height,26)
    cols=indices(msg.width,32)
    data=memoryview(msg.data)
    contrast=[]
    values=[]
    for y in rows:
        last=None
        for x in cols:
            offset=y*msg.step+x*channels
            if depth:
                value=struct.unpack_from((">" if msg.is_bigendian else "<")+scale[msg.encoding][0],data,offset)[0]
            elif channels==1:
                value=data[offset]
            else:
                a,g,b=data[offset:offset+3]
                if msg.encoding.startswith("bgr"):
                    a,b=b,a
                value=(77*a+150*g+29*b)/256
            values.append(value)
            if not depth and last is not None:
                contrast.append(abs(value-last))
            last=value
    count=len(values)
    if depth:
        missing=sum(not math.isfinite(d) or d<=0 for d in values)/count
        return ("depth_holes" if missing>=.6 else "ok"), {"samples":count,"invalid_fraction":round(missing,4),"encoding":msg.encoding}
    dark=sum(v<=12 for v in values)/count
    bright=sum(v>=245 for v in values)/count
    # Include vertical changes so horizontal stripes are not classified as textureless.
    contrast.extend(abs(a-b) for a,b in zip(values,values[len(cols):]))
    edge=sum(c>=12 for c in contrast)/max(len(contrast),1)
    code="black_image" if dark>=.93 else "overexposed" if bright>=.93 else "weak_texture" if edge<.025 else "ok"
    return code,dict(samples=count,dark_fraction=round(dark,4),bright_fraction=round(bright,4),
                     texture_edge_fraction=round(edge,4),evidence_type="raw_image_proxy")


def measure_scan(msg):
    slots=indices(len(msg.ranges),720)
    if not slots or not 0<=msg.range_min<msg.range_max or not math.isfinite(msg.range_max):
        raise ValueError("invalid_scan_layout")
    values=[msg.ranges[i] for i in slots]
    hit=sum(math.isfinite(v) and msg.range_min<=v<msg.range_max for v in values)
    clear=sum(v==math.inf or v==msg.range_max for v in values)
    invalid=len(slots)-hit-clear
    code="invalid_ranges" if invalid/len(slots)>.5 else "few_returns" if hit/len(slots)<.05 else "ok"
    return code,dict(samples=len(slots),finite_returns=hit,no_return=clear,invalid=invalid,
                     range_max_m=msg.range_max,evidence_type="raw_scan")


def measure_cloud(msg):
    count=msg.width*msg.height
    if not count:
        return "empty_cloud",dict(samples=0,points=0)
    fields={f.name:f for f in msg.fields}
    if msg.point_step<1 or msg.row_step<msg.point_step*msg.width or len(msg.data)<msg.row_step*msg.height:
        raise ValueError("invalid_cloud_layout")
    if any(k not in fields or fields[k].datatype not in (7,8) or fields[k].count<1 for k in "xyz"):
        raise ValueError("invalid_cloud_fields")
    coords=[]
    for key in "xyz":
        f=fields[key]
        unpack=struct.Struct((">" if msg.is_bigendian else "<")+("f" if f.datatype==7 else "d"))
        if f.offset+unpack.size>msg.point_step:
            raise ValueError("invalid_cloud_fields")
        coords.append((f.offset,unpack))
    slots=indices(count,600)
    valid=0
    for i in slots:
        at=(i//msg.width)*msg.row_step+(i%msg.width)*msg.point_step
        coord=[decoder.unpack_from(msg.data,at+offset)[0] for offset,decoder in coords]
        valid+=all(math.isfinite(v) for v in coord) and any(v!=0 for v in coord)
    code="invalid_cloud_points" if valid/len(slots)<.1 else "few_points" if count<24 else "ok"
    return code,dict(samples=len(slots),points=count,valid_samples=valid,evidence_type="raw_cloud")


def measure_imu(msg):
    rates=msg.angular_velocity
    acc=msg.linear_acceleration
    samples=[acc.x,acc.y,acc.z,rates.x,rates.y,rates.z]
    if not all(map(math.isfinite,samples)):
        return "imu_nonfinite",{"finite_axes":sum(map(math.isfinite,samples))}
    return "ok",dict(acceleration_norm=round(math.hypot(*samples[:3]),4),angular_speed=round(math.hypot(*samples[3:]),4))


def match(msg):
    if len(msg.data)>2048:
        raise ValueError("invalid_match_report")
    report=json.loads(msg.data)
    if not isinstance(report,dict) or not isinstance(report.get("frame_id"),str):
        raise ValueError("invalid_match_report")
    stamp=report.get("stamp_ns")
    total,inliers=report.get("matches"),report.get("inliers")
    if any(type(x)!=int for x in (stamp,total,inliers)) or not 0<=inliers<=total<=10_000_000:
        raise ValueError("invalid_match_report")
    ratio=inliers/max(total,1)
    code="low_match_inliers" if inliers<6 or ratio<.25 else "ok"
    return stamp,report["frame_id"],code,dict(matches=total,inliers=inliers,inlier_ratio=round(ratio,4),evidence_type="frontend_report")
