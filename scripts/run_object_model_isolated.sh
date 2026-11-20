#!/usr/bin/env bash
# Run the CPU model as a site user with a local Unix socket as its only input/output.
set -euo pipefail
if [ "$#" -ne 5 ]; then
  echo "usage: $0 SOURCE_ROOT PYTHON_SITE_PACKAGES WEIGHTS_FILE EXPECTED_SHA256 SOCKET_DIR" >&2
  exit 2
fi
source_root=$(realpath -e -- "$1")
packages=$(realpath -e -- "$2")
weights=$(realpath -e -- "$3")
expected=$4
socket_dir=$(realpath -e -- "$5")
[[ -d "$packages" && -f "$weights" && -d "$socket_dir" && -O "$socket_dir" ]]
[[ "$expected" =~ ^sha256:[0-9a-f]{64}$ ]]
test -s "$source_root/perception/spatial_facts/object_worker.py"
exec bwrap --unshare-all --die-with-parent --new-session --clearenv \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /etc/ld.so.cache /etc/ld.so.cache \
  --ro-bind-try /etc/alternatives /etc/alternatives \
  --dir /sys/devices/system --ro-bind /sys/devices/system/cpu /sys/devices/system/cpu \
  --proc /proc --dev /dev --tmpfs /tmp --dir /tmp/yolo --dir /app \
  --ro-bind "$source_root/perception" /app/perception --ro-bind "$source_root/edge_contracts" /app/edge_contracts \
  --ro-bind "$packages" /packages --ro-bind "$weights" /model/detector.pt --bind "$socket_dir" /ipc \
  --setenv PATH /usr/bin:/bin --setenv PYTHONPATH /app:/packages --setenv HOME /tmp \
  --setenv LANG C.UTF-8 --setenv YOLO_CONFIG_DIR /tmp/yolo --setenv CUDA_VISIBLE_DEVICES "" \
  --setenv OMP_NUM_THREADS 1 --setenv OPENBLAS_NUM_THREADS 1 --setenv MKL_NUM_THREADS 1 --chdir /tmp \
  /usr/bin/python3 -B -m perception.spatial_facts.object_worker \
  --weights /model/detector.pt --expected-digest "$expected" --socket /ipc/object.sock
