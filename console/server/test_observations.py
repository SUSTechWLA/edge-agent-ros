"""Exercise manifest hot-plug through real ROS delivery and the public HTTP API."""
import json
import os
import tempfile
import threading
import time
import unittest
from functools import partial
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.request import urlopen

import rclpy
from rclpy.executors import SingleThreadedExecutor
from rclpy.node import Node
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Range

from main import Handler
from observations import preview
from observer import Observer


class PreviewTest(unittest.TestCase):
    def test_large_sequence_reads_only_the_prefix(self):
        class CountReads:
            def __len__(self):
                return 4_000_000
            def __getitem__(self,index):
                if index>=32:
                    raise AssertionError("walked beyond the display prefix")
                return index
        self.assertEqual(preview(CountReads()),(list(range(32)),True))
        outer = [CountReads() for _ in range(40)]
        data,truncated = preview(outer)
        self.assertTrue(truncated)
        self.assertLessEqual(sum(len(row)+1 for row in data)+1,192)
        self.assertEqual(preview("x"*400),("x"*256,True))


class ManifestTest(unittest.TestCase):
    def test_live_registration_silence_recovery_and_unload(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            mounts = directory/"observations"
            mounts.mkdir()
            env = {"EDGE_ENVIRONMENT":"hardware","EDGE_CONSOLE_DIR":tmp,"EDGE_OBSERVATIONS_DIR":str(mounts),
                   "EDGE_LEDGER_PATH":str(directory/"core.db"),"EDGE_TASK_DB":str(directory/"tasks.db")}
            with patch.dict(os.environ,env):
                rclpy.init()
                node = Observer()
                writer = Node("additional_sensor")
                executor = SingleThreadedExecutor()
                executor.add_node(node)
                httpd = ThreadingHTTPServer(("127.0.0.1",0),partial(Handler,observer=node))
                server = threading.Thread(target=httpd.serve_forever,daemon=True)
                server.start()
                try:
                    self.exercise(node,writer,executor,mounts,httpd.server_port)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    server.join()
                    executor.shutdown()
                    writer.destroy_node()
                    node.destroy_node()
                    node.history.close()
                    rclpy.shutdown()

    def exercise(self, node, writer, executor, directory, port):
        def get(path):
            with urlopen(f"http://127.0.0.1:{port}"+path,timeout=3) as reply:
                return json.load(reply)

        def until(predicate, publish=None):
            end = time.monotonic()+7
            while time.monotonic()<end:
                if publish:
                    publish()
                executor.spin_once(timeout_sec=0.04)
                if predicate():
                    return
                time.sleep(0.015)
            self.fail("timeout waiting for external observation transition")

        self.assertEqual(get("/api/state")["extra_sources"],{})
        pub = writer.create_publisher(Range,"/front/range",qos_profile_sensor_data)
        config = {"id":"range","name":"front range","topic":"/front/range","type":"sensor_msgs/msg/Range","ttl_s":0.7}
        definition = directory/"range.json"
        definition.write_text(json.dumps(config))
        (directory/"other.json").write_text(json.dumps({**config,"id":"other","type":"missing_test_package/msg/Unknown"}))
        def status(key):
            return node.snapshot()["extra_sources"].get(key,{}).get("status")
        until(lambda:status("range")=="waiting" and status("other")=="error")
        self.assertTrue(get("/api/state")["extra_sources"]["other"]["error"])
        msg = Range()
        msg.header.stamp.sec = 2
        msg.header.frame_id = "range_link"
        msg.range = 0.875
        msg.min_range = 0.125
        msg.max_range = 3.0
        until(lambda:node.extra.items["range"].last_status=="online",lambda:pub.publish(msg))
        first = get("/api/state")["extra_sources"]["range"]
        self.assertEqual((first["frame"],first["stamp_s"],first["data"]["range"]),("range_link",2.0,0.875))
        self.assertFalse(first["truncated"])
        until(lambda:node.extra.items["range"].last_status=="stale")
        msg.header.stamp.sec = 3
        msg.range = 1.125
        until(lambda:node.extra.items["range"].last_status=="online",lambda:pub.publish(msg))
        # A sensor registered now must not leak into a past mission snapshot.
        self.assertEqual(node.at_source_time("2020-01-01T00:00:00Z")["extra_sources"],{})
        definition.unlink()
        def continue_sampling():
            msg.header.stamp.nanosec += 1000000
            pub.publish(msg)
        until(lambda:status("range") is None and pub.get_subscription_count()==0,continue_sampling)
        self.assertNotIn("range",get("/api/state")["extra_sources"])
        events = get("/api/events?after=0&limit=100")["events"]
        chain = [e for e in events if e["data"].get("id")=="range"]
        self.assertEqual([e["kind"] for e in chain],["observation."+v for v in ("added","online","stale","online","removed")])
        self.assertEqual([e["cause_seq"] for e in chain],[None]+[e["seq"] for e in chain[:-1]])
        replay = [get(f"/api/replay?at={e['seq']}")["extra_sources"].get("range") for e in chain]
        self.assertEqual([s["status"] if s else None for s in replay],["waiting","online","stale","online",None])
        self.assertEqual(replay[3]["data"]["range"],1.125)
        self.assertEqual(len([e for e in events if e["kind"]=="observation.error"]),1)
        print("Range lifecycle:",[(e["seq"],e["kind"]) for e in chain],
              "stale age:",chain[2]["data"]["age_s"],"recovered range:",replay[3]["data"]["range"],flush=True)


if __name__=="__main__":
    unittest.main()
