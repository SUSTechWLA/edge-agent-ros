"""Estimate relative board dimensions from one photograph, square markers and a measured baseline."""
import argparse
import hashlib
import json
from pathlib import Path

import cv2
import numpy as np

from calibration.circle_target import IDS,marker_corners


def measure(photo, baseline):
    gray=cv2.imread(str(photo),cv2.IMREAD_GRAYSCALE)
    marks=marker_corners(cv2.cvtColor(gray,cv2.COLOR_GRAY2RGB))
    if set(marks)!=set(IDS):
        raise ValueError("all four unique printed markers must be visible")
    corners=np.stack([marks[i] for i in IDS])
    def center(q):
        p=np.column_stack((q,np.ones(4)))
        c=np.cross(np.cross(p[0],p[2]),np.cross(p[1],p[3]))
        return c[:2]/c[2]
    unit=np.float32([[0,0],[1,0],[1,1],[0,1]])
    h=cv2.getPerspectiveTransform(np.float32([center(m) for m in corners]),unit)
    q=cv2.perspectiveTransform(corners.reshape(1,-1,2),h).reshape(4,4,2)
    # A rectangular grid leaves one unknown axis scale. Fit it from the square printed codes.
    edges=q-np.roll(q,1,axis=1)
    horizontal=abs(edges[:,:,0])>abs(edges[:,:,1])
    marker_x=abs(edges[:,:,0][horizontal])*baseline
    marker_y=abs(edges[:,:,1][~horizontal])
    height=float(np.median(marker_x)/np.median(marker_y))
    pad=150
    h=np.array([[baseline,0,pad],[0,height,pad],[0,0,1.]])@h
    rect=cv2.warpPerspective(gray,h,(round(baseline+2*pad),round(height+2*pad)))
    centers=[]
    diameters=[]
    seeds=[]
    for x,y in ((0,0),(1,0),(1,1),(0,1)):
        # Each quarter is searched independently. The supplied board spacing and diameter are not inputs.
        x0=round(pad+x*baseline/2)
        y0=round(pad+y*height/2)
        part=rect[y0:round(y0+height/2),x0:round(x0+baseline/2)]
        circles=cv2.HoughCircles(cv2.GaussianBlur(part,(5,5),1),cv2.HOUGH_GRADIENT,1,minDist=baseline*.20,
                                 param1=80,param2=35,minRadius=int(baseline*.06),maxRadius=int(height*.23))
        if circles is None or len(circles[0])!=1:
            raise ValueError("no unique circular rim in photograph quadrant")
        cx,cy,r=circles[0,0]
        seeds.append((float(cx+x0),float(cy+y0),float(r)))
    blurred=cv2.GaussianBlur(rect,(5,5),1.)
    dy,dx=np.gradient(blurred.astype(float))
    ey,ex=np.nonzero(cv2.Canny(blurred,18,65))
    pix=np.column_stack((ex,ey))
    for x,y,r in seeds:
        offsets=pix-[x,y]
        radii=np.linalg.norm(offsets,axis=1)
        grad=np.column_stack((dx[ey,ex],dy[ey,ex]))
        cos=abs((offsets*grad).sum(axis=1))/np.maximum(1.,radii*np.linalg.norm(grad,axis=1))
        ring=pix[(abs(radii-r)<4.5)&(cos>.8)]
        take=np.ones(len(ring),dtype=bool)
        for _ in range(4):
            group=ring[take]
            if len(group)<40:
                raise ValueError("insufficient optical rim support")
            a,b,c=np.linalg.lstsq(np.column_stack((2*group,np.ones(len(group)))),
                                  (group*group).sum(axis=1),rcond=None)[0]
            radius=np.sqrt(c+a*a+b*b)
            residual=np.linalg.norm(ring-[a,b],axis=1)-radius
            take=abs(residual)<max(1.4,2.5*np.median(abs(residual[take])))
        sectors=np.arctan2(ring[take,1]-b,ring[take,0]-a)
        if len(np.unique(np.floor((sectors+np.pi)/(np.pi/12))))<18:
            raise ValueError("optical rim lacks angular support")
        centers.append(np.array([a-pad,b-pad]))
        diameters.append(float(2*radius))
    lengths=lambda pairs:[float(np.linalg.norm(centers[i]-centers[j])) for i,j in pairs]
    return dict(scope="relative geometry from one projectively rectified photo; non-certified",
                photo_sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),image_shape_px=list(gray.shape),
                scale_input_horizontal_marker_baseline_mm=baseline, estimated_vertical_marker_spacing_mm=height,
                horizontal_marker_edges_mm=marker_x.tolist(),hole_horizontal_center_spans_mm=lengths([(0,1),(3,2)]),
                hole_vertical_center_spans_mm=lengths([(0,3),(1,2)]),hole_diameters_mm=diameters)


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("photo",type=Path)
    cli.add_argument("--baseline-mm",type=float,required=True)
    args=cli.parse_args()
    print(json.dumps(measure(args.photo,args.baseline_mm),indent=2))
