import hashlib
import json
import math
import struct
import tempfile
import time
import unittest
import zlib
from pathlib import Path

from edge_contracts.image_frames import chunk
from perception.rover_plan_field import rgb_scene, scene


def digest(data):
    return "sha256:"+hashlib.sha256(data).hexdigest()


def png(width,height,raw,*,depth=False):
    header=struct.pack(">IIBBBBB",width,height,16 if depth else 8,0 if depth else 2,0,0,0)
    row=width*(2 if depth else 3)
    return (b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR",header)
            +chunk(b"IDAT",zlib.compress(b"".join(b"\0"+raw[i:i+row] for i in range(0,len(raw),row))))
            +chunk(b"IEND",b""))


class LocalSceneTest(unittest.TestCase):
    def test_voxel_height_transforms_and_observed_ground(self):
        cal=digest(b"nominal mount")
        sensor={"translation_m":[0.,0.,.25],"quaternion_xyzw":[0.,0.,math.sqrt(.5),math.sqrt(.5)],
                "frame_id":"sensor","extrinsic_ref":cal}
        cfg={"model_evidence":{"digest":cal},"sources":{"lidar":sensor}}
        c={"source_id":"lidar","frame_id":"sensor","type":"livox.mid360.preview","last_receive_ns":1000,
           "points":[[1.,-.02,.12,10],[1.,-.02,.13,12],[2.,-.02,1.,10]]}
        raw=json.dumps(c).encode()
        evidence={"source_id":"lidar","frame_id":"sensor","artifact_ref":digest(raw),
                  "stamp_ns":"1000","sampled_points":3}
        snap={"source_evidence":{"lidars":[evidence]},"calibration_ref":cal,"scan_stamp_ns":"1000","base_frame":"base"}
        result=scene.build_scene(snap,cfg,lambda ref:raw)
        self.assertEqual(2,len(result["voxels"]))
        self.assertIn([.05,1.05,.35,2],result["voxels"])
        self.assertIn([.05,1.05],result["occupied_ground"])
        self.assertIn([.05,.55],result["observed_ground"])
        self.assertNotIn([.05,1.05],result["observed_ground"])
        self.assertEqual("unavailable", result["texture"]["mode"])
        with self.assertRaises(ValueError):
            scene.build_scene(snap,cfg,lambda ref:raw+b" ")
        with self.assertRaises(ValueError):
            scene.build_scene({**snap,"calibration_ref":digest(b"other")},cfg,lambda ref:raw)

    def test_replay_real_hash_bound_jetson_window_when_present(self):
        directory=Path(__file__).resolve().parents[1]/"artifacts/verification/jetson-field-plan-20261123/verified/sensor-evidence"
        if not (directory/"snapshot.json").exists():
            self.skipTest("field archive is optional")
        snap=json.loads((directory/"snapshot.json").read_bytes())
        cfg=json.loads((directory/"sensor-calibration.json").read_bytes())
        files={s["artifact_ref"]:(directory/(s["source_id"]+".json")).read_bytes() for s in snap["source_evidence"]["lidars"]}
        profile={"front_m":.259,"rear_m":.234,"left_m":.166,"right_m":.166}
        one=scene.build_scene(snap,cfg,files.__getitem__,footprint=profile)
        self.assertEqual((330,597,123),(len(one["voxels"]),len(one["observed_ground"]),len(one["occupied_ground"])))
        self.assertEqual("camera_panel",one["texture"]["mode"])
        self.assertAlmostEqual(99.035,one["texture"]["skew_ms"])


class RGBDepthTest(unittest.TestCase):
    frame="camera_color_optical_frame"
    width=height=9

    def setUp(self):
        self.pixels=bytes((13,123,240))*81
        self.rgb=png(9,9,self.pixels)
        self.depths=[1000]*81
        self.info={"type":"sensor_msgs/msg/CameraInfo","frame_id":self.frame,"width":9,"height":9,
                   "distortion_model":"plumb_bob","d":[0]*5,"k":[100,0,4,0,100,4,0,0,1]}

    def depth(self):
        return png(9,9,struct.pack(">81H",*self.depths),depth=True)

    def test_mapped_pixel_and_hidden_depth_discontinuity(self):
        read=lambda:rgb_scene.build_rgbd(self.rgb,self.depth(),self.info,self.frame)
        valid=read()
        self.assertEqual([-.04,-.04,1.,13,123,240],valid["vertices"][0])
        self.assertEqual([.04,.04,1.,13,123,240],valid["vertices"][3])
        self.assertEqual(2,len(valid["triangles"]))
        for z in (0,1800):
            self.depths[40]=z
            self.assertFalse(read()["triangles"])
        self.info["d"][0]=.1
        with self.assertRaises(ValueError):
            read()

    def test_latest_requires_same_exposure_calibration_and_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            roots=[Path(tmp)/s for s in ("color","depth","info")]
            stamp=time.time_ns()
            cal=json.dumps(self.info).encode()
            for root,sid,raw in zip(roots,(rgb_scene.COLOR,rgb_scene.DEPTH,rgb_scene.INFO),(self.rgb,self.depth(),cal)):
                (root/"artifacts").mkdir(parents=True)
                ref=digest(raw)
                sample={"stamp_ns":str(stamp),"frame_id":self.frame,"source_id":sid,"artifact_ref":ref,
                        "calibration_ref":digest(cal),"clock_id":"unix","media_type":"image/png"}
                if sid==rgb_scene.INFO:
                    (root/"observations.json").write_text(json.dumps({"actor_ref":"r","samples":[sample]}))
                    (root/"artifacts"/(ref[7:]+".json")).write_bytes(raw)
                else:
                    (root/"index.json").write_text(json.dumps({"source_id":sid,"actor_ref":"r","frames":[sample]}))
                    (root/"artifacts"/(ref[7:]+".png")).write_bytes(raw)
            archived={}
            out=rgb_scene.latest_scene(*roots,archive=lambda ref,s,b:archived.update({ref:b}))
            self.assertEqual(str(stamp),out["stamp_ns"])
            self.assertEqual("camera_only",out["registration"])
            self.assertEqual(3,len(archived))
            bad=roots[1]/"index.json"
            index=json.loads(bad.read_text())
            index["frames"][0]["stamp_ns"]=str(stamp+1)
            bad.write_text(json.dumps(index))
            with self.assertRaisesRegex(ValueError,"same-exposure"):
                rgb_scene.latest_scene(*roots)


if __name__=="__main__":
    unittest.main()
