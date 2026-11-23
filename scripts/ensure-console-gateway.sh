#!/bin/sh
# Run on network readiness after Docker has attempted to restore the published console.
set -eu
container=${1:?expected existing console container}
network=${2:?expected existing Docker network}
url=${3:?expected login URL}
case "$url" in http://*/login) ;; *) exit 2 ;; esac
if curl --noproxy '*' --fail --silent --max-time 2 "$url" >/dev/null; then exit 0; fi
actual=$(docker inspect --format '{{.HostConfig.NetworkMode}}' "$container")
[ "$actual" = "$network" ]
[ "$(docker inspect --format '{{.HostConfig.Privileged}}' "$container")" = false ]
if [ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]; then
  docker start "$container" >/dev/null
fi
# Docker can leave a container running without its failed network endpoint when the LAN IP was absent at boot.
if [ "$(docker inspect --format '{{len .NetworkSettings.Networks}}' "$container")" = 0 ]; then
  docker network connect "$network" "$container"
fi
curl --noproxy '*' --fail --silent --retry 2 --retry-connrefused --max-time 3 "$url" >/dev/null
