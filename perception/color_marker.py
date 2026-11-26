"""Pixel support of the magenta fiducial used by the campus sign detector."""
import numpy as np


def marker_pixels(rgb):
    red, green, blue = np.moveaxis(rgb.astype("int16"), 2, 0)
    mask = ((red >= 75) & (blue >= 55) & (red-green >= 38) & (blue-green >= 30) &
            (red*2 >= blue) & (blue*2 >= red))
    v, u = np.nonzero(mask)
    if len(u) < 30:
        return None
    x0, x1, y0, y1 = int(u.min()), int(u.max())+1, int(v.min()), int(v.max())+1
    width, height = x1-x0, y1-y0
    if width < 5 or height < 4 or not .38 <= width/height <= 5 or len(u) < .36 * width * height:
        return None
    return v, u, [x0, y0, x1, y1]
