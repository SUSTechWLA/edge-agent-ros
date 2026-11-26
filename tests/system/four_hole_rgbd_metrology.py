"""Measure a visible four-hole plate independently of its metric target profile.

Inputs are synchronized RGB, color-aligned depth_mm, K and distortion in NPZ.
Coordinates and diameters are measured on a plane fitted to valid depth pixels.
This is a witness, not an automatic source of certified board dimensions.
"""
import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from calibration.circle_target import IDS, marker_corners


def rays(uv, k, dist):
    coords = cv2.undistortPoints(np.asarray(uv, dtype=float).reshape(-1, 1, 2), k, dist)
    return np.column_stack((coords.reshape(-1, 2), np.ones(len(coords))))


def seed_circles(gray, hull, marker_masks):
    x, y, w, h = cv2.boundingRect(hull)
    crop = cv2.GaussianBlur(gray[y:y+h, x:x+w], (5, 5), 1)
    size = min(h, w)
    detected = cv2.HoughCircles(crop, cv2.HOUGH_GRADIENT, dp=1, minDist=size*.12,
                                param1=40, param2=22, minRadius=max(5, size//28),
                                maxRadius=size//3)
    if detected is None:
        raise ValueError("no circle candidates")
    accepted = []
    for u, v, r in detected[0]:
        p = np.array([u+x, v+y], dtype=float)
        if cv2.pointPolygonTest(hull, tuple(p), True) < .6*r:
            continue
        if any(cv2.pointPolygonTest(mask, tuple(p), True) > -.55*r for mask in marker_masks):
            continue
        accepted.append((p, float(r)))
    if len(accepted) != 4:
        raise ValueError(f"expected four unambiguous image circles, got {len(accepted)}")
    return accepted


def depth_plane(depth, mask, k, dist):
    v, u = np.nonzero((mask != 0) & (depth > 0) & (depth < 65535))
    ray = rays(np.column_stack((u,v)), k, dist)
    z = depth[v,u].astype(float)
    keep = abs(z-np.median(z)) < 300.
    for _ in range(6):
        if keep.sum() < .55*len(z):
            raise ValueError("depth has insufficient dominant board plane support")
        coeff = np.linalg.lstsq(ray[keep], 1./z[keep], rcond=None)[0]
        residual = z-1./(ray@coeff)
        mad = np.median(abs(residual[keep]-np.median(residual[keep])))
        keep = abs(residual) <= max(8., 3.5*1.4826*mad)
    fractions = []
    for vx in np.array_split(np.arange(len(v)), 3):
        for ux in np.array_split(vx[np.argsort(u[vx])],3):
            fractions.append(float(keep[ux].mean()))
    valid = len(z)/int(mask.sum())
    if valid < .5 or min(fractions)<.50:
        raise ValueError("uneven/insufficient depth coverage")
    return coeff, dict(valid_fraction=valid, retained_points=int(keep.sum()),
                       minimum_sector_inlier_fraction=min(fractions),
                       median_range_mm=float(np.median(z[keep])),
                       normal_depth_rms_mm=float(np.sqrt(np.mean(residual[keep]**2))))


def circle_on_plane(points):
    origin = np.mean(points,axis=0)
    local = points-origin
    keep = np.ones(len(local),dtype=bool)
    for _ in range(5):
        p = local[keep]
        a,b,c = np.linalg.lstsq(np.column_stack((2*p, np.ones(len(p)))),
                                (p*p).sum(axis=1),rcond=None)[0]
        center = np.array([a,b])
        radius = np.sqrt(c + center@center)
        residual = np.linalg.norm(local-center,axis=1)-radius
        keep = abs(residual) < max(3.,3.5*1.4826*np.median(abs(residual[keep])))
    if keep.sum()<12 or keep.mean()<.70:
        raise ValueError(f"inconsistent optical rim: {keep.sum()}/{len(points)} radial inliers")
    angles = np.sort(np.arctan2(*(local[keep]-center).T[::-1]))
    max_gap = np.rad2deg(np.diff(np.r_[angles, angles[0]+2*np.pi]).max())
    # A semicircle or shorter arc alone does not bracket a center in both axes.
    if max_gap >= 180:
        raise ValueError(f"insufficient angular support across circle circumference: {max_gap:.1f} degrees")
    return center+origin, float(radius*2), dict(rim_samples=int(keep.sum()),max_unsupported_arc_deg=float(max_gap),
                           radial_fit_rms_mm=float(np.sqrt(np.mean(residual[keep]**2))))


def measure(path):
    with np.load(path,allow_pickle=False) as src:
        stamps = [int(src[key]) for key in ("stamp_ns","depth_ns","info_ns")]
        if len(set(stamps))!=1:
            raise ValueError("frame stamps differ")
        rgb,depth,k,d=src["rgb"],src["depth_mm"],src["k"].reshape(3,3),src["dist"]
        if depth.shape != rgb.shape[:2] or str(src["frame"]) != "camera_color_optical_frame":
            raise ValueError("unexpected color-aligned depth geometry")
    markers = marker_corners(rgb)
    if set(markers) != set(IDS):
        raise ValueError("incomplete marker set")
    masks = [np.rint(markers[i]).astype(np.int32) for i in IDS]
    hull = cv2.convexHull(np.concatenate(masks))
    gray = cv2.cvtColor(rgb,cv2.COLOR_RGB2GRAY)
    seeds = seed_circles(gray,hull,masks)
    mask = np.zeros(depth.shape,np.uint8)
    cv2.fillConvexPoly(mask,hull,1)
    for center, radius in seeds:
        cv2.circle(mask,tuple(np.rint(center).astype(int)),int(np.ceil(1.25*radius)),0,-1)
    plane, quality = depth_plane(depth,mask,k,d)
    def project(uv):
        r = rays(uv,k,d)
        return r/(r@plane)[:,None]
    quads = np.stack([project(markers[i]) for i in IDS])
    centers = quads.mean(axis=1)
    ex = (centers[1]+centers[2]-centers[0]-centers[3])
    ex /= np.linalg.norm(ex)
    ey = np.cross(plane/np.linalg.norm(plane),ex)
    axes = np.stack([ex,ey],axis=1)
    smoothed = cv2.GaussianBlur(gray,(5,5),.9)
    edge = cv2.Canny(smoothed,6,18)
    v,u=np.nonzero(edge)
    pixels = np.column_stack((u,v))
    grad = np.column_stack([cv2.Sobel(smoothed,cv2.CV_32F,dx,dy)[v,u] for dx,dy in [(1,0),(0,1)]])
    holes = []
    for center,r in seeds:
        delta = pixels-center
        dist=np.linalg.norm(delta,axis=1)
        alignment=abs((grad*delta).sum(axis=1))/np.maximum(1.,dist*np.linalg.norm(grad,axis=1))
        idx=np.flatnonzero((abs(dist-r)<.17*r)&(alignment>.8))
        angles=np.arctan2(delta[idx,1],delta[idx,0])
        bins=(np.floor((angles+np.pi)*36/np.pi).astype(int)%72)
        score=np.linalg.norm(grad[idx],axis=1)*alignment[idx]**2
        winners=idx[np.argsort(-score)]
        _,first=np.unique(bins[np.argsort(-score)],return_index=True)
        flat=project(pixels[winners[first]])@axes
        xy,dia,q=circle_on_plane(flat)
        holes.append((xy,dia,q))
    holes.sort(key=lambda item:item[0][1])
    top=sorted(holes[:2],key=lambda item:item[0][0])
    bottom=sorted(holes[2:],key=lambda item:-item[0][0])
    ordered=top+bottom
    def spacing(c, pairs):
        return [float(np.linalg.norm(c[i]-c[j])) for i,j in pairs]
    h,v=((0,1),(3,2)),((0,3),(1,2))
    hc=np.stack([row[0] for row in ordered])
    return dict(file=path.name,sha256=hashlib.sha256(path.read_bytes()).hexdigest(),stamp_ns=stamps[0],
                plane=quality,marker_side_mm=np.linalg.norm(quads-np.roll(quads,1,axis=1),axis=2).ravel().tolist(),
                marker_horizontal_mm=spacing(centers,h),marker_vertical_mm=spacing(centers,v),
                hole_horizontal_mm=spacing(hc,h),hole_vertical_mm=spacing(hc,v),
                hole_diameter_mm=[row[1] for row in ordered],rim_fit=[row[2] for row in ordered])


if __name__ == "__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("sources",nargs="+",type=Path)
    cli.add_argument("--output",required=True,type=Path)
    args=cli.parse_args()
    rows=[measure(p) for p in args.sources]
    if len(set(row["stamp_ns"] for row in rows))!=len(rows):
        raise ValueError("repeated hardware frame")
    summary={}
    for field in rows[0]:
        if field.endswith("_mm"):
            data=np.array([row[field] for row in rows])
            summary[field]=dict(median=float(np.median(data)),min=float(data.min()),max=float(data.max()),
                                frame_medians=np.median(data,axis=1).tolist())
    args.output.write_text(json.dumps(dict(method="color_aligned_depth_plane_and_measured_rgb_rims",
                  units="mm",summary=summary,frames=rows),indent=2,allow_nan=False)+"\n",encoding="utf-8")
    print(json.dumps(summary,indent=2))
