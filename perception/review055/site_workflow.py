"""Station workflow example. The deployment supplies this single callable and its source digest.

review(frame, depth_raster, columns, distance_m) receives a validated, immutable pair.
A real user can supply an existing workflow behind this boundary. This reference workflow
counts a depth histogram per column and computes an interpolated quantile from the counts.
"""
from collections import Counter
import math
import struct

def lower_decile(histogram, count):
    if not count:
        return None
    lo,remainder=divmod(count-1,10)
    hi=lo+bool(remainder)
    seen=0
    low=None
    for depth,freq in sorted(histogram.items()):
        seen+=freq
        if low is None and seen>lo:
            low=depth
        if seen>hi:
            return (low*(10-remainder)+depth*remainder)/10
    raise ValueError("workflow.invalid_histogram")

def review(frame, depth_raster, columns, distance_m):
    data,width,height,stride=depth_raster
    measurements=[]
    observations=[]
    candidates=[]
    unknown=[]
    for i in range(columns):
        left,right=width*i//columns,width*(i+1)//columns
        histogram=Counter()
        for y in range(height):
            offset=y*stride+1
            cells=struct.iter_unpack(">H",data[offset+2*left:offset+2*right])
            histogram.update(v for (v,) in cells if 0<v<65535)
        valid=sum(histogram.values())
        total=(right-left)*height
        native=lower_decile(histogram,valid)
        depth=None if native is None else native*frame["depth_scale_m"]
        coverage=valid/total
        if coverage<.6 or depth is None:
            state="unknown"
            unknown.append(i+1)
        elif depth<=distance_m or math.isclose(depth,distance_m,abs_tol=1e-12,rel_tol=0):
            state="near_surface"
            candidates.append((depth,i+1))
        else:
            state="above_threshold"
        measurements.append({"bounds_xyxy":[left,0,right,height],"pixels":total,"valid_pixels":valid,"value":depth})
        observations.append({"column":i+1,"depth_m":depth,"pixels":total,"valid_pixels":valid,
                             "valid_fraction":coverage,"state":state})
    analysis={**frame,"grid":[1,columns],"regions":measurements,"statistic":"p10","channel":"depth","unit":"m"}
    summary={"regions":observations,"unknown_columns":unknown,"column_count":columns,"distance_m":distance_m,
             "order":"nearest_first","review_order":[idx for _,idx in sorted(candidates)],
             "scope":"camera_optical_depth_only","statistic":"p10"}
    return {"analysis":analysis,"review":summary}
