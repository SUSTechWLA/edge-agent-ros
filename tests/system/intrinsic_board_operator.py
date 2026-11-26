"""Gazebo-only operator for one genuine workbench request to adjust the board."""
import argparse
from itertools import product
import json
import math
import os

import numpy as np


WIDTH, HEIGHT = 640, 480
FOCAL = WIDTH/(2*math.tan(1.211/2))
PATTERN = np.array([(0,y+dy,z+dz) for y,z in product((-.55,.55),(-.35,.35))
                    for dy,dz in product((-.10,.10),repeat=2)])
HOLES = np.array([(0,y,z) for y,z in product((-.25,.25),(-.20,.20))])


def rotation(roll,pitch,yaw):
    cr,cp,cy = np.cos([roll,pitch,yaw])
    sr,sp,sy = np.sin([roll,pitch,yaw])
    return np.array([[cy*cp,cy*sp*sr-sy*cr,cy*sp*cr+sy*sr],
                     [sy*cp,sy*sp*sr+cy*cr,sy*sp*cr-cy*sr],
                     [-sp,cp*sr,cp*cr]])


def candidates(aim, missing, previous):
    if aim=="common" and not previous:
        return [(3.1,0.,.50,0.,0.,0.)]
    if aim=="bottom":
        x_values = (1.31,1.43,1.55,1.70,1.85)
        rolls,pitches = (-.10,0,.10),(.22,.38,.52)
        y_values = (-.5,-.40,-.30,-.15,0,.15,.30,.40,.5)
        yaws = (-.20,0,.20)
    else:
        x_values = (2.18,2.45,2.70,2.95,3.16,3.39,3.63,3.86)
        rolls = (-.37,-.26,-.19) if aim=="roll_left" else (.19,.26,.37) if aim=="roll_right" else (0.,.10,-.10,.19,-.19)
        pitches = (-.45,-.34,-.24) if aim=="pitch_down" else (.24,.34,.45) if aim=="pitch_up" else (-.30,-.19,0.,.20,.33)
        y_values = (-.72,-.47,-.23,0.,.23,.47,.72)
        yaws = (-.30,-.19,0.,.19,.30)
    result = []
    for x,y,roll,pitch,yaw in product(x_values,y_values,rolls,pitches,yaws):
        r = rotation(roll,pitch,yaw)
        z = .7*abs(r[2,1])+.5*abs(r[2,2])+.009
        if aim!="bottom":
            z = max(.50,z)
        pose = (x,y,z,roll,pitch,yaw)
        world = PATTERN@r.T+pose[:3]
        uv = np.column_stack((WIDTH/2-FOCAL*world[:,1]/(world[:,0]-.17),
                              HEIGHT/2-FOCAL*(world[:,2]-.40)/(world[:,0]-.17)))
        margin = np.min(np.minimum(uv,[WIDTH,HEIGHT]-uv))
        if margin < 12:
            continue
        cells = {f"{('top','middle','bottom')[int(v*3/HEIGHT)]}_{('left','center','right')[int(u*3/WIDTH)]}" for u,v in uv}
        if aim=="bottom" and not (cells & missing & {"bottom_left","bottom_center","bottom_right"}):
            continue
        holes = HOLES@r.T + pose[:3] - [.215,0.,.20]
        # Front scanner is inclined down. Avoid pushing hole rims above the top ray.
        angles = np.arctan2(holes[:,2],np.hypot(holes[:,0],holes[:,1])) + .70
        paired = bool(angles.max()+.12/np.linalg.norm(holes,axis=1).min() < .895)
        if aim in ("common","holdout") and not paired:
            continue
        if any(math.dist(old[:3],pose[:3])<.14 and math.dist(old[3:],pose[3:])<.20 for old in previous):
            continue
        novelty = min((math.hypot(math.dist(old[:3],pose[:3]),math.dist(old[3:],pose[3:]))
                       for old in previous),default=1.)
        # Keep plane obliquity and viewing directions varied, while keeping tilted views compact enough.
        score = (5*len(cells&missing) + 2*paired + 3*min(.8,novelty)
                 + min(margin,65)/110 - .22*abs(x-3.05))
        if aim=="bottom":
            score += .035*np.mean(np.sort(uv[:,1])[-4:])
        result.append((score,pose))
    return [pose for _,pose in sorted(result,reverse=True)]


