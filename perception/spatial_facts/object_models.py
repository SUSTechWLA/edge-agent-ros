"""Offline CPU adapter for a site-supplied YOLO detector and RGB foreground extraction."""
import hashlib
import math
from pathlib import Path

from edge_contracts.spatial_facts import SpatialFactError, digest_ref, require
from perception.spatial_facts.object_grounding import mask_runs


def foreground(bgr, box, cv2, np):
    height,width = bgr.shape[:2]
    stride = max(1,math.ceil(width/160),math.ceil(height/120))
    gw,gh = math.ceil(width/stride),math.ceil(height/stride)
    # Use the same source pixel centers as the depth consumer, including partial cells.
    xs = np.minimum(np.arange(gw)*stride+stride//2,width-1)
    ys = np.minimum(np.arange(gh)*stride+stride//2,height-1)
    small = bgr[ys[:,None],xs]
    x0,y0 = max(0,box[0]//stride),max(0,box[1]//stride)
    x1,y1 = min(gw,math.ceil(box[2]/stride)),min(gh,math.ceil(box[3]/stride))
    if x1-x0 < 5 or y1-y0 < 5 or (x1-x0)*(y1-y0) >= .94*gw*gh:
        return None
    mask = np.zeros((gh,gw),np.uint8)
    model = lambda: np.zeros((1,65),np.float64)
    try:
        cv2.grabCut(small,mask,(x0,y0,x1-x0,y1-y0),model(),model(),2,cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    iy,ix = np.where((mask==cv2.GC_PR_FGD)|(mask==cv2.GC_FGD))
    if len(ix) < 24 or len(ix) >= .93*(x1-x0)*(y1-y0):
        return None
    return mask_runs(zip(ix.tolist(),iy.tolist()),stride,width,height)


def load_yolo(weights, expected_digest, *, device="cpu"):
    path = Path(weights).resolve(strict=True)
    require(digest_ref(expected_digest) and path.is_file() and 0 < path.stat().st_size <= 256*1024*1024,
            "grounding.model_binding_mismatch")
    with path.open("rb") as stream:
        hasher=hashlib.sha256()
        for block in iter(lambda:stream.read(1024*1024),b""):
            hasher.update(block)
    require(expected_digest == "sha256:"+hasher.hexdigest(),"grounding.model_binding_mismatch")
    require(device == "cpu","grounding.model_device_unsupported")
    import cv2
    import numpy as np
    import torch
    import ultralytics
    torch.set_num_threads(1)
    cv2.setNumThreads(1)
    model = ultralytics.YOLO(str(path), task="detect")

    def predict(png):
        require(isinstance(png,bytes) and len(png)<=1024*1024 and png.startswith(b"\x89PNG\r\n\x1a\n"),
                "grounding.invalid_rgb_png")
        try:
            bgr = cv2.imdecode(np.frombuffer(png,np.uint8),cv2.IMREAD_COLOR)
            require(bgr is not None and 0 < bgr.shape[0] <= 480 and 0 < bgr.shape[1] <= 640,
                    "grounding.invalid_rgb_png")
            result, = model.predict(source=bgr,device=device,verbose=False,conf=.45,imgsz=640,max_det=24)
            rows=[]
            h,w = bgr.shape[:2]
            for box,score,cls in zip(result.boxes.xyxy.tolist(),result.boxes.conf.tolist(),result.boxes.cls.tolist()):
                label=result.names[int(cls)]
                x0,y0,x1,y1 = box
                coords = [max(0,math.floor(x0)),max(0,math.floor(y0)),min(w,math.ceil(x1)),min(h,math.ceil(y1))]
                if coords[0]>=coords[2] or coords[1]>=coords[3]:
                    continue
                row={"label":label,"score":float(score),"bbox_xyxy":coords}
                if label!="potted plant":
                    segment=foreground(bgr,coords,cv2,np)
                    if segment:
                        row["foreground"]=segment
                rows.append(row)
        except (RuntimeError,cv2.error) as error:
            raise SpatialFactError("grounding.detector_unavailable") from error
        return {"provider":"ultralytics.YOLO","version":ultralytics.__version__,"device":device,
                "model_ref":expected_digest,"rgb_ref":"sha256:"+hashlib.sha256(png).hexdigest(),"detections":rows}
    return predict
