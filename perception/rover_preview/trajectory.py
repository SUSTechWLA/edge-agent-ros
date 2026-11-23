"""Forward local path primitives in metres. These curves carry geometry, not a time law."""
from dataclasses import dataclass
import math


MAX_ARC_STEP = .075
MAX_YAW_STEP = .10
DEFAULT_CURVATURE_LIMIT = 5.0
# Maximum |d² smoothstep / du²| is attained at (3 ± sqrt(3))/6.
BENDS = ((3-math.sqrt(3))/6, (3+math.sqrt(3))/6)


@dataclass(frozen=True)
class Segment:
    x0: float
    x1: float
    y0: float
    y1: float

    def at(self, u):
        span = self.x1-self.x0
        shift = self.y1-self.y0
        s = u*u*u*(10 + u*(-15+6*u))
        slope = shift/span * derivative(u)
        second = shift/(span*span) * second_derivative(u)
        return (self.x0+span*u, self.y0+shift*s, math.atan(slope),
                second/(1+slope*slope)**1.5)

    def record(self):
        return dict(x0_m=self.x0,x1_m=self.x1,y0_m=self.y0,y1_m=self.y1)


def derivative(u):
    return 30*(u*(1-u))**2


def second_derivative(u):
    return 60*u*(1-u)*(1-2*u)


@dataclass
class Curve:
    segments: tuple
    side: str
    samples: list
    curvature_bound: float
    length: float
    sweep_padding: float

    def record(self,limit):
        return {"algorithm_id":"forward_quintic_shift.v1","continuity":"C2",
                "side":self.side,"curvature_limit_inv_m":limit,
                "max_curvature_bound_inv_m":self.curvature_bound,
                "sampled_length_m":self.length,"sweep_interpolation_bound_m":self.sweep_padding,
                "segments_body":[seg.record() for seg in self.segments],
                "samples_body":[dict(x_m=x,y_m=y,yaw_rad=a,curvature_inv_m=k)
                                for x,y,a,k in self.samples]}


def sample(segments,side,limit,radius):
    """Bound the whole interval using extrema of the polynomial derivatives.

    The centre moves at most dx*max(ds/dx). A corner moves additionally at
    most footprint_radius*dx*max(|dyaw/dx|). Half their sum bounds the motion
    from any point to its nearer endpoint in x, including simultaneous turns.
    """
    samples=[segments[0].at(0.)]
    max_curvature=0.
    padding=0.
    length=0.
    for seg in segments:
        span=seg.x1-seg.x0
        shift=abs(seg.y1-seg.y0)
        # For y(x) linear in x (the constant-lateral case) these samples
        # are still used to check every swept footprint.
        if shift==0:
            n=math.ceil(span/MAX_ARC_STEP)
            samples.extend(seg.at(i/n) for i in range(1,n+1))
            padding=max(padding,.5*span/n)
            length+=span
            continue
        scale=shift/span
        scale2=scale/span
        stack=[(0.,1.,0)]
        while stack:
            a,b,level=stack.pop()
            da,db=derivative(a),derivative(b)
            least=scale*min(da,db)
            greatest=scale*(1.875 if a<=.5<=b else max(da,db))
            bend=max(abs(second_derivative(a)),abs(second_derivative(b)))
            if any(a<=u<=b for u in BENDS):
                bend=10/math.sqrt(3)
            accel=scale2*bend
            dx=(b-a)*span
            speed=math.hypot(1.,greatest)
            angular=accel/(1+least*least)
            bound=angular/math.hypot(1.,least)
            end=seg.at(b)
            if abs(end[3])>limit:
                return None
            if dx*speed>MAX_ARC_STEP or dx*angular>MAX_YAW_STEP or bound>limit:
                if level>=14:
                    return None
                mid=.5*(a+b)
                stack.append((mid,b,level+1))
                stack.append((a,mid,level+1))
                continue
            max_curvature=max(max_curvature,bound)
            padding=max(padding,.5*dx*(speed+radius*angular))
            prev=samples[-1]
            length+=math.hypot(end[0]-prev[0],end[1]-prev[1])
            samples.append(end)
    return Curve(segments,side,samples,max_curvature,length,padding)


def direct(goal):
    return (Segment(0.,goal[0],0.,goal[1]),)


def detours(goal,front_x):
    """Candidate plateau around the closest frontal return; both sides compete."""
    x,y=goal
    peak=max(.32*x,min(.68*x,front_x))
    start=max(.32*x,peak-.08*x)
    end=min(.72*x,peak+.13*x)
    for width in (.55,.80,1.05,1.30,1.55):
        for side,sign in (("left",1.),("right",-1.)):
            apex=.5*y+sign*width
            yield side,(Segment(0,start,0.,apex),Segment(start,end,apex,apex),Segment(end,x,apex,y))
