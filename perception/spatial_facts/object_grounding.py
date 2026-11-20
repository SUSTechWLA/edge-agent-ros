"""Locate a visible surface of a named RGB instance in its aligned optical depth frame."""
import math
import re
import struct

from edge_contracts.spatial_facts import digest_ref, require
from perception.spatial_facts.visual_grounding import calibrated_inputs


# The requested nouns refer to labels in the deployed detector's vocabulary.
LABELS = {"绿植":"potted plant", "盆栽":"potted plant", "盆栽植物":"potted plant", "椅子":"chair",
          "办公椅":"chair", "显示器":"tv", "电视":"tv", "杯子":"cup", "水杯":"cup", "瓶子":"bottle",
          "水瓶":"bottle", "电脑鼠标":"mouse", "键盘":"keyboard", "笔记本电脑":"laptop", "背包":"backpack"}
SIDES = {"左侧":"left","左边":"left","右侧":"right","右边":"right","中央":"center","中间":"center"}


def description(query):
    require(isinstance(query, str) and 0 < len(query) <= 128, "grounding.unsupported_target")
    match = re.fullmatch(r"(?:画面|图像)?(?:中)?(?:的)?(左侧|左边|右侧|右边|中央|中间)?(?:的)?"
                         r"([\u4e00-\u9fff]+?)(?:的(?:一片)?绿色叶片|的(?:一片)?绿叶)?", query.strip())
    require(match is not None, "grounding.unsupported_target")
    side, noun = match.groups()
    if noun in ("绿色区域","绿色色块"):
        return SIDES.get(side), None
    require(noun in LABELS and (match.end(2) == len(query.strip()) or LABELS[noun] == "potted plant"),
            "grounding.unsupported_target")
    return SIDES.get(side), LABELS[noun]


def mask_runs(cells, stride, w, h):
    runs = []
    for i, j in sorted(cells, key=lambda p:(p[1],p[0])):
        if runs and runs[-1][0] == j and runs[-1][2] == i:
            runs[-1][2] += 1
        else:
            runs.append([j, i, i+1])
    return {"stride":stride,"grid_width":math.ceil(w/stride),"grid_height":math.ceil(h/stride),"runs":runs}


def in_side(u, width, side):
    if side == "left":
        return u < width / 2
    if side == "right":
        return u >= width / 2
    if side == "center":
        return width / 3 <= u < 2 * width / 3
    return True


def select_detection(prediction, label, side, image_size, binding, model_ref):
    require(isinstance(prediction, dict) and digest_ref(model_ref) and prediction.get("model_ref") == model_ref
            and prediction.get("rgb_ref") == binding["rgb_ref"], "grounding.detection_binding_mismatch")
    w, h = image_size
    rows = prediction.get("detections")
    require(isinstance(rows, list) and len(rows) <= 100, "grounding.invalid_detection")
    eligible = []
    for row in rows:
        require(isinstance(row,dict) and isinstance(row.get("label"),str), "grounding.invalid_detection")
        if row["label"] != label:
            continue
        box, score = row.get("bbox_xyxy"), row.get("score")
        require(isinstance(box,list) and len(box) == 4 and all(type(x) is int for x in box)
                and 0 <= box[0] < box[2] <= w and 0 <= box[1] < box[3] <= h
                and type(score) in (int,float) and 0 <= score <= 1, "grounding.invalid_detection")
        if score >= .45 and in_side((box[0]+box[2])/2, w, side):
            eligible.append(row)
    require(eligible, "grounding.target_absent")
    require(len(eligible) == 1, "grounding.target_ambiguous")
    return eligible[0]


def green(r, g, b):
    # Hue from olive-green to green; reject dark and low-chroma neutral surfaces.
    return g >= 33 and g >= r and g-b >= max(12, .24*g) and g-r <= 3*(g-b)


def decode_foreground(mask, stride, w, h):
    require(isinstance(mask,dict) and mask.get("stride") == stride and mask.get("grid_width") == math.ceil(w/stride)
            and mask.get("grid_height") == math.ceil(h/stride), "grounding.foreground_unavailable")
    runs = mask.get("runs")
    require(isinstance(runs,list) and 0 < len(runs) <= 4096, "grounding.foreground_unavailable")
    cells = set()
    for r in runs:
        require(isinstance(r,list) and len(r) == 3 and all(type(v) is int for v in r)
                and 0 <= r[0] < math.ceil(h/stride) and 0 <= r[1] < r[2] <= math.ceil(w/stride),
                "grounding.foreground_unavailable")
        cells.update((x,r[0]) for x in range(r[1],r[2]))
    return cells


