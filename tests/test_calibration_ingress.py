"""Run in the hardware observer image using generated ROS 2 message types."""
from array import array
import struct
import time
import unittest

import cv2
import numpy as np
from sensor_msgs.msg import CameraInfo, Image, PointCloud2, PointField

from calibration.board_ros import BoardSource, decode_cloud
from calibration.camera_geometry import decode_rgb
from calibration.clock_observer import point_window


def cloud(big_endian=False):
    fields = [PointField(name=name, offset=offset, datatype=kind, count=1)
              for name, offset, kind in (("intensity",12,7), ("timestamp",18,8), ("line",17,2),
                                         ("z",8,7), ("x",0,7), ("tag",16,2), ("y",4,7))]
    msg = PointCloud2(width=2, height=2, point_step=26, row_step=64,
                      is_bigendian=big_endian, fields=fields)
    payload = bytearray(b"\xee" * (msg.row_step * msg.height))
    for i, sec in enumerate((10., 10.008, 10.012, 10.02)):
        start = i//2*msg.row_step + i%2*msg.point_step
        struct.pack_into((">" if big_endian else "<")+"ffffBBd", payload, start,
                         i+.25, i-2., .5, 120., 4, 2, sec)
    msg.data = array("B", payload)
    return msg


def image_info():
    width, height, padding = 32, 24, 5
    bgr = np.random.default_rng(45).integers(0,256,(height,width,3),dtype=np.uint8)
    buf = np.pad(bgr.reshape(height,-1), ((0,0),(0,padding)), constant_values=255)
    image = Image(width=width, height=height, step=width*3+padding, encoding="bgr8",
                  data=array("B",buf.tobytes()))
    info = CameraInfo(width=width, height=height, distortion_model="plumb_bob",
                      d=[.23,-.07,.01,-.01,0.])
    info.k = [24.,0.,15.5, 0.,27.,12., 0.,0.,1.]
    info.p = [22.,0.,15.,0., 0.,22.,11.,0., 0.,0.,1.,0.]
    return image, info, np.ascontiguousarray(bgr[:,:,::-1])


class CalibrationIngress(unittest.TestCase):
    def test_readonly_board_source_receives_mixed_livox_over_dds(self):
        from rclpy.qos import qos_profile_sensor_data
        profile = dict(camera=dict(topic="/ingress/rgb", info_topic="/ingress/info",
                                   frame="optical", image_geometry="rectified"),
                       lidars=[dict(id="left", topic="/ingress/lidar", frame="left")],
                       max_skew_ns=25_000_000)
        image, info, expected = image_info()
        points = cloud()
        topics = (("/ingress/rgb",image,"optical"), ("/ingress/info",info,"optical"),
                  ("/ingress/lidar",points,"left"))
        with BoardSource(profile) as source:
            pubs = [source.node.create_publisher(type(msg), topic, qos_profile_sensor_data)
                    for topic, msg, _ in topics]
            deadline = time.monotonic()+5
            while not all(p.get_subscription_count() for p in pubs) and time.monotonic()<deadline:
                source.executor.spin_once(timeout_sec=.03)
            self.assertTrue(all(p.get_subscription_count() for p in pubs))
            for pub, (_, msg, frame) in zip(pubs, topics):
                msg.header.frame_id = frame
                msg.header.stamp.sec = 10
                pub.publish(msg)
            frame = source.next_frame(timeout_s=3)
            self.assertIsNotNone(frame)
            self.assertEqual(frame["frames"], {"camera":"optical", "left":"left"})
            np.testing.assert_array_equal(frame["clouds"]["left"][:,0],[.25,1.25,2.25,3.25])
            np.testing.assert_array_equal(frame["rgb"],expected)

    def test_mixed_livox_layout_respects_row_padding_field_order_and_float64_time(self):
        for big_endian in (False, True):
            msg = cloud(big_endian)
            original = bytes(msg.data)
            xyz, timestamps = decode_cloud(msg, "timestamp")
            np.testing.assert_array_equal(xyz[:,0], [.25,1.25,2.25,3.25])
            np.testing.assert_array_equal(xyz[:,1], [-2.,-1.,0.,1.])
            self.assertEqual(timestamps.dtype.itemsize, 8)
            np.testing.assert_array_equal(timestamps, [10.,10.008,10.012,10.02])
            cfg = {"field":"timestamp","mode":"absolute","unit":"s"}
            mid, selected = point_window(10_000_000_000, xyz, timestamps, cfg)
            self.assertEqual(mid, 10_010_000_000)
            np.testing.assert_array_equal(selected, xyz[1:3])
            self.assertEqual(bytes(msg.data), original)
            spatial_xyz, unused = decode_cloud(msg)
            np.testing.assert_array_equal(spatial_xyz, xyz)
            self.assertIsNone(unused)

    def test_invalid_layout_rejected_without_fabricated_points(self):
        for mutation in ("short", "stride", "duplicate", "overlap", "not_scalar", "unknown_time"):
            with self.subTest(mutation=mutation):
                msg = cloud()
                if mutation == "short":
                    msg.data = msg.data[:-1]
                elif mutation == "stride":
                    msg.row_step = 24
                elif mutation == "duplicate":
                    msg.fields.append(msg.fields[3])
                elif mutation == "overlap":
                    msg.fields[1].offset = 6
                elif mutation == "not_scalar":
                    msg.fields[1].count = 2
                with self.assertRaises(ValueError):
                    decode_cloud(msg, "other" if mutation == "unknown_time" else "timestamp")

    def test_coarse_epochs_rejected_and_uint32_relative_nanoseconds_work(self):
        cfg = {"mode":"relative","unit":"ns"}
        xyz = np.zeros((3,3))
        stamp, selected = point_window(1_700_000_000_000_000_000,xyz,
                                       np.array([100,8_000_100,16_000_100],dtype=np.uint32),cfg)
        self.assertEqual(stamp, 1_700_000_000_008_000_100)
        self.assertEqual(len(selected), 1)
        epoch = 1_700_000_000_000_000_000
        wire_time = np.asarray([epoch,epoch+8_000_000,epoch+16_000_000],dtype=np.float64)
        stamp, selected = point_window(epoch,xyz,wire_time,{"mode":"absolute","unit":"ns"})
        self.assertEqual(stamp,epoch+8_000_000)
        self.assertEqual(len(selected), 1)
        with self.assertRaisesRegex(ValueError, "precision"):
            point_window(stamp,xyz,np.array([1.7e9,1.7e9,1.7e9],dtype=np.float32),
                         {"mode":"absolute","unit":"s"})

    def test_rectified_stream_is_decoded_with_projection_matrix(self):
        image, info, expected = image_info()
        rgb, camera_k = decode_rgb(image,info,{"image_geometry":"rectified"})
        np.testing.assert_array_equal(rgb, expected)
        np.testing.assert_array_equal(np.reshape(camera_k,(3,3)),np.reshape(info.p,(3,4))[:,:3])

    def test_unrectified_stream_uses_raw_distortion_and_matching_k(self):
        image, info, raw = image_info()
        rgb, camera_k = decode_rgb(image,info,{"image_geometry":"raw"})
        k = np.reshape(info.k,(3,3))
        np.testing.assert_array_equal(np.reshape(camera_k,(3,3)),k)
        np.testing.assert_array_equal(rgb, cv2.undistort(raw,k,np.array(info.d),None,k))
        self.assertFalse(np.array_equal(rgb,raw))
        info.distortion_model = "equidistant"
        with self.assertRaisesRegex(ValueError, "distortion"):
            decode_rgb(image,info,{"image_geometry":"raw"})


if __name__ == "__main__":
    unittest.main()
