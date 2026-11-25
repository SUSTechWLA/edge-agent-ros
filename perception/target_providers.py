"""Image recognizers; geometry and depth consumers can reuse their 2-D detections."""
from functools import lru_cache


def campus_sign(target, rgb, k, context, settings):
    from perception.color_marker import marker_pixels
    match = marker_pixels(rgb)
    if match is None:
        return []
    return [dict(label=target, bbox_xyxy=match[2])]


@lru_cache(maxsize=2)
def _model(weights, digest):
    from perception.spatial_facts.object_models import load_yolo
    return load_yolo(weights, digest)


def yolo(target, rgb, k, context, settings):
    """Adapter for a site-installed and hash-pinned object model."""
    import cv2
    ok, png = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise ValueError("cannot_encode_rgb_observation")
    prediction = _model(settings["weights"],settings["digest"])(png.tobytes())
    return [row for row in prediction["detections"] if row["label"] == target]
