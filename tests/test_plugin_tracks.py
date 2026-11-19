"""A mixed task freezes both owners before dispatch. A collision removes the new grant."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "agent"))
import plugin_tracks


class Router(unittest.TestCase):
    def test_mixed_freeze_keeps_original_operation_on_replay(self):
        steps = [{"soft_pin":{"binding":"a"}}, {"physical_pin":{"binding":"b"}}]
        calls = []
        def freeze(track, action, task, provider, steps):
            self.assertEqual((action,task,provider),("freeze_plan","task","planner"))
            from copy import deepcopy
            result = deepcopy(steps)
            index = 0 if track == "soft" else 1
            result[index][track + "_pin"]["reservation_id"] = f"{task}/step/{index+1}"
            calls.append(track)
            return provider, result
        with patch.object(plugin_tracks.soft_client,"enabled",return_value=True), \
             patch.object(plugin_tracks.soft_client,"request",side_effect=lambda *a,**k:freeze("soft",*a,**k)), \
             patch.object(plugin_tracks,"physical_request",side_effect=lambda *a,**k:freeze("physical",*a,**k)):
            first = plugin_tracks.freeze("task",["planner",steps])
            self.assertEqual(first,plugin_tracks.freeze("task",["planner",steps]))
        self.assertEqual(calls,["soft","physical"] * 2)
        self.assertEqual(first[1][0]["soft_pin"]["reservation_id"],"task/step/1")
        self.assertEqual(first[1][1]["physical_pin"]["reservation_id"],"task/step/2")

    def test_cross_track_conflict_withholds_both_exports(self):
        name = "capture_frame"
        cap = {"id":name}
        soft = {"tools":[cap], "claims":{name:"one"}}
        physical = {"tools":[cap], "claims":{name:"two"}}
        with patch.object(plugin_tracks,"views",return_value=(soft,physical)):
            self.assertEqual(plugin_tracks.catalog({"status":{"id":"status"}}),{"status":{"id":"status"}})

    def test_physical_pin_never_falls_back_to_soft(self):
        with patch.dict("os.environ",{"EDGE_PHYSICAL_SOCKET":""}), patch.object(plugin_tracks.soft_client,"request") as wrong:
            with self.assertRaises(plugin_tracks.soft_client.SoftError) as caught:
                plugin_tracks.call_for({"class":"physical"},"call",wire={})
        self.assertEqual(caught.exception.code,"tool.binding_unavailable")
        wrong.assert_not_called()


if __name__ == "__main__":
    unittest.main()