def set_board(pose):
    if (os.getenv("ROS_DOMAIN_ID"),os.getenv("GZ_PARTITION"))!=("197","edge_fixed_rover_197"):
        raise RuntimeError("expected isolated Gazebo calibration environment")
    from gz.transport13 import Node
    from gz.msgs10.pose_pb2 import Pose
    from gz.msgs10.boolean_pb2 import Boolean
    x,y,z,roll,pitch,yaw=pose
    cr,cp,cy=np.cos([roll/2,pitch/2,yaw/2])
    sr,sp,sy=np.sin([roll/2,pitch/2,yaw/2])
    request=Pose(name="calibration_board")
    request.position.x,request.position.y,request.position.z = x,y,z
    request.orientation.x=sr*cp*cy-cr*sp*sy
    request.orientation.y=cr*sp*cy+sr*cp*sy
    request.orientation.z=cr*cp*sy-sr*sp*cy
    request.orientation.w=cr*cp*cy+sr*sp*sy
    ok,reply=Node().request("/world/fixed_calibration/set_pose",request,Pose,Boolean,5000)
    if not ok or not reply.data:
        raise RuntimeError("Gazebo board placement was not confirmed")


def tilt_at_placement(aim, robot, fixed_xy, missing, previous):
    """Model an attended tilt about the board's original area, with its lowest corner on the floor."""
    qx,qy,qz,qw = robot["rotation_xyzw"]
    yaw = math.atan2(2*(qw*qz+qx*qy),1-2*(qy*qy+qz*qz))
    body = rotation(0.,0.,yaw)
    photo = np.array(robot["position_m"])+body@[.17,0.,.40]
    base_to_optical = np.array([[0,0,1],[-1,0,0],[0,-1,0]])
    to_optical = body@base_to_optical
    options = []
    for roll,pitch in product((-.40,-.30,-.23,-.13,0.,.13,.23,.30,.40),
                              (-.52,-.37,-.24,-.12,0.,.12,.24,.37,.52)):
        board = rotation(roll,pitch,0.)
        pose = (*fixed_xy,.7*abs(board[2,1])+.5*abs(board[2,2])+.009,roll,pitch,0.)
        if previous and math.dist(previous[-1][3:],pose[3:])<.2:
            continue
        points = (PATTERN@board.T+pose[:3]-photo)@to_optical
        if points[:,2].min()<=.1:
            continue
        uv = points[:,:2]/points[:,2,None]*FOCAL+[WIDTH/2,HEIGHT/2]
        margin = np.min(np.minimum(uv,[WIDTH,HEIGHT]-uv))
        if margin<14:
            continue
        n = board[:,0]@to_optical
        right = -board[:,1]@to_optical
        pitch_deg = math.degrees(math.atan2(n[1],n[2]))
        roll_deg = math.degrees(math.atan2(right[1],right[0]))
        cells = {f"{('top','middle','bottom')[int(v*3/HEIGHT)]}_{('left','center','right')[int(u*3/WIDTH)]}" for u,v in uv}
        bottom = cells & missing & {"bottom_left","bottom_center","bottom_right"}
        to_fix = {"pitch_up":pitch_deg>=12,"pitch_down":pitch_deg<=-12,
                  "roll_left":roll_deg<=-10,"roll_right":roll_deg>=10,"bottom":bool(bottom)}
        if not to_fix.get(aim):
            continue
        desired_pitch = {"pitch_up":20.,"pitch_down":-20.,"bottom":20.}.get(aim,0.)
        desired_roll = {"roll_left":-15.,"roll_right":15.}.get(aim,0.)
        score = (4*len(bottom) + min(margin,50)/100
                 - .08*abs(pitch_deg-desired_pitch) - .08*abs(roll_deg-desired_roll))
        options.append((score,pose))
    if not options:
        raise ValueError("no physically realizable attended tilt at the original board placement")
    return max(options)[1]


if __name__=="__main__":
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument("aim",choices=["common","bottom","pitch_down","pitch_up","roll_left","roll_right","spread","holdout"])
    cli.add_argument("--missing",default="[]")
    cli.add_argument("--previous",default="[]")
    cli.add_argument("--fixed-robot",help="Current Gazebo world pose for an attended tilt at the original placement")
    cli.add_argument("--fixed-xy",default="[3.1,0.0]")
    args=cli.parse_args()
    missing,previous = set(json.loads(args.missing)),json.loads(args.previous)
    if args.fixed_robot:
        pose = tilt_at_placement(args.aim,json.loads(args.fixed_robot),json.loads(args.fixed_xy),missing,previous)
    else:
        poses=candidates(args.aim,missing,previous)
        if not poses:
            raise SystemExit("no suitable fixture placement")
        pose=poses[0]
    set_board(pose)
    print(json.dumps(dict(aim=args.aim,xyz_rpy=pose)))
