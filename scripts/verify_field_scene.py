"""Export two immutable local scenes from the facts service, or reproduce their geometry offline.

Inside facts: python3 -B verify_field_scene.py collect RGBD_SHA256 SNAPSHOT_SHA256 > scenes.zip
After extracting scenes.zip: python3 -B scripts/verify_field_scene.py verify EXTRACTED_DIRECTORY
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import zipfile
from urllib.request import ProxyHandler, build_opener


def require(ok,detail):
    if not ok:
        raise ValueError(detail)


def ref(data):
    return "sha256:"+hashlib.sha256(data).hexdigest()


def name(digest):
    require(re.fullmatch(r"sha256:[0-9a-f]{64}",digest),"invalid artifact digest")
    return digest[7:]


def collect(mesh_ref,scan_ref):
    root=Path("/evidence")
    def bound(folder,digest,ext):
        data=(root/folder/(name(digest)+ext)).read_bytes()
        require(ref(data)==digest,"frozen artifact digest mismatch")
        return data
    mesh=bound("rgbd-scenes",mesh_ref,".json")
    scan=bound("snapshots",scan_ref,".json")
    source=json.loads(scan)["source_evidence"]
    rgb_sources=json.loads(mesh)["source_evidence"]
    with build_opener(ProxyHandler({})).open("http://127.0.0.1:8150/scene/"+name(scan_ref)+".json", timeout=5) as r:
        voxel=r.read(2_000_000)
    files={"rgbd-scene.json":mesh,"occupancy-scene.json":voxel,"lidar-snapshot.json":scan,
           "lidar-calibration.json":Path("/site/jetson-rover.json").read_bytes(),
           "chassis-profile.json":Path("/opt/edge/perception/rover_preview/lingao.json").read_bytes(),
           "lidar-task-color.png":bound("artifacts",source["camera"]["ref"],".png")}
    for cloud in source["lidars"]:
        sid=cloud["source_id"]
        require(sid in ("mid360.front","mid360.rear"),"unexpected scan source")
        files[sid+".json"]=bound("artifacts",cloud["artifact_ref"],".json")
    for key,suffix in (("color",".png"),("depth",".png"),("calibration",".json")):
        files["rgbd-"+key+suffix]=bound("artifacts",rgb_sources[key]["artifact_ref"],suffix)
    manifest={"schema_version":1,"rgbd_scene_ref":mesh_ref,"lidar_snapshot_ref":scan_ref,
        "rgbd_stamp_ns":json.loads(mesh)["stamp_ns"],"lidar_stamp_ns":json.loads(scan)["scan_stamp_ns"],
        "relative_pose_between_scenes":None,"files":{k:ref(v) for k,v in files.items()}}
    files["manifest.json"]=(json.dumps(manifest,indent=2)+"\n").encode()
    out=io.BytesIO()
    with zipfile.ZipFile(out,"w",compression=zipfile.ZIP_DEFLATED) as z:
        for filename,data in files.items():
            z.writestr(filename,data)
    sys.stdout.buffer.write(out.getvalue())


def verify(directory):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from perception.rover_plan_field.rgb_scene import build_rgbd
    from perception.rover_plan_field.scene import build_scene
    manifest=json.loads((directory/"manifest.json").read_bytes())
    def read(filename):
        data=(directory/filename).read_bytes()
        require(ref(data)==manifest["files"][filename],"file digest mismatch for "+filename)
        return data
    for filename in manifest["files"]:
        require(Path(filename).name==filename,"invalid archive filename")
        read(filename)
    raw=read("rgbd-scene.json")
    require(ref(raw)==manifest["rgbd_scene_ref"],"RGB-D scene digest")
    mesh=json.loads(raw)
    s=mesh["source_evidence"]
    for key,suffix in (("color",".png"),("depth",".png"),("calibration",".json")):
        require(s[key]["artifact_ref"]==ref(read("rgbd-"+key+suffix)),key+" binding")
        require(s[key]["frame_id"]==mesh["frame_id"],key+" frame")
    require(mesh["stamp_ns"]==s["color"]["stamp_ns"]==s["depth"]["stamp_ns"]==manifest["rgbd_stamp_ns"],
            "unsynchronized RGB and depth")
    computed=build_rgbd(read("rgbd-color.png"),read("rgbd-depth.png"),json.loads(read("rgbd-calibration.json")),
                        mesh["frame_id"],stride=mesh["stride_px"])
    require(all(mesh.get(k)==v for k,v in computed.items()),"RGB geometry, color or topology differ")
    require(mesh["texture_url"]=="/camera/"+name(s["color"]["artifact_ref"])+".png","texture URL differs")
    scan_raw=read("lidar-snapshot.json")
    require(ref(scan_raw)==manifest["lidar_snapshot_ref"],"task snapshot digest")
    scan=json.loads(scan_raw)
    require(scan["scan_stamp_ns"]==manifest["lidar_stamp_ns"],"scan clock")
    sources=scan["source_evidence"]
    require(ref(read("lidar-task-color.png"))==sources["camera"]["ref"],"task optical binding")
    clouds={s["artifact_ref"]:read(s["source_id"]+".json") for s in sources["lidars"]}
    occupancy=build_scene(scan,json.loads(read("lidar-calibration.json")),clouds.__getitem__,
                          footprint=json.loads(read("chassis-profile.json")),snapshot_ref=ref(scan_raw))
    require(occupancy==json.loads(read("occupancy-scene.json")),"occupancy or rays differ")
    print(json.dumps({"lidar_voxels":len(occupancy["voxels"]),"rgbd_valid_vertices":sum(v is not None for v in mesh["vertices"]),
          "rgbd_triangles":len(mesh["triangles"]),"rgbd_stamp_ns":mesh["stamp_ns"],"lidar_stamp_ns":scan["scan_stamp_ns"],
          "source_files_verified":len(manifest["files"])},indent=2))


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    cli=parser.add_subparsers(dest="action",required=True)
    pack=cli.add_parser("collect")
    pack.add_argument("mesh_ref")
    pack.add_argument("scan_ref")
    cli.add_parser("verify").add_argument("directory",type=Path)
    args=parser.parse_args()
    if args.action=="collect":
        collect(args.mesh_ref,args.scan_ref)
    else:
        verify(args.directory)
