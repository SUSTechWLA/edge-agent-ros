#!/bin/sh
# Invoke from scripts/robot.sh so the deployment image and ROBOT_SOURCE have been checked.
set -eu
cd "$(dirname "$0")/.."
action=${1:-status}
: "${EDGE_HARDWARE_IMAGE:?run sh scripts/robot.sh plugins}"
image=$(docker image inspect --format '{{.Id}}' "$EDGE_HARDWARE_IMAGE")
platform=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' "$EDGE_HARDWARE_IMAGE")
manifest=$(docker image inspect --platform "$platform" --format '{{.Descriptor.digest}}' "$image")
case "$action" in
  camera-*)
    : "${EDGE_SOFT_SITE:?provide the resolved profile which includes the camera pack}"
    : "${EDGE_PACK_LOCK:?provide the independently recorded pack fingerprint}"
    export EDGE_PHYSICAL_SITE="$EDGE_SOFT_SITE/physical"
    export EDGE_PHYSICAL_IMAGE=$image EDGE_PHYSICAL_PLATFORM_DIGEST=$manifest
    site=$EDGE_SOFT_SITE; track=physical; lock=$EDGE_PACK_LOCK ;;
  *)
    : "${EDGE_SOFT_SITE:?provide the prepared and locked site directory}"
    : "${EDGE_PACK_LOCK:?provide the independently recorded pack fingerprint}"
    export EDGE_SOFT_IMAGE=$image EDGE_SOFT_PLATFORM_DIGEST=$manifest
    site=$EDGE_SOFT_SITE; track=soft; lock=$EDGE_PACK_LOCK ;;
esac
case "$site" in /*) ;; *) echo "site directory must be absolute" >&2; exit 2 ;; esac
export EDGE_SOFT_IMAGE=$image EDGE_SOFT_PLATFORM_DIGEST=$manifest
config() {
  runner=$1
  shift
  if [ "$action" = camera-start ]; then
    "$runner" -f compose.hardware.yaml -f compose.soft-runtime.yaml -f compose.soft-runtime-hardware.yaml -f compose.physical-camera.yaml "$@"
  elif [ "$action" = start ]; then
    "$runner" -f compose.hardware.yaml -f compose.soft-runtime.yaml -f compose.soft-runtime-hardware.yaml "$@"
  elif [ "$track" = physical ]; then
    "$runner" -f compose.hardware.yaml -f compose.physical-camera.yaml "$@"
  else
    "$runner" -f compose.hardware.yaml -f compose.soft-runtime.yaml -f compose.soft-runtime-hardware.yaml -f compose.hardware-observe.yaml "$@"
  fi
}
docker_compose() { docker compose "$@"; }
compose() { config docker_compose "$@"; }
check_config() { python3 scripts/check_board_plugins.py "$@"; }
guard() {
  config check_config --track "$track" --source "$EDGE_REVISION" \
    --site "$site" --image "$EDGE_HARDWARE_IMAGE" --lock "$lock" "$@"
}
verify() {
  subcommand=$1
  shift
  compose run --rm -T --no-deps --entrypoint python3 soft-admin -B /opt/edge/agent/site_pack.py \
    "$subcommand" --site /site --expect-lock "$EDGE_PACK_LOCK" "$@"
}
case "$action" in
  check) guard; verify verify ;;
  bench)
    if [ -n "$(compose ps -q core agent)" ]; then
      echo "Core or Agent is running in this project; finish its explicit handoff first." >&2
      exit 1
    fi
    guard
    verify activate --root /registry
    compose up -d --no-build perception console soft-facts soft-gateway soft-host evidence session
    guard --running
    compose ps --services --status running ;;
  camera-check) guard ;;
  start|camera-start)
    if [ -n "$(compose ps -q agent core)" ]; then
      echo "An Agent or Core already owns this project; perform its existing task handoff first." >&2; exit 1
    fi
    if [ "$action" = camera-start ]; then
      guard --running
      controller=$(compose ps -q physical-controller)
      compose run --rm -T --no-deps physical-admin status | python3 -c '
import json,sys
r=json.load(sys.stdin)
if r["phase"]!="ACTIVE" or any(not op["settled"] for op in r["operations"]): sys.exit("camera has outstanding handoff/operations")'
    fi
    track=soft
    guard --running
    verify activate --root /registry
    compose exec -T console python3 -c '
from pathlib import Path
import sqlite3,sys
p=Path("/var/lib/edge-tasks/tasks.db")
if p.exists():
    db=sqlite3.connect(p.as_uri()+"?mode=ro",uri=True)
    unfinished=db.execute("SELECT count(*) FROM tasks WHERE state NOT IN (?,?,?)",("BLOCKED","SUCCEEDED","CANCELED")).fetchone()[0]
    if unfinished: sys.exit("reconcile existing tasks before switching profile")'
    # Reopen the Console task API on explicit operator request. The control services, drivers
    # and physical provider processes are outside this command's service list.
    compose up -d --no-build --no-deps console agent soft-facts
    guard --running
    if [ "$action" = camera-start ]; then
      track=physical
      guard --running
      test "$controller" = "$(compose ps -q physical-controller)"
    fi ;;
  camera-status|camera-drain)
    compose run --rm -T --no-deps physical-admin "${action#camera-}" ;;
  camera-stop)
    ident=$(compose ps -a -q physical-camera)
    test -n "$ident"
    # The ledger rejects release until an independent verifier has observed stop.
    compose run --rm -T --no-deps physical-admin release
    compose stop physical-camera
    proof=$(mktemp)
    trap 'rm -f "$proof"' 0
    docker container inspect "$ident" | python3 -c '
import json,sys
r,=json.load(sys.stdin)
print(json.dumps({"Id":r["Id"],"Image":r["Image"],"ImageManifestDescriptor":r.get("ImageManifestDescriptor"),
    "Config":{k:r["Config"].get(k) for k in ("Hostname","Entrypoint","Cmd")},
    "State":{k:r["State"][k] for k in ("Status","Running")}}))' > "$proof"
    compose run --rm -T --no-deps -v "$proof:/exit-proof.json:ro" physical-admin process-stopped --inspection-file /exit-proof.json
    compose stop physical-verifier physical-controller ;;
  camera-bench)
    guard
    if [ -n "$(compose ps -q core agent physical-controller physical-camera physical-verifier)" ]; then
      echo "An existing deployment must complete its handoff first." >&2; exit 1
    fi
    compose run --rm -T --no-deps physical-admin install
    compose up -d --no-build physical-controller physical-camera physical-verifier
    guard --running
    compose run --rm -T --no-deps physical-admin status ;;
  status) compose ps ;;
  *) echo 'Usage: sh scripts/robot.sh plugins [check|bench|start|status|camera-check|camera-bench|camera-start|camera-status|camera-drain|camera-stop]' >&2; exit 2 ;;
esac
