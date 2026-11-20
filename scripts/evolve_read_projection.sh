#!/usr/bin/env bash
# Run a single site-authorized, read-only projection for an existing Agent task.
set -euo pipefail
exec python3 -B "$(dirname -- "$0")/evolve_read_projection.py" "$@"
