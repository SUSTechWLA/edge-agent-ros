#!/bin/sh
# Optional recorder for a single task in an existing Edge Agent Docker project.
set -eu
# Git Bash on Windows must leave Docker's Linux container paths intact.
case "$(uname -s)" in MINGW*|MSYS*) export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*';; esac
cd "$(dirname "$0")/.."
usage() {
  echo 'Usage: sh scripts/record-task.sh sim|campus|hardware enable|drain|disable|start TASK [RID] [--laser-scan TOPIC] [--imu TOPIC] [--sensor-hz HZ] [--external-sensor imu|laser_scan]|status RID|stop RID|export RID NEW_DIR' >&2
  exit 2
}
[ "$#" -ge 2 ] || usage
mode=$1
action=$2
shift 2
case "$mode" in
  sim|campus)
    base=compose.yaml
    src=recorder-source
    url=http://task-recorder:8188
    agent=${EDGE_RECORD_AGENT_URL:-http://agent:8090}
    if [ "$mode" = campus ]; then
      export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-edge-agent-campus}
      export EDGE_RECORDING_PORT=${EDGE_RECORDING_PORT:-8189}
      hub=$(docker compose -f "$base" ps --status running -q agent)
      if [ -n "$hub" ]; then
        registry_volume=$(docker inspect --format '{{range .Mounts}}{{if eq .Destination "/etc/edge/plugins"}}{{if eq .Type "volume"}}{{.Name}}{{end}}{{end}}{{end}}' "$hub")
        [ -n "$registry_volume" ] || { echo 'Campus Agent requires its named plugin registry.' >&2; exit 2; }
        export EDGE_CAMPUS_RECORD_REGISTRY=$registry_volume
        export EDGE_RECORDER_IMAGE=${EDGE_RECORDER_IMAGE:-$(docker inspect --format '{{.Config.Image}}' "$hub")}
        domain=$(docker inspect --format '{{range .Config.Env}}{{if eq (index (split . "=") 0) "ROS_DOMAIN_ID"}}{{println .}}{{end}}{{end}}' "$hub")
        export EDGE_ROS_DOMAIN_ID=${EDGE_ROS_DOMAIN_ID:-${domain#ROS_DOMAIN_ID=}}
      elif [ "$action" = start ] || [ "$action" = enable ]; then
        echo 'Start the campus Agent before enabling recording for that project.' >&2; exit 2
      else
        export EDGE_CAMPUS_RECORD_REGISTRY=${EDGE_CAMPUS_RECORD_REGISTRY:-${COMPOSE_PROJECT_NAME}_campus-plugins}
      fi
    fi
    ;;
  hardware)
    base=compose.hardware.yaml
    src=recorder-hardware-source
    url=http://127.0.0.1:${EDGE_RECORDING_PORT:-8188}
    agent=${EDGE_RECORD_AGENT_URL:-http://127.0.0.1:8090}
    hardware_image=${EDGE_HARDWARE_IMAGE:-edge-agent-ros/hardware:v0.1}
    if [ -f ROBOT_SOURCE ] && [ -z "${EDGE_HARDWARE_IMAGE:-}" ]; then
      short=$(cut -c1-12 ROBOT_SOURCE)
      hardware_image="edge-agent-ros/hardware:robot-$short"
    fi
    export EDGE_RECORDER_IMAGE=${EDGE_RECORDER_IMAGE:-$hardware_image}
    ;;
  *) usage ;;
esac
dc() {
  if [ "$mode" = campus ]; then
    docker compose -f "$base" -f compose.task-recorder.yaml -f compose.task-recorder-campus.yaml "$@"
  else
    docker compose -f "$base" -f compose.task-recorder.yaml "$@"
  fi
}
cmd() { dc exec -T task-recorder python3 -B -m task_recorder.cli "$@"; }
admission() { dc exec -T task-recorder python3 -B -m task_recorder.manage admission "$1"; }
registry() { dc run --no-deps --rm -T recorder-admin python3 -B -m task_recorder.manage registry "$1" "$url"; }
ready() {
  dc up -d --no-deps --no-build task-recorder
  dc exec -T task-recorder python3 -B -c 'import time
from task_recorder.cli import endpoint, EndpointError
for i in range(40):
    try: endpoint("http://127.0.0.1:8188","/health"); break
    except EndpointError:
        if i==39: raise
        time.sleep(.2)'
}
recid() {
  case "$1" in ''|[!a-zA-Z0-9]*|*[!a-zA-Z0-9_.-]*) usage;; esac
  [ "${#1}" -le 96 ] || usage
}
case "$action" in
  enable)
    [ "$#" = 0 ] || usage
    ready
    registry enable
    admission enable ;;
  start)
    [ "$#" -ge 1 ] || usage
    task=$1
    shift
    rid=$task
    case "${1:-}" in ''|--*) ;; *) rid=$1; shift ;; esac
    recid "$rid"
    scan=${EDGE_RECORD_LASER_SCAN:-}
    imu=${EDGE_RECORD_IMU:-}
    sensor_hz=${EDGE_RECORD_SENSOR_HZ:-4}
    external_scan=
    external_imu=
    while [ "$#" -gt 0 ]; do
      [ "$#" -ge 2 ] && [ -n "$2" ] || usage
      case "$1" in
        --laser-scan) scan=$2;; --imu) imu=$2;; --sensor-hz) sensor_hz=$2;;
        --external-sensor) case "$2" in laser_scan) external_scan=1;; imu) external_imu=1;; *) usage;; esac ;;
        *) usage;;
      esac
      shift 2
    done
    signals=
    if [ -n "$scan$imu" ]; then
      [ "$mode" != hardware ] || { echo 'Use --external-sensor for summaries from the deployed physical collector.' >&2; exit 2; }
      [ -z "$scan" ] || [ -z "$external_scan" ] || usage
      [ -z "$imu" ] || [ -z "$external_imu" ] || usage
      for topic in "$scan" "$imu"; do
        case "$topic" in *[!A-Za-z0-9_/]*) usage;; esac
        [ -z "$topic" ] || printf '%s\n' "$topic" | grep -Eq '^(/[A-Za-z_][A-Za-z0-9_]*)+$' || usage
      done
      case "$sensor_hz" in ''|*[!0-9.]*) usage;; esac
      awk -v hz="$sensor_hz" 'BEGIN { exit !(hz ~ /^[0-9]+([.][0-9]+)?$/ && hz >= 0.1 && hz <= 10) }' || usage
      signals="$scan|$imu|$sensor_hz"
    fi
    set -- agent_state
    if [ -n "$external_scan" ]; then set -- "$@" laser_scan; fi
    if [ -n "$external_imu" ]; then set -- "$@" imu; fi
    case "${EDGE_RECORD_AGENT_TRACE:-1}" in 1) set -- "$@" agent_trace;; 0) ;; *) usage;; esac
    case "${EDGE_RECORD_REQUEST_TEXT:-0}" in 1) set -- "$@" agent_request;; 0) ;; *) usage;; esac
    case "${EDGE_RECORD_SOFTWARE_ONLY:-0}" in
      1) [ -z "${EDGE_RECORD_CAMERA:-}$signals" ] || usage ;;
      0)
        set -- "$@" clock map fused_pose twist bus
        if [ -n "${EDGE_RECORD_CAMERA:-}" ]; then set -- "$@" rgb; fi
        if [ -n "$scan" ]; then set -- "$@" laser_scan; fi
        if [ -n "$imu" ]; then set -- "$@" imu; fi ;;
      *) usage ;;
    esac
    if [ -n "$(dc ps --all --status exited -q task-recorder)" ]; then
      echo 'The recorder is disabled. Enable it explicitly to admit new recordings.' >&2
      exit 2
    fi
    ready
    gate=$(admission state)
    case "$gate" in *'"draining": false'*) registry ensure;; esac
    status=$(cmd start "$rid" --task-id "$task" --sources "$@")
    printf '%s\n' "$status"
    case "$status" in *'"status": "ACTIVE"'*) ;; *) exit 0;; esac
    cid=$(dc ps -q task-recorder)
    project=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$cid")
    name="edge-rec-$project-$rid"
    set -- python3 -B -m task_recorder.collect "$task" "$rid" --url "$url" --agent "$agent" --timeout "${EDGE_RECORD_WATCH_TIMEOUT:-86400}"
    if [ "${EDGE_RECORD_SOFTWARE_ONLY:-0}" = 1 ]; then set -- "$@" --software; fi
    if [ "$mode" != hardware ]; then set -- "$@" --sim; fi
    if [ -n "${EDGE_RECORD_CAMERA:-}" ]; then set -- "$@" --camera "$EDGE_RECORD_CAMERA"; fi
    if [ -n "$scan" ]; then set -- "$@" --laser-scan "$scan"; fi
    if [ -n "$imu" ]; then set -- "$@" --imu "$imu"; fi
    if [ -n "$signals" ]; then set -- "$@" --sensor-hz "$sensor_hz"; fi
    # An identical ACTIVE recording may already own its collector. Only accept
    # containers from this exact Compose project and the requested service.
    owns() {
      [ "$(docker inspect --format '{{.State.Running}} {{index .Config.Labels "com.docker.compose.project"}} {{index .Config.Labels "com.docker.compose.service"}} {{index .Config.Labels "edge.recorder.camera"}}|{{index .Config.Labels "edge.recorder.signals"}}' "$name" 2>/dev/null || true)" = "true $project $src ${EDGE_RECORD_CAMERA:-}|$signals" ]
    }
    if ! owns; then
      dc run -d --rm --no-deps --label "edge.recorder.camera=${EDGE_RECORD_CAMERA:-}" --label "edge.recorder.signals=$signals" --name "$name" "$src" "$@" || owns || {
        echo "Collector launch failed. Inspect recording $rid and retry start or explicitly stop it." >&2
        exit 1
      }
    fi ;;
  status|stop)
    [ "$#" = 1 ] || usage
    recid "$1"
    cmd "$action" "$1" ;;
  export)
    [ "$#" = 2 ] || usage
    recid "$1"
    [ ! -e "$2" ] || { echo 'Choose a new export directory.' >&2; exit 2; }
    cmd export "$1"
    cid=$(dc ps -q task-recorder)
    mkdir -p "$2"
    docker cp "$cid:/recordings/$1/." "$2" ;;
  drain|disable)
    [ "$#" = 0 ] || usage
    if [ -z "$(dc ps -q task-recorder)" ]; then
      if [ "$action" = disable ]; then registry remove; fi
      exit 0
    fi
    admission drain
    registry drain
    if [ "$action" = disable ]; then
      admission empty
      registry remove
      dc stop task-recorder
    fi ;;
  *) usage ;;
esac
