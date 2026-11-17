#!/bin/sh
# Run from any directory with POSIX sh and Docker Compose v2.
set -eu
cd "$(dirname "$0")/.."
mode=${1:-sim}
action=${2:-up}
if [ "$#" -gt 0 ]; then shift; fi
if [ "$#" -gt 0 ]; then shift; fi
case "$mode" in
  sim) config=compose.yaml; console_port=8080 ;;
  hardware) config=compose.hardware.yaml; console_port=${EDGE_CONSOLE_PORT:-8765} ;;
  *) echo 'Usage: sh scripts/deploy.sh sim|hardware [up|start|build|status|logs|stop|down] [services ...]' >&2; exit 2 ;;
esac
compose() { docker compose -f "$config" "$@"; }
build() {
  if [ -z "${EDGE_REVISION:-}" ] && git rev-parse --verify HEAD >/dev/null 2>&1; then
    EDGE_REVISION=$(git rev-parse HEAD)
    if [ -n "$(git status --porcelain)" ]; then EDGE_REVISION="$EDGE_REVISION-dirty"; fi
    export EDGE_REVISION
  fi
  if [ "$mode" = sim ]; then
    base=${EDGE_BASE_IMAGE:-edge-agent-ros/base:jazzy-harmonic}
    if [ "$base" = edge-agent-ros/base:jazzy-harmonic ] && ! docker image inspect "$base" >/dev/null 2>&1; then
      docker build -f docker/Dockerfile.base -t "$base" \
        --build-arg "UBUNTU_MIRROR=${UBUNTU_MIRROR:-}" --build-arg "ROS_MIRROR=${ROS_MIRROR:-}" .
    fi
    compose build sim
  else
    compose build core
  fi
}
case "$action" in
  build) build; exit ;;
  status) compose ps "$@"; exit ;;
  logs) compose logs --tail 100 "$@"; exit ;;
  stop) compose stop "$@"; exit ;;
  down) compose down; exit ;;
  up) build ;;
  start) : ;; # Start an image already built or imported with docker load.
  *) echo "Unknown action: $action" >&2; exit 2 ;;
esac
full_sim=no
if [ "$#" -eq 0 ]; then
  if [ "$mode" = sim ]; then
    full_sim=yes
    set -- sim bridge witness gateway core perception scene agent console
  else set -- core perception agent console; fi
fi
if [ "$mode" = sim ]; then
  case " $* " in
    *" agent "*)
      if compose config --environment | grep -Eq '^EDGE_OLLAMA_URL=http://model:11434/?$'; then
        compose --profile model up -d model
      fi ;;
  esac
fi
compose up -d --no-build "$@"
if [ "$full_sim" = yes ]; then
  compose exec -T core /usr/local/bin/edge-entrypoint python3 /opt/edge/client/ready.py
fi
case " $* " in
  *" console "*)
    # No host Python/curl needed. Waiting for HTTP is independent of sensors
    # which may be absent on a new board or a read-only diagnostic deployment.
    compose exec -T console python3 - "$console_port" <<'PY'
import json, sys, time, urllib.request
url = f"http://127.0.0.1:{sys.argv[1]}/api/state"
for attempt in range(30):
    try:
        with urllib.request.urlopen(url, timeout=1) as response:
            state = json.load(response)
        print("Console ready:", state.get("robot_id", "edge"))
        break
    except (OSError, ValueError):
        if attempt == 29:
            raise
        time.sleep(1)
PY
    printf 'Console: http://127.0.0.1:%s/\n' "${EDGE_CONSOLE_PORT:-8765}"
    ;;
esac
compose ps "$@"
