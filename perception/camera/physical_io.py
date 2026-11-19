"""Local role-socket protocol and durable immutable capture files for the physical camera."""
import hashlib
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agent"))
from soft_client import SoftError, request_at


def frame_bytes(msg):
    """CDR little-endian sensor_msgs/Image with initialized alignment bytes for reproducible original-frame hashes."""
    body = bytearray()

    def aligned(fmt, value):
        body.extend(bytes((-len(body)) % 4))
        body.extend(struct.pack("<" + fmt,value))

    def string(text):
        encoded = text.encode("utf-8") + b"\0"
        aligned("I",len(encoded))
        body.extend(encoded)

    aligned("i",msg.header.stamp.sec)
    aligned("I",msg.header.stamp.nanosec)
    string(msg.header.frame_id)
    aligned("I",msg.height)
    aligned("I",msg.width)
    string(msg.encoding)
    body.append(int(msg.is_bigendian))
    aligned("I",msg.step)
    aligned("I",len(msg.data))
    body.extend(msg.data)
    return b"\0\1\0\0" + body


class ControlError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def request(role, action, **fields):
    fields.setdefault("instance_id",socket.gethostname())
    try:
        return request_at(f"/sockets/{role}/physical.sock",action,timeout=2,**fields)
    except SoftError as error:
        raise ControlError(error.code) from error


def artifact_ref(operation, epoch):
    if type(epoch) is not int or epoch < 1:
        raise ValueError("invalid fence")
    return f"frames/{hashlib.sha256(operation.encode()).hexdigest()}-{epoch}.cdr"


def write_once(path, raw):
    """A successful return covers data and directory durability; another attempt cannot replace it."""
    directory = Path(path).parent
    directory.mkdir(exist_ok=True,parents=True)
    with tempfile.NamedTemporaryFile(dir=directory,delete=False) as output:
        staging = Path(output.name)
        try:
            os.fchmod(output.fileno(),0o640)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
    try:
        os.link(staging,path)
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        staging.unlink(missing_ok=True)
