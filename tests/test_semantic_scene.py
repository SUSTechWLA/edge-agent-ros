import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import planning
from edge_contracts.semantic_scene import compact_scene, target_scene
from exploration.survey_providers import MapStore


def local_scene():
    return target_scene(target="four_hole_board", frame="rgb_optical", stamp_ns=123456,
                        archive="a"*64, plane={"center_m":[.1,0,2.3],"normal":[0,0,1]},
                        detector={"id":"board","version":"1","manifest_sha256":"b"*64},
                        lidar_roles={"front":"front_mid360"}, seen=[])


class SceneTest(unittest.TestCase):
    def test_single_view_keeps_frame_and_never_fuses_missing_lidar(self):
        scene = local_scene()
        self.assertIsNone(scene["map_ref"])
        self.assertEqual(scene["nodes"][0]["position_m"], [.1,0,2.3])
        self.assertEqual(scene["edges"], [["target","observed_by","camera"]])
        self.assertEqual(scene["nodes"][2]["state"], "not_detected")
        for updates in ({"edges":[["target","inside","unseen"]]}, {"map_ref":{"id":"campus"}},
                        {"nodes":[{**scene["nodes"][0],"position_m":[0,float("nan"),0]}]}):
            with self.assertRaises(ValueError):
                compact_scene({**scene, **updates})
        self.assertEqual(compact_scene({**scene,"raw_lidar":[0]*2000}),scene)

    def test_published_map_query_preserves_ambiguity_and_2d(self):
        with tempfile.TemporaryDirectory() as temp:
            store=MapStore(temp)
            places=[dict(entity_id=key, name="入口",state="proposed",anchor=[i,.5],
                         entrance_pose=None) for i,key in enumerate(("north","south"))]
            record=store.publish(dict(map_id="campus",operation_id="map/1", grid=dict(
                width=3,height=3, resolution=1., frame="odom",origin=[0,0,0], cells=[0]*9),
                semantic=places, robot_id="rover",calibration_id="rig",stamp_ns=256,state="PARTIAL",reason="survey"))
            ref=record["map_ref"]
            query=dict(map_id="campus",revision=ref["revision"],query="入口")
            all_rows=store.scene(query)
            self.assertEqual(all_rows["map_ref"],ref)
            self.assertEqual([n["id"] for n in all_rows["nodes"]],["north","south"])
            self.assertEqual(store.scene(dict(query,limit=1))["omitted"],1)
            self.assertEqual(store.scene(dict(query,query="south"))["nodes"][0]["position_m"],[1,.5])
            with self.assertRaises(ValueError):
                store.scene({"map_id":"campus"})
            with self.assertRaises(LookupError):
                store.scene({**query,"revision":"c"*64})

    def test_model_receives_bounded_scene_from_real_planner_entry(self):
        site=Path(__file__).resolve().parents[1]/"calibration/station"
        caps={p.stem:json.loads(p.read_bytes()) for p in (site/"tools").glob("*.json")}
        prior={"tool":"inspect_calibration_view","args":{},"status":"SUCCEEDED",
               "feedback":{"goal":{"tool":"verify_sensor_extrinsics"}},
               "evidence":{"scene_slice":local_scene(),"raw_cloud":[9]*4096,"environment":"simulation",
                           "robot_id":"gazebo-lingao","image_topic":"/fixed/rgbd/image"}}
        task={"id":"r1","text":"请帮我标定当前设备","steps":[prior]}
        def choose(url, body, timeout):
            brief=body["observations"][0]
            self.assertEqual(brief["scene"]["frame"],"rgb_optical")
            self.assertNotIn("raw_cloud",json.dumps(body))
            for deployment_value in ("simulation","gazebo-lingao","/fixed/rgbd/image"):
                self.assertNotIn(deployment_value,json.dumps(body))
            return dict(goal="sensor_calibration",tool="sensor_calibration_status",args={},reason="检查采集进度")
        planning.feedback_plan(task,caps,site/"feedback.json",None,
                               model_profile={"protocol":"http-planner","url":"local","version":"1"},
                               model_request=choose)


if __name__ == "__main__":
    unittest.main()
