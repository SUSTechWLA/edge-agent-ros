"""Portable metric checks shared by the operator station and the four-hole solver."""
from math import hypot, isfinite


def four_hole_lengths(geometry):
    try:
        side = geometry["marker_size_m"]
        width,height = geometry["marker_spacing_m"]
        dx,dy = geometry["hole_spacing_m"]
        diameter = geometry["hole_diameter_m"]
        values = (side,width,height,dx,dy,diameter)
        if not all(type(value) in (int,float) and isfinite(value) for value in values):
            raise ValueError
        if not (.015 <= side < min(width,height) and .04 <= diameter < min(dx,dy)
                and dx < width <= 4 and dy < height <= 4):
            raise ValueError
        if hypot(max(0,(width-dx-side)/2),max(0,(height-dy-side)/2)) <= diameter/2:
            raise ValueError
        return values
    except (ValueError,TypeError,KeyError) as exc:
        raise ValueError("invalid_four_hole_board_geometry") from exc
