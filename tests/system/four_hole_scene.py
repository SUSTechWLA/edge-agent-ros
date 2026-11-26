"""Synthetic camera and two rigidly mounted scanners, independent of the calibration solver."""
import cv2
import numpy as np

PROFILE = {
    "camera_frame": "rgb_optical",
    "lidar_frames": {"left": "mid_a", "right": "mid_b"},
    "four_hole_board": {
        "marker_size_m": .2, "marker_spacing_m": [1.1, .7],
        "hole_spacing_m": [.5, .4], "hole_diameter_m": .24,
    },
}
K = np.array([[805.,0,640.], [0,805.,400.], [0,0,1.]])
TAGS = ((1,150,150), (2,1250,150), (4,1250,850), (3,150,850))
HOLES = np.array([[-.25,-.20], [.25,-.20], [.25,.20], [-.25,.20]])
POSES = [
    ([.02,-.04,.01], [-.12,-.10,1.75]), ([.22,.10,-.10], [.15,-.04,1.90]),
    ([-.19,.28,.05], [-.07,.15,2.05]), ([.08,-.28,.11], [.1,-.14,1.85]),
    ([-.27,-.12,.02], [.03,.10,2.1]),
]
SENSORS = {
    "left": (cv2.Rodrigues(np.array([-.07,.22,.45]))[0], np.array([-.21,.04,.03])),
    "right": (cv2.Rodrigues(np.array([.10,-.27,-.60]))[0], np.array([.21,.06,.01])),
}


def render(axis, shift, omit=()):
    pattern = np.full((1000,1400),255,np.uint8)
    dictionary = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_6X6_250)
    for marker,x,y in TAGS:
        if marker not in omit:
            if hasattr(cv2.aruco,"generateImageMarker"):
                tile = cv2.aruco.generateImageMarker(dictionary,marker,200)
            else:
                tile = cv2.aruco.drawMarker(dictionary,marker,200)
            pattern[y-100:y+100, x-100:x+100] = tile
    for hx,hy in HOLES:
        cv2.circle(pattern,(round(700+1000*hx), round(500+1000*hy)),120,108,-1,cv2.LINE_AA)
    plane = np.array([[-.7,-.5,0], [.7,-.5,0], [.7,.5,0], [-.7,.5,0]],float)
    pixels = cv2.projectPoints(plane,np.array(axis),np.array(shift),K,None)[0].astype(np.float32)
    H = cv2.getPerspectiveTransform(np.float32([[0,0],[1400,0],[1400,1000],[0,1000]]), pixels)
    gray = cv2.warpPerspective(pattern,H,(1280,800),borderValue=178)
    return cv2.cvtColor(gray,cv2.COLOR_GRAY2RGB)


def cloud(axis, shift, sensor, holes=HOLES, seed=826):
    rng = np.random.default_rng(seed)
    xy = rng.uniform([-.7,-.5],[.7,.5], size=(10500,2))
    for hole in holes:
        xy = xy[np.linalg.norm(xy-hole,axis=1)>.12]
    target = np.column_stack((xy, rng.normal(0,.0015,len(xy))))
    camera = target @ cv2.Rodrigues(np.asarray(axis))[0].T + shift
    # A second, solid wall is seen through and around the actual board.
    background = rng.uniform([-1,-.7,2.6],[1,.7,2.605],size=(6000,3))
    r,t = SENSORS[sensor]
    return (np.concatenate((camera,background))-t) @ r





def score(publication):
    """Compare the solver's immutable revision to the independent simulated scanner mounts."""
    report=publication["report"]
    fit_ids=report["fit_ids"]
    check_ids=report["validation_ids"]
    if (set(fit_ids)&set(check_ids) or len(set(fit_ids))<3 or len(set(check_ids))<2
            or report["status"]!="VERIFIED" or set(report["transforms"])!={"lidar_left","lidar_right"}):
        raise ValueError("missing independent spatial validation")
    scores={}
    for role,(rotation,offset) in SENSORS.items():
        tf=report["transforms"]["lidar_"+role]
        quaternion=np.asarray(tf["rotation_xyzw"])
        if abs(np.linalg.norm(quaternion)-1)>1e-6:
            raise ValueError("invalid unit rotation")
        x,y,z,w=quaternion
        actual=np.array([[1-2*(y*y+z*z),2*(x*y-z*w),2*(x*z+y*w)],
                         [2*(x*y+z*w),1-2*(x*x+z*z),2*(y*z-x*w)],
                         [2*(x*z-y*w),2*(y*z+x*w),1-2*(x*x+y*y)]])
        angle=float(np.degrees(np.arccos(np.clip((np.trace(rotation.T@actual)-1)/2,-1.,1.))))
        error=float(np.linalg.norm(np.asarray(tf["translation_m"])-offset))
        if (tf["parent_frame"]!="rgbd_optical" or tf["child_frame"]!="lidar_"+role
                or error>.015 or angle>.75):
            raise ValueError(f"mount error: {role} {error:.5f} m {angle:.3f} deg")
        scores[role]=dict(translation_error_m=error,rotation_error_deg=angle,
                          truth_translation_m=offset.tolist(),truth_rotation_matrix=rotation.tolist(),
                          held_out_max_hole_error_m=tf["validation"]["max_hole_error_m"])
    return dict(scope="synthetic ROS DDS, generated images and point clouds",
                task_id=publication["task_id"],fit_ids=fit_ids, validation_ids=check_ids,
                limits=dict(translation_error_m=.015,rotation_error_deg=.75),
                independent_truth=scores)


if __name__=="__main__":
    import json
    import sys
    from pathlib import Path
    print(json.dumps(score(json.loads(Path(sys.argv[1]).read_bytes())),ensure_ascii=False,indent=2))
