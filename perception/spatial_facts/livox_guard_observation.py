"""One-shot native MID-360 observation for an installed RearGuard. No ROS or actuator I/O.

Capture emits a bounded window of original ingress packets. Report runs on the station
with the installed vehicle config and implementation, and never issues a motion permit.
"""
import argparse
import base64
from collections import Counter
import hashlib
from ipaddress import IPv4Address
import json
from pathlib import Path
import socket
import sys
import time

from perception.spatial_facts.livox_capture import (
    CARTESIAN_MM, LIVOX_HEADER, checked_header, open_receiver, receive, release_process_capabilities,
)


def capture(interface, destination, source):
    packets = []
    previous = None
    deadline = time.monotonic() + 3
    with open_receiver(interface, destination, [source]) as rx:
        release_process_capabilities()
        while time.monotonic() < deadline:
            try:
                observed = receive(rx, destination.packed, {source.packed})
            except socket.timeout:
                continue
            if observed is None:
                continue
            _, received, raw = observed
            header, error = checked_header(raw)
            if error:
                packets.clear()
                previous = None
                continue
            clock, _, start, seq, frame = header
            if previous:
                old_clock, old_start, old_seq, old_frame, old_rx = previous
                sequential = seq == (old_seq + 1) % 65536 or (seq == 0 and frame == (old_frame + 1) % 256)
                if (not sequential or clock != old_clock or not 0 < start-old_start <= 5_000_000
                        or not 0 <= received-old_rx <= 5_000_000):
                    packets.clear()
            previous = clock, start, seq, frame, received
            packets.append((received, base64.b64encode(raw).decode('ascii')))
            if len(packets) >= 256:
                raise ValueError('packet_rate_exceeds_bound')
            if received-packets[0][0] >= 50_000_000:
                return {'source_ip':str(source), 'destination_ip':str(destination), 'packets':packets}
    raise ValueError('no_contiguous_window')


def report(config_path):
    import numpy as np
    from dog_front_servo.config import load_config
    from dog_front_servo.rear_guard import RearGuard
    from dog_front_servo import rear_guard

    data = sys.stdin.buffer.read(700001)
    if len(data) > 700000:
        raise ValueError('packet_window_too_large')
    window = json.loads(data)
    cfg = load_config(str(config_path))['rear_guard']
    if (cfg['livox_tag_policy'] != 'mid360_high_confidence' or cfg['message_type'] != 'livox_custom'
            or cfg['topic'].split('/')[-1] != 'lidar_'+str(IPv4Address(window['source_ip'])).replace('.', '_')):
        raise ValueError('packet_source_or_tag_policy_does_not_match_installed_guard')
    packets = window['packets']
    if not 1 <= len(packets) < 256:
        raise ValueError('invalid_packet_count')
    xyz, tags, times, clocks, crcs = [], Counter(), [], set(), Counter()
    dtype = np.dtype({'names':['xyz','tag'],'formats':[('<i4',(3,)),'u1'],
                     'offsets':[0,13],'itemsize':CARTESIAN_MM.size})
    for received, encoded in packets:
        raw = base64.b64decode(encoded,validate=True)
        h, error = checked_header(raw)
        if error or any(raw[12:24]):
            raise ValueError(error or 'unsupported_packet_info')
        clock, crc, stamp, _, _ = h
        _, _, interval, count, *_ = LIVOX_HEADER.unpack_from(raw)
        if count != 96 or not interval:
            raise ValueError('invalid_intra_packet_timing')
        # MID-360 data_type 1. The timestamp is point 0; time_interval spans points 0..N-1.
        offsets = (np.arange(count, dtype=np.int64) * int(interval) * 100 + (count-1)//2) // (count-1)
        times.append(stamp + offsets)
        a = np.frombuffer(raw,dtype=dtype,count=count,offset=LIVOX_HEADER.size)
        tags.update(map(int,a['tag']))
        keep = ((a['tag'] & 0x3f) == 0) & a['xyz'].any(axis=1)
        xyz.append(a['xyz'][keep].astype(np.float64)*.001)
        clocks.add(clock)
        crcs['verified' if crc else 'device_field_zero']+=1
    acquisition_ns = np.concatenate(times)
    if len(clocks)!=1 or np.any(np.diff(acquisition_ns)<=0):
        raise ValueError('inconsistent_device_clock')
    points=np.vstack(xyz)
    if not 0 < len(points) <= cfg['max_points']:
        raise ValueError('invalid_filtered_point_count')
    first, last=map(int,(acquisition_ns[0],acquisition_ns[-1]))
    if not 0 < last-first <= 200_000_000:
        raise ValueError('invalid_device_window')
    # Pure in-memory instance. Never load the driver, follower, or command arbiter.
    guard=RearGuard({**cfg,'enabled':True})
    guard.add((first+(last-first)//2)/1e9,points)
    at_scan=guard.status(last/1e9)
    now=time.time_ns()
    at_host=guard.status(now/1e9)
    x,y,z=guard.cloud[1].T
    bumper=-x-cfg['rear_extent_m']
    band=(bumper>0)&(np.abs(y)<=cfg['half_width_m'])
    low=band&(bumper<=2)&(z<cfg['min_height_m'])
    recv_age=(now-packets[-1][0])/1e9
    guard_file=Path(rear_guard.__file__)
    return dict(kind='rear_native_guard_observation',motion_authorized=False,
                robot_guard_enabled_in_config=cfg['enabled'],source_ip=window['source_ip'],
                implementation_sha256=hashlib.sha256(guard_file.read_bytes()).hexdigest(),
                config_sha256=hashlib.sha256(config_path.read_bytes()).hexdigest(),packets=len(packets),
                native_point_count=len(acquisition_ns),native_tag_histogram=dict(tags),
                retained_high_confidence_nonzero=len(points),crc_counts=dict(crcs),
                device_time_type=clocks.pop(),device_first_point_ns=str(first),device_last_point_ns=str(last),
                first_packet_point_offset_examples_ns=[int(times[0][i]-first) for i in (0,1,2,-1)],
                kernel_first_receive_ns=str(packets[0][0]),kernel_last_receive_ns=str(packets[-1][0]),
                observed_host_unix_ns=str(now),last_receive_age_s=recv_age,
                kernel_receive_fresh=0<=recv_age<=cfg['max_age_s'],max_age_s=cfg['max_age_s'],
                clock_alignment_independently_verified=False,guard_at_host_now_reason=at_host['reason'],
                scan_geometry_reason=at_scan['reason'],scan_clearance_m=at_scan['clearance_m'],
                configured_stop_clearance_m=cfg['stop_clearance_m'],under_height_band_returns_within_2m=int(low.sum()),
                scope='Read-only scan geometry. Device clock and near-ground coverage are not verified for vehicle control.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='cmd',required=True)
    raw=commands.add_parser('capture')
    raw.add_argument('--interface',required=True)
    raw.add_argument('--destination',type=IPv4Address,required=True)
    raw.add_argument('--source',type=IPv4Address,required=True)
    check=commands.add_parser('report')
    check.add_argument('--config',type=Path,required=True)
    args=parser.parse_args()
    try:
        result=capture(args.interface,args.destination,args.source) if args.cmd=='capture' else report(args.config)
        print(json.dumps(result,separators=(',',':'),allow_nan=False))
    except (OSError,ValueError,TypeError,KeyError,ImportError) as exc:
        print(json.dumps({'motion_authorized':False,'error':str(exc)}),file=sys.stderr)
        raise SystemExit(1)


if __name__=='__main__':
    main()
