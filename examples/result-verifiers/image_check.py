"""Decode one P5 image from the operation-owned artifact store."""
import hashlib
import re

from artifact_verification import Counterevidence, Inconclusive


# The bounded P5 reader accepts ASCII comments between header tokens and a
# single whitespace byte before pixels. Pixel values, including newline, are data.
TOKEN = re.compile(rb"[0-9]{1,8}")


def inspect(name, data, _candidate):
    if not name.lower().endswith(".pgm"):
        raise Inconclusive("image.unsupported_format")
    if not data.startswith(b"P5"):
        raise Counterevidence("image.invalid_pgm")
    pos = 2
    values = []
    for _ in range(3):
        # Tokenize explicitly so long malformed headers cannot trigger regex backtracking.
        token_end = pos
        while pos < len(data):
            if data[pos] in b" \t\r\n\v\f":
                pos += 1
            elif data[pos] == 35:
                end = data.find(b"\n", pos + 1)
                if end < 0:
                    raise Counterevidence("image.invalid_pgm")
                pos = end + 1
            else:
                break
            if pos > 4096:
                raise Inconclusive("image.header_too_large")
        if pos == token_end:
            raise Counterevidence("image.invalid_pgm")
        match = TOKEN.match(data, pos)
        if not match:
            raise Counterevidence("image.invalid_pgm")
        values.append(int(match[0]))
        pos = match.end()
    width, height, maxval = values
    if not width or not height or not 1 <= maxval <= 65535 or pos >= len(data) or data[pos] not in b" \t\r\n\v\f":
        raise Counterevidence("image.invalid_pgm")
    if maxval > 255:
        raise Inconclusive("image.unsupported_bit_depth")
    pixels = memoryview(data)[pos+1:]
    if len(pixels) != width * height:
        raise Counterevidence("image.invalid_raster", expected_pixels=width*height, actual_bytes=len(pixels))
    brightest = max(pixels)
    if brightest > maxval:
        raise Counterevidence("image.invalid_raster", maxval=maxval, observed_max=brightest)
    return {"format":"P5", "width":width, "height":height, "pixels":len(pixels),
            "pixel_min":min(pixels), "pixel_max":brightest, "sha256":hashlib.sha256(data).hexdigest()}
