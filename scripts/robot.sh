#!/bin/sh
# Single-board Docker entrypoint. Site configuration and evidence stay outside the checkout.
set -eu
cd "$(dirname "$0")/.."
action=${1:-observe}
if [ "$#" -gt 0 ]; then shift; fi
if [ -f ROBOT_SOURCE ]; then
  EDGE_REVISION=$(cat ROBOT_SOURCE)
else
  EDGE_REVISION=${EDGE_REVISION:-$(git rev-parse HEAD)}
fi
export EDGE_REVISION
short=$(printf '%.12s' "$EDGE_REVISION")
export EDGE_HARDWARE_IMAGE=${EDGE_HARDWARE_IMAGE:-edge-agent-ros/hardware:robot-$short}
check_image() {
  actual=$(docker image inspect "$EDGE_HARDWARE_IMAGE" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
  if [ "$actual" != "$EDGE_REVISION" ]; then
    echo "Image/source revision mismatch ($actual vs $EDGE_REVISION). Build this release first." >&2
    exit 2
  fi
}
case "$action" in
  build) exec sh scripts/deploy.sh hardware build ;;
  observe)
    if ! docker image inspect "$EDGE_HARDWARE_IMAGE" >/dev/null 2>&1; then
      sh scripts/deploy.sh hardware build
    fi
    check_image
    exec sh scripts/deploy.sh hardware observe ;;
  preflight|status|logs) exec sh scripts/deploy.sh hardware "$action" "$@" ;;
  start)
    check_image
    exec sh scripts/deploy.sh hardware start ;;
  stop) exec sh scripts/deploy.sh hardware stop ;;
  factory|calibration-status|fit-targets)
    : "${EDGE_ROBOT_ID:?set EDGE_ROBOT_ID to this robot}"
    image=$EDGE_HARDWARE_IMAGE
    check_image
    case "$EDGE_ROBOT_ID" in *[!a-zA-Z0-9_-]*|'') echo 'Invalid EDGE_ROBOT_ID' >&2; exit 2;; esac
    case "$action" in
      calibration-status)
        exec docker run --rm --network none --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp \
          -e PYTHONDONTWRITEBYTECODE=1 --mount "type=volume,src=edge-factory-$EDGE_ROBOT_ID,dst=/factory,readonly" \
          "$image" python3 -m calibration.factory status --state /factory --environment hardware --robot-id "$EDGE_ROBOT_ID" ;;
    esac
    : "${EDGE_FACTORY_DIR:?set EDGE_FACTORY_DIR to the station input directory}"
    station=$(cd "$EDGE_FACTORY_DIR" && pwd -P)
    case "$action" in
      factory) set -- python3 -m calibration.ros_capture --profile /station/profile.json --identity /station/bindings.json --state /factory --robot-id "$EDGE_ROBOT_ID" "$@" ;;
      fit-targets) set -- python3 -m calibration.targets /station/targets.json "$@" ;;
    esac
    exec docker run --rm --init --network host --read-only --cap-drop ALL --security-opt no-new-privileges --tmpfs /tmp \
      -e ROS_DOMAIN_ID="${EDGE_ROS_DOMAIN_ID:-72}" -e RMW_IMPLEMENTATION="${EDGE_RMW:-rmw_cyclonedds_cpp}" \
      -e ROS_LOG_DIR=/tmp/ros-logs -e PYTHONDONTWRITEBYTECODE=1 \
      --mount "type=bind,src=$station,dst=/station,readonly" --mount "type=volume,src=edge-factory-$EDGE_ROBOT_ID,dst=/factory" \
      "$image" "$@" ;;
  *) echo 'Usage: sh scripts/robot.sh [build|observe|preflight|status|logs|fit-targets|factory|calibration-status|start|stop]' >&2; exit 2 ;;
esac
