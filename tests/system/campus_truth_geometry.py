"""Measured Gazebo footprint geometry and pose conversion for campus navigation witnesses."""
import math

FOOTPRINT = (0.155, 0.275, 0.28, 0.28)


def rectangle(x, y, yaw, front, rear, left, right):
    sine, cosine = math.sin(yaw), math.cos(yaw)
    points = ((front, left), (-rear, left), (-rear, -right), (front, -right))
    return [(x + cosine*a - sine*b, y + sine*a + cosine*b) for a, b in points]


def segments(polygon):
    return list(zip(polygon, polygon[1:] + polygon[:1]))


def clearance(left, right):
    """Minimum edge distance for separated boxes, zero for touching or overlapping boxes."""
    def project(polygon, nx, ny):
        coordinates = [x*nx+y*ny for x,y in polygon]
        return min(coordinates), max(coordinates)

    def separated(edge):
        (x,y),(a,b) = edge
        nx,ny = y-b,a-x
        l0,l1 = project(left,nx,ny)
        r0,r1 = project(right,nx,ny)
        return l0 > r1 or r0 > l1

    def point_distance(point, edge):
        (x,y),(a,b) = edge
        dx,dy = a-x,b-y
        along = ((point[0]-x)*dx + (point[1]-y)*dy) / (dx*dx+dy*dy)
        along = max(0.,min(along,1.))
        return math.hypot(point[0]-x-along*dx,point[1]-y-along*dy)

    le,re = segments(left),segments(right)
    if not any(map(separated,le+re)):
        return 0.
    return min(min(point_distance(p,e) for p in left for e in re),
               min(point_distance(p,e) for p in right for e in le))


def stamp_ns(stamp):
    return stamp.sec*10**9 + stamp.nanosec


def odometry(message):
    p,q = message.pose.pose.position,message.pose.pose.orientation
    v = message.twist.twist
    return {"xy": [p.x,p.y], "yaw": math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z)),
            "linear_mps": math.hypot(v.linear.x,v.linear.y), "angular_radps": v.angular.z,
            "stamp_ns": stamp_ns(message.header.stamp), "frame": message.header.frame_id}
