"""Bounded scan-to-scan translation update in the previous base frame."""
import math


class Scan:
    def __init__(self,ranges,angle_min,step,rmin,rmax,laser_x):
        self.start=angle_min
        self.step=step
        self.laser_x=laser_x
        self.points=[]
        self.segments=[]
        if (len(ranges)>4096 or not all(map(math.isfinite,(angle_min,step,rmin,rmax,laser_x))) or
                step<=0 or not 0<=rmin<rmax):
            return
        for i,r in enumerate(ranges):
            if not math.isfinite(r) or not rmin<=r<=rmax:
                self.points.append(None)
            else:
                a=angle_min+i*step
                self.points.append((laser_x+r*math.cos(a),r*math.sin(a)))
        for a,b in zip(self.points,self.points[1:]+self.points[:1]):
            segment=None
            if a is not None and b is not None:
                dx,dy=b[0]-a[0],b[1]-a[1]
                length=math.hypot(dx,dy)
                if .002<length<.24:
                    segment=(a[0],a[1],dx/length,dy/length,length)
            self.segments.append(segment)


def estimate(previous,current,guess,turn,dt):
    """Point-to-line least squares with gyro yaw fixed and bounded correction.

    Poorly constrained directions retain the wheel prediction. The angular
    correspondence search requires a full scan.
    """
    size=len(previous.points)
    if (not all(map(math.isfinite,(*guess,turn,dt))) or not .05<=dt<=1. or size!=len(current.points) or size<90 or
            abs(size*previous.step-2*math.pi)>.02 or abs(current.step-previous.step)>1e-6 or
            abs(turn)>1.3*dt or math.hypot(*guess)>.36*dt):
        return guess,"invalid_interval"
    if abs(turn)<.004 and math.hypot(*guess)<.001:
        return guess,"stationary"
    x,y=guess
    co,si=math.cos(turn),math.sin(turn)
    for _ in range(3):
        xx=yy=xy=bx=by=0.
        count=0
        for p in current.points[::2]:
            if p is None:
                continue
            qx,qy=co*p[0]-si*p[1]+x,si*p[0]+co*p[1]+y
            ang=math.atan2(qy,qx-previous.laser_x)
            idx=round((ang-previous.start)/previous.step)
            best=.08**2
            pair=None
            for i in range(idx-3,idx+4):
                seg=previous.segments[i%size]
                if seg is None:
                    continue
                ax,ay,ux,uy,length=seg
                dx,dy=qx-ax,qy-ay
                across=uy*dx-ux*dy
                along=ux*dx+uy*dy
                end=max(0.,-along,along-length)
                d2=across*across+end*end
                if d2<best and end<.02:
                    best=d2
                    pair=(uy,-ux,across)
            if pair is None:
                continue
            nx,ny,res=pair
            weight=min(1.,.02/max(1e-6,abs(res)))
            xx+=weight*nx*nx; yy+=weight*ny*ny; xy+=weight*nx*ny
            bx-=weight*nx*res; by-=weight*ny*res
            count+=1
        trace=xx+yy
        det=xx*yy-xy*xy
        # The smaller eigenvalue must constrain the second translation axis.
        eig_min=(trace-math.hypot(xx-yy,2*xy))/2
        if count<15 or trace<10.:
            return guess,"degenerate"
        partial=eig_min<3. or eig_min<.04*trace
        if partial:
            # A single wall constrains only its normal; keep encoder prediction
            # along the tangent to avoid creating a fictitious displacement.
            angle=.5*math.atan2(2*xy,xx-yy)
            nx,ny=math.cos(angle),math.sin(angle)
            along=(bx*nx+by*ny)/(trace-eig_min)
            dx,dy=along*nx,along*ny
        else:
            dx=(yy*bx-xy*by)/det; dy=(xx*by-xy*bx)/det
        x+=dx; y+=dy
        if math.hypot(dx,dy)<.0002:
            break
    if math.hypot(x-guess[0],y-guess[1])>.24*dt or math.hypot(x,y)>.38*dt+.01:
        return guess,"innovation_rejected"
    return (x,y),"one_axis" if partial else "matched"


def rotate(point,angle):
    x,y=point
    c,s=math.cos(angle),math.sin(angle)
    return c*x-s*y,s*x+c*y


class ScanCorrection:
    def __init__(self):
        self.anchor=None
        self.dx=self.dy=0.

    def update(self,t,scan,pose):
        """Estimate a local translation; input pose contains only wheel+gyro."""
        reason="initial"
        replace=self.anchor is None
        if self.anchor is not None:
            previous,old,t0,old_x,old_y=self.anchor
            dt=(t-t0)*1e-9
            if dt<=0:
                return "out_of_order"
            turn=math.remainder(pose[2]-old[2],2*math.pi)
            guess=rotate((pose[0]-old[0],pose[1]-old[1]),-old[2])
            change,reason=estimate(previous,scan,guess,turn,dt)
            if reason in ("matched","one_axis","stationary"):
                x,y=rotate((change[0]-guess[0],change[1]-guess[1]),old[2])
                self.dx,self.dy=old_x+x,old_y+y
            # Keep the last correction on loss of observability.
            replace=dt>.45 or abs(turn)>.24
        if replace:
            self.anchor=scan,pose,t,self.dx,self.dy
        return reason
