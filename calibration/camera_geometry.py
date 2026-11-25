"""Decode a camera stream to the pinhole geometry consumed by the board detector."""
import numpy as np


def decode_rgb(image, info, camera):
    if (image.encoding not in ("rgb8", "bgr8") or (image.width, image.height) != (info.width, info.height)
            or not 0 < image.width * image.height <= 8_500_000 or image.step < image.width * 3
            or len(image.data) != image.step * image.height):
        raise ValueError("invalid RGB image or CameraInfo dimensions")
    pixels = np.ndarray((image.height, image.width, 3), np.uint8, buffer=image.data,
                        strides=(image.step, 3, 1))
    if image.encoding == "bgr8":
        pixels = pixels[:, :, ::-1]
    mode = camera.get("image_geometry", "pinhole")
    d = np.asarray(info.d, dtype=float)
    if not np.isfinite(d).all():
        raise ValueError("invalid camera distortion")
    k = np.asarray(info.k, dtype=float).reshape(3, 3)
    if mode == "rectified":
        k = np.asarray(info.p, dtype=float).reshape(3, 4)[:, :3]
    if (not np.isfinite(k).all() or min(k[0, 0], k[1, 1]) <= 0
            or not np.allclose(k[2], [0, 0, 1])):
        raise ValueError("invalid camera intrinsics")
    if mode == "raw":
        if info.distortion_model not in ("plumb_bob", "rational_polynomial") or len(d) not in (4, 5, 8):
            raise ValueError("unsupported raw camera distortion model")
        if np.any(d):
            import cv2
            pixels = cv2.undistort(pixels, k, d, None, k)
    elif mode != "rectified" and (mode != "pinhole" or np.any(np.abs(d) > 1e-8)):
        raise ValueError("declare raw or rectified camera geometry")
    return np.ascontiguousarray(pixels), k.flatten().tolist()
