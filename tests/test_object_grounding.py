import hashlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/"tests")]
from edge_contracts.spatial_facts import SpatialFactError
from perception.spatial_facts.object_grounding import description, locate_object
from perception.spatial_facts.object_models import load_yolo
import test_visual_grounding as fixtures


class ObjectSurface(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.VisualGroundingTest()
        f = self.fixture
        f.setUp()
        for y in range(f.h):
            for x in range(f.w):
                green = 63<=x<75 and 8<=y<28
                f.rgb[3*(y*f.w+x):3*(y*f.w+x+1)] = bytes((66,98,35) if green else (116,121,126))
                f.depths[y*f.w+x]=850 if green else 1700
        f.render()
        self.model="sha256:"+"a"*64
        f.profile["grounding"]["object_model_ref"]=self.model
        self.detections=[{"label":"potted plant","score":.75,"bbox_xyxy":[42,4,79,57]}]

    def prediction(self, data):
        return {"provider":"fixture","version":"1","device":"cpu","model_ref":self.model,
                "rgb_ref":"sha256:"+hashlib.sha256(data).hexdigest(),"detections":self.detections}

    def result(self, target="画面右侧的绿植", predictor=None):
        f=self.fixture
        return locate_object(target,f.batch,f.store,f.profile,now_ns=f.now,predict=predictor or self.prediction)

    def test_inside_measured_leaf_outside_box_center(self):
        result=self.result()
        self.assertEqual([42,4,79,57],result["detection"]["bbox_xyxy"])
        self.assertEqual([63,8,75,28],result["bbox_xyxy"])
        u,v=result["point_pixel"]
        self.assertTrue(63<u<74 and 8<v<27)
        self.assertEqual(850,result["point_depth_raw"])
        self.assertAlmostEqual(.85,result["axial_depth_m"])
        self.assertGreater(result["range_m"],.85)
        self.assertEqual("detected_instance_green_pixels",result["selection"]["support"])
        self.assertEqual("potted plant",description("画面右边绿植的绿色叶片")[1])

    def test_model_receipt_matches_input_and_identity(self):
        for field in ("model_ref","rgb_ref"):
            def wrong(raw):
                return {**self.prediction(raw),field:"sha256:"+"f"*64}
            with self.subTest(field=field),self.assertRaisesRegex(SpatialFactError,"detection_binding_mismatch"):
                self.result(predictor=wrong)

    def test_semantic_targets_require_single_eligible_detection(self):
        self.detections.append({**self.detections[0],"bbox_xyxy":[48,6,70,38]})
        with self.assertRaisesRegex(SpatialFactError,"target_ambiguous"):
            self.result()
        self.detections.clear()
        with self.assertRaisesRegex(SpatialFactError,"target_absent"):
            self.result()
        self.assertIsNone(self.result("画面右侧的绿色区域")["detection"])
        with self.assertRaisesRegex(SpatialFactError,"unsupported_target"):
            self.result("桌上的小型无人机")

    def test_no_depth_sampling_on_background_or_boundary(self):
        f=self.fixture
        for y in range(8,28):
            for x in range(63,75):
                f.depths[y*f.w+x]=0
        f.render()
        with self.assertRaisesRegex(SpatialFactError,"depth_insufficient"):
            self.result()
        for y in range(8,28):
            for x in range(63,75):
                f.depths[y*f.w+x]=1200 if (x+y)%2 else 650
        f.render()
        with self.assertRaisesRegex(SpatialFactError,"depth_surface_unverified"):
            self.result()

    def test_generic_object_mask_correlated_to_instance(self):
        self.detections=[{"label":"chair","score":.6,"bbox_xyxy":[40,0,80,60]}]
        with self.assertRaisesRegex(SpatialFactError,"foreground_unavailable"):
            self.result("右边的椅子")
        self.detections[0]["foreground"]={"stride":1,"grid_width":80,"grid_height":60,
                                         "runs":[[y,63,75] for y in range(8,28)]}
        result=self.result("右边的椅子")
        self.assertEqual(result["point_depth_raw"],850)
        self.assertEqual(result["selection"]["support"],"detected_instance_rgb_grabcut")

    def test_digest_checked_before_model_loading(self):
        with tempfile.TemporaryDirectory() as tmp:
            file=Path(tmp)/"model.pt"
            file.write_bytes(b"not a reviewed release")
            with self.assertRaisesRegex(SpatialFactError,"model_binding_mismatch"):
                load_yolo(file,self.model)


if __name__=="__main__":
    unittest.main()
