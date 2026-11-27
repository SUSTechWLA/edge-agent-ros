#!/bin/sh
# Compose the existing published-sensor site and its read-only Agent services.
set -eu
root=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
: "${EDGE_FIELD_SITE_ENV:?path to the approved sensing site env}"
: "${EDGE_FIELD_RELEASE_ENV:?path to the approved field release env}"
if [ "$#" -eq 0 ]; then set -- config --quiet; fi
exec docker compose --project-directory "$root" -p "${EDGE_FIELD_PROJECT:-edge-nav-spatial}" \
    --env-file "$EDGE_FIELD_SITE_ENV" --env-file "$EDGE_FIELD_RELEASE_ENV" --profile published \
    -f "$root/compose/navigation-readonly.yaml" -f "$root/compose/navigation-camera-info.yaml" \
    -f "$root/compose/navigation-camera-image.yaml" -f "$root/compose/navigation-camera-depth.yaml" \
    -f "$root/compose/navigation-livox.yaml" -f "$root/compose/stationary-perception.yaml" \
    -f "$root/compose/autonomous-read.yaml" -f "$root/compose/object-perception.yaml" \
    -f "$root/compose/frame-query.yaml" -f "$root/compose/perception-operators.yaml" \
    -f "$root/compose/perception-autostart.yaml" -f "$root/compose/field-plugin-author.yaml" "$@"