def connected_foreground(image, detection, side, foliage):
    raw, w, h, row = image
    stride = max(1, math.ceil(w/160), math.ceil(h/120))
    x0,y0,x1,y1 = detection["bbox_xyxy"] if detection else [0,0,w,h]
    fg = None if foliage else decode_foreground(detection.get("foreground"),stride,w,h)
    cells = set()
    for j in range(y0//stride, math.ceil(y1/stride)):
        v = min(j*stride+stride//2,h-1)
        for i in range(x0//stride, math.ceil(x1/stride)):
            u = min(i*stride+stride//2,w-1)
            if not (x0 <= u < x1 and y0 <= v < y1) or (not detection and not in_side(u,w,side)):
                continue
            offset = v*row + 1+3*u
            if (green(*raw[offset:offset+3]) if foliage else (i,j) in fg):
                cells.add((i,j))
    regions = []
    while cells:
        todo = [cells.pop()]
        region = set(todo)
        while todo:
            i,j = todo.pop()
            for p in ((i+1,j),(i-1,j),(i,j+1),(i,j-1)):
                if p in cells:
                    cells.remove(p)
                    region.add(p)
                    todo.append(p)
        if len(region) >= 24:
            regions.append(region)
    require(regions,"grounding.foreground_unavailable")
    # A surface-point query accepts a representative component of that single instance.
    regions.sort(key=lambda region:(-len(region), min((j,i) for i,j in region)))
    return regions[0], stride


def surface_point(region, stride, depths, k, scale):
    raw,w,h,row = depths
    measured = {}
    for i,j in region:
        u,v = min(w-1,i*stride+stride//2),min(h-1,j*stride+stride//2)
        value = struct.unpack_from(">H",raw,v*row+1+2*u)[0]
        if 0 < value < 65535:
            measured[i,j] = value
    fraction = len(measured)/len(region)
    require(fraction >= .45, "grounding.depth_insufficient")
    candidates = {}
    for i,j in measured:
        neighbors = [(i+dx,j+dy) for dx in (-1,0,1) for dy in (-1,0,1)]
        if not all(p in region for p in neighbors):
            continue
        local = [measured[p] for p in neighbors if p in measured]
        z = measured[i,j]*scale
        if len(local) < 8 or (max(local)-min(local))*scale > max(.025,.025*z):
            continue
        candidates[i,j] = (max(local)-min(local))*scale
    require(len(candidates) >= 4,"grounding.depth_surface_unverified")
    # Prefer points well inside the color segment with deep support in the immediate
    # neighborhood. The selected depth is always a real pixel, never an average.
    ci = sum(i for i,j in region)/len(region)
    cj = sum(j for i,j in region)/len(region)
    def score(p):
        i,j = p
        support = sum((i+x,j+y) in candidates for x in range(-2,3) for y in range(-2,3))
        return (-support,(i-ci)**2+(j-cj)**2,candidates[p],j,i)
    i,j = min(candidates,key=score)
    u,v = min(w-1,i*stride+stride//2),min(h-1,j*stride+stride//2)
    z = measured[i,j]*scale
    xyz = [(u-k[2])*z/k[0],(v-k[5])*z/k[4],z]
    return {"point_pixel":[u,v],"point_depth_raw":measured[i,j],"point_camera_optical_m":xyz,
            "range_m":math.sqrt(sum(a*a for a in xyz)),"axial_depth_m":z,
            "quality":{"sampled_mask_cells":len(region),"valid_depth_fraction":fraction,
                       "supported_interior_cells":len(candidates),"local_depth_spread_m":candidates[i,j],
                       "depth_quantization_step_m":scale,"absolute_accuracy":"uncharacterized"}}


def locate_object(query, batch, artifacts, profile, *, now_ns, predict=None):
    side,label = description(query)
    image,depths,k,binding = calibrated_inputs(batch,artifacts,profile,now_ns)
    w,h = image[1:3]
    detection = identity = None
    if label:
        require(callable(predict),"grounding.detector_unavailable")
        report = predict(artifacts[binding["rgb_ref"]])
        detection = select_detection(report,label,side,(w,h),binding,profile["grounding"].get("object_model_ref"))
        identity = {key:report[key] for key in ("model_ref","rgb_ref","provider","version","device") if key in report}
        identity.update({key:detection[key] for key in ("label","score","bbox_xyxy")})
    foliage = label in (None,"potted plant")
    region,stride = connected_foreground(image,detection,side,foliage)
    point = surface_point(region,stride,depths,k,binding["depth_scale_m"])
    xs,ys = zip(*region)
    box = [min(xs)*stride,min(ys)*stride,min(w,(max(xs)+1)*stride),min(h,(max(ys)+1)*stride)]
    return {"schema_version":1,"query":query,"method":"rgb_instance_visible_surface_depth.v1",
            "target_descriptor":{"label":label,"side":side},"detection":identity,
            "selection":{"support":"detected_instance_green_pixels" if label=="potted plant" else
                         "green_pixels" if label is None else "detected_instance_rgb_grabcut",
                         "scope":"visible_surface_patch"},
            "bbox_xyxy":box,"mask":mask_runs(region,stride,w,h),**binding,**point}
