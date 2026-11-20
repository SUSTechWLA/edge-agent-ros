"""Independent live qualification of a read-only RGB-D target provider in the facts network."""
import argparse
import hashlib
from http.client import HTTPConnection, HTTPException
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time
import zlib


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
CHECKS = ["live_observation", "release_sha256", "source_sha256", "target_and_frame", "mask_and_bbox",
          "measured_depth_pixel", "independent_backprojection", "unsupported_target_rejected"]
UNKNOWN = "识别画面中的紫色独角兽并告诉我它的序列号"
RETRY = {"grounding.unsynchronized_input", "grounding.stale_input", "perception.sample_unavailable",
         "perception.input_unavailable", "perception.source_missing"}


def check(ok, code):
    if not ok:
        raise ValueError(code)


def sha(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def read(path, maximum):
    check(path.resolve(strict=True) == path.absolute() and path.is_file(), "verify.invalid_file")
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    check(0 < len(data) <= maximum, "verify.invalid_size")
    return data


def ns(value):
    check(type(value) is int or isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,18}", value),
          "verify.invalid_stamp")
    value = int(value)
    check(0 < value < 2**63, "verify.invalid_stamp")
    return value


def exchange(host, port, path, body, timeout):
    conn = HTTPConnection(host, port, timeout=timeout)
    try:
        conn.request("POST", path, json.dumps(body, ensure_ascii=False, allow_nan=False).encode(),
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        data = resp.read(288*1024 + 1)
        check(len(data) <= 288*1024, "verify.response_too_large")
        if resp.getheader("Content-Type", "").split(";")[0] != "application/json":
            if resp.status == 503:
                return 503, {"error": {"code": "perception.input_unavailable"}}
            raise ValueError("verify.invalid_response_type")
        return resp.status, json.loads(data)
    finally:
        conn.close()


def raster(data, bits, color):
    """Independent, size-limited PNG scanline decoder for repository RGB8 / mono16 wire images."""
    check(data[:8] == b"\x89PNG\r\n\x1a\n", "verify.invalid_png")
    pos, chunks = 8, []
    for tag in (b"IHDR", b"IDAT", b"IEND"):
        check(pos+12 <= len(data), "verify.invalid_png")
        n, name = struct.unpack_from(">I4s", data, pos)
        end = pos + 8 + n
        check(name == tag and end+4 <= len(data) and
              zlib.crc32(data[pos+4:end]) == struct.unpack_from(">I", data, end)[0], "verify.invalid_png")
        chunks.append(data[pos+8:end])
        pos = end + 4
    check(pos == len(data) and len(chunks[0]) == 13 and not chunks[2], "verify.invalid_png")
    w,h,b,c,comp,filt,interlace = struct.unpack(">IIBBBBB", chunks[0])
    check(1 <= w <= 640 and 1 <= h <= 480 and (b,c,comp,filt,interlace) == (bits,color,0,0,0),
          "verify.invalid_image_layout")
    stride = 1 + w*(3 if color == 2 else 2)
    dec = zlib.decompressobj()
    raw = dec.decompress(chunks[1], h*stride+1)
    check(dec.eof and not dec.unconsumed_tail and not dec.unused_data and len(raw) == h*stride and
          all(n == 0 for n in raw[::stride]), "verify.invalid_scanlines")
    return w, h, stride, raw


def verify_target(result, batch, profile, inputs, query, actor):
    g = profile["grounding"]
    cfg = g.get("depth_profile") or profile["sources"][g["depth_source"]]
    check(result["query"] == query and result["actor_ref"] == actor == batch["actor_ref"] == profile["actor_ref"]
          and result["clock_id"] == batch["clock_id"] == "unix" and batch["basis"] == "published"
          and result["frame_id"] == cfg["frame_id"], "verify.invalid_target_provenance")
    refs, files, times = {}, {}, []
    for label, kind, ext in (("rgb", "image", ".png"), ("depth","image",".png"), ("calibration","calibration",".json")):
        sid = g[label+"_source"]
        samples = [s for s in batch["samples"] if s["source_id"] == sid]
        check(len(samples) == 1, "verify.source_binding")
        s = samples[0]
        ref = result[label+"_ref"]
        check(s["actor_ref"] == actor and s["frame_id"] == cfg["frame_id"] and s["clock_id"] == "unix" and
              result[label+"_source_id"] == sid and s["kind"] == "artifact" and s["artifact_ref"] == ref and
              s["calibration_ref"] == result["calibration_ref"] and DIGEST.fullmatch(ref) and
              s["media_type"] == ("image/png" if kind == "image" else "application/json") and
              ns(s["stamp_ns"]) == ns(result[label+"_stamp_ns"]), "verify.source_binding")
        refs[label] = ref
        times.append(ns(s["stamp_ns"]))
        mount = profile["mounts"][sid]
        parts = mount["root"].split("/")
        check(mount["format"] == kind and 1 <= len(parts) <= 4 and
              all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", part) for part in parts), "verify.invalid_mount")
        data = read(inputs.joinpath(*parts, "artifacts", ref[7:]+ext), 4*1024*1024 if kind == "image" else 8192)
        check(sha(data) == ref, "verify.artifact_sha256")
        files[label] = data
    skew = abs(times[0]-times[1])
    check(skew <= g.get("max_skew_ns", 40_000_000) and max(times) <= ns(batch["as_of_ns"]) and
          ns(batch["as_of_ns"])-min(times) <= g.get("max_age_ns",2_000_000_000), "verify.unsynchronized")
    w,h,row,rgb = raster(files["rgb"],8,2)
    dw,dh,drow,depth = raster(files["depth"],16,0)
    cal = json.loads(files["calibration"])
    check((dw,dh) == (w,h) == (result["width"],result["height"]) == (cal["width"],cal["height"])
          and cal["frame_id"] == cfg["frame_id"] and cal["type"] == "sensor_msgs/msg/CameraInfo",
          "verify.invalid_geometry")
    k, distortion = cal["k"], cal["d"]
    check(len(k) == 9 and all(type(n) in (float,int) and math.isfinite(n) for n in k)
          and k[0]>0 and k[4]>0 and k[8]==1 and all(k[i]==0 for i in (1,3,6,7)) and all(n==0 for n in distortion)
          and cal["p"] == [k[0],0,k[2],0,0,k[4],k[5],0,0,0,1,0] and cal["r"] == [1,0,0,0,1,0,0,0,1]
          and cal["binning_x"] in (0,1) and cal["binning_y"] in (0,1)
          and cal["roi"]["x_offset"] == cal["roi"]["y_offset"] == 0
          and (cal["roi"]["width"],cal["roi"]["height"]) in ((0,0),(w,h)) and cal["roi"]["do_rectify"] is False,
          "verify.unsupported_intrinsics")
    mask, box, point = result["mask"], result["bbox_xyxy"], result["point_pixel"]
    stride = mask["stride"]
    check(type(stride) is int and 1<=stride<=32 and len(point)==2 and len(box)==4 and
          all(type(v) is int for v in (*box,*point)), "verify.invalid_mask")
    gw,gh = (w+stride-1)//stride,(h+stride-1)//stride
    x,y = point
    check((gw,gh) == (mask["grid_width"],mask["grid_height"]) and
          0<=box[0]<=x<box[2]<=w and 0<=box[1]<=y<box[3]<=h and 1<=len(mask["runs"])<=2048,
          "verify.invalid_bbox")
    cells = set()
    for run in mask["runs"]:
        check(isinstance(run,list) and len(run)==3 and all(type(v) is int for v in run), "verify.invalid_mask")
        j,a,b = run
        check(0 <= j < gh and 0 <= a < b <= gw, "verify.invalid_mask")
        for i in range(a,b):
            check((i,j) not in cells, "verify.overlapping_runs")
            cells.add((i,j))
            off = min(h-1,j*stride+stride//2)*row + 1 + 3*min(w-1,i*stride+stride//2)
            pixel = rgb[off:off+3]
            check(115*3 <= sum(pixel) <= 224*3 and max(pixel)-min(pixel)<=30, "verify.appearance_mismatch")
    check((x//stride,y//stride) in cells and len(cells) >= .12*gw*gh, "verify.target_outside_mask")
    xs,ys = zip(*cells)
    check(box == [min(xs)*stride,min(ys)*stride,min(w,(max(xs)+1)*stride),min(h,(max(ys)+1)*stride)],
          "verify.bbox_mask_mismatch")
    center = (sum(xs)/len(xs)+.5)/gw
    side = "left" if center <= .44 else "right" if center >= .56 else "center"
    words = {"left":("左侧","左边"),"right":("右侧","右边"),"center":("中央","中间")}
    check(result["target_descriptor"] == {"side":side,"color":"light_gray","shape":"planar_patch"} and
          any(s in query for s in words[side]) and "浅灰" in query and "平面" in query, "verify.target_mismatch")
    raw = struct.unpack_from(">H", depth, y*drow+1+2*x)[0]
    scale = cfg["depth_scale_m"]
    check(type(raw) is int and 0 < raw == result["point_depth_raw"] and type(scale) in (int,float) and
          0<scale<=.1 and scale==result["depth_scale_m"] and cfg["depth_scale_ref"]==result["depth_scale_ref"],
          "verify.invalid_depth_measurement")
    z = raw * scale
    xyz = [(x-k[2])*z/k[0], (y-k[5])*z/k[4], z]
    actual = result["point_camera_optical_m"]
    check(isinstance(actual,list) and len(actual)==3 and all(type(n) in (float,int) and math.isfinite(n)
          and abs(n-e)<=1e-7 for n,e in zip(actual,xyz)), "verify.backprojection_mismatch")
    return {"source_refs": refs, "rgb_depth_skew_ns": skew, "calibration_age_ns": ns(batch["as_of_ns"])-times[2],
            "bbox_xyxy": box, "selected_pixel": point,
            "raw_depth": raw, "depth_scale_m": scale, "frame_id": cfg["frame_id"], "backprojected_m": xyz,
            "mask_cell_count": len(cells), "observation_as_of_ns": str(batch["as_of_ns"])}


def qualify(args):
    raw = read(args.profile,32768)
    profile = json.loads(raw)
    pdigest = sha(raw)
    check(DIGEST.fullmatch(args.candidate_digest) and isinstance(args.backend_digest,str) and
          DIGEST.fullmatch(args.backend_digest) and profile["actor_ref"] == args.actor and
          0 < len(args.query) <= 256 and 1 <= args.timeout <= 120, "verify.invalid_settings")
    deadline = time.monotonic()+args.timeout
    attempts = 0
    def post(host, port, path, body):
        remaining = deadline-time.monotonic()
        check(remaining > 0, "verify.live_timeout")
        return exchange(host, port, path, body, min(3.,remaining))
    while time.monotonic()<deadline:
        attempts += 1
        status, batch = post("spatial-facts",8137,"/observations",{"actor_ref":args.actor})
        if status != 200:
            check(status==503,"verify.observation_failed")
        else:
            check(batch.get("actor_ref")==args.actor and batch.get("basis")=="published" and
                  0 <= time.time_ns()-ns(batch.get("as_of_ns")) <= 2_000_000_000, "verify.stale_batch")
            request = {"actor_ref":args.actor,"observations":batch,"target":args.query}
            status,out = post("stationary-perception",8140,"/ground",request)
            if status==200:
                check(out["profile_digest"]==pdigest and out["backend_digest"]==args.backend_digest, "verify.release_mismatch")
                report = verify_target(out["result"],batch,profile,args.inputs,args.query,args.actor)
                neg_status,unknown = post("stationary-perception",8140,"/ground",{**request,"target":UNKNOWN})
                check(neg_status==422 and unknown=={"error":{"code":"grounding.unsupported_target"}},
                      "verify.unsupported_target_accepted")
                return {"schema_version":1, "passed":True, "checks":CHECKS, "backend_digest":args.backend_digest,
                        "profile_digest":pdigest, "candidate_digest":args.candidate_digest, "tool_id":"mark_target",
                        "query":args.query, "actor_ref":args.actor, "attempts":attempts, "unknown_target":UNKNOWN,
                        "receipt_sha256":sha(json.dumps(out,sort_keys=True,separators=(",",":"),allow_nan=False).encode()),
                        "validated_ns":str(time.time_ns()), **report}
            code = out.get("error",{}).get("code")
            check(status in (422,503) and code in RETRY, code if isinstance(code,str) else "verify.provider_failed")
        time.sleep(min(.08,max(0,deadline-time.monotonic())))
    raise ValueError("verify.live_timeout")


def main():
    cli = argparse.ArgumentParser(description=__doc__)
    cli.add_argument("--candidate-digest", required=True)
    cli.add_argument("--query", required=True)
    cli.add_argument("--actor", required=True)
    cli.add_argument("--inputs", type=Path, default=Path("/inputs"))
    cli.add_argument("--profile", type=Path, default=Path("/config/perception.json"))
    cli.add_argument("--backend-digest", default=os.environ.get("EDGE_NAV_PERCEPTION_DIGEST"))
    cli.add_argument("--timeout", type=float, default=30, help="Total live qualification budget in seconds")
    args = cli.parse_args()
    try:
        report = qualify(args)
    except (OSError, ValueError, TypeError, KeyError, IndexError, RecursionError, HTTPException, zlib.error) as exc:
        reason = str(exc)
        code = reason if re.fullmatch(r"[a-z][a-z_.]{2,100}", reason) else "verify.invalid_or_missing_evidence"
        print(json.dumps({"schema_version":1,"tool_id":"mark_target","passed":False,"error":{"code":code}}))
        return 1
    print(json.dumps(report, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
