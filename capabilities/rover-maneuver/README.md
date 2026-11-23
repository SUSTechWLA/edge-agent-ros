# Rover maneuver deployment

The simulation and commissioned bench adapters use the same controller, executor and independent verifier. Apply `compose.rover-maneuver.yaml` to the standard deployment to expose `avoid_front_obstacle` to the Agent. Its default site is `sim.json`. The ROS stack must already provide the report, odometry, independent witness and Core services declared in that profile. The supplied simulation profile matches the four-wheel LingAo Gazebo fixture with the calibrated gateway.

## Simulation

Set these variables in the Compose environment. The physical roles and Agent use the same immutable image and platform.

| Variable | Value |
| --- | --- |
| `EDGE_ROVER_IMAGE_REPOSITORY`, `EDGE_ROVER_IMAGE_DIGEST` | The two parts of the locally installed image's `RepoDigests` reference, split at `@`. |
| `EDGE_ROVER_PLATFORM` | Reviewed platform, for example `linux/amd64` for local Gazebo. |
| `EDGE_PHYSICAL_PLATFORM_DIGEST` | Digest of the platform manifest reported by `docker image inspect --platform` for that immutable reference. |
| `EDGE_ROVER_SITE` | Absolute directory containing the original bytes of the platform manifest in `platform.json` and a copy of the approved `sim.json`. The acceptance runner prepares both. |
| `EDGE_ROS_DOMAIN_ID` | Domain shared with the approved Gateway, Core and witness. |
| `EDGE_ROVER_TOKEN_VOLUME` | Existing operator-managed Docker volume containing a private file named `motion`, readable by the Agent. |

Inspect the resolved services before starting the virtual stack.

```sh
docker compose -f compose.yaml -f compose.campus.yaml -f compose.rover-maneuver.yaml config
```

To obtain the exact local platform bytes from a trusted image, the repository's `scripts/prepare_soft_pilots.py` exports `local_image(reference)`. Verify that the SHA-256 of those bytes equals `EDGE_PHYSICAL_PLATFORM_DIGEST`. Keep the operator credential outside the site directory. Supply a 16–256 byte operator token from your credential store. After creating the external volume, an operator can provision it once from a private file on Linux.

```sh
docker volume create "$EDGE_ROVER_TOKEN_VOLUME"
docker run --rm -i --network none --cap-drop ALL \
  --mount "source=$EDGE_ROVER_TOKEN_VOLUME,target=/run/operator" \
  --entrypoint sh "$EDGE_ROVER_IMAGE_REPOSITORY@$EDGE_ROVER_IMAGE_DIGEST" \
  -ec 'umask 077; test ! -e /run/operator/motion; cat > /run/operator/motion' < "$OPERATOR_TOKEN_FILE"
```

The Agent reads this file through a read-only mount. An authorized client sends the token as an HTTP Bearer credential and includes the exact `motion_resource` in its request to `POST /agent/tasks`. In simulation this resource is `sim.lingao.base`. The controller owns the operation ledger and has no network. Each ROS role can access its own socket and site configuration. The simulation roles use the application's ROS network for Gazebo transport discovery. Production volumes survive process and container restarts.

## Local closed-loop acceptance

The runner loads the same production overlay, prepares an isolated temporary credential volume, verifies an unauthorized submission, submits the language task to the model, and checks the physical and Core receipts against the Gazebo witness. Set `EDGE_AVOIDANCE_MODEL_VOLUME` to the existing local Ollama volume and use a local image built from the current source. Windows can invoke the repository's `scripts/project-python.ps1` launcher.

```sh
python3 -B tests/system/campus_local_plan_acceptance.py run \
  --image "$EDGE_IMAGE" --assets "$EDGE_CAMPUS_ASSETS_DIR" --out "$EVIDENCE_DIR" \
  --vehicle lingao --planner footprint_follow --dynamic-policy yield_crossing \
  --scenario crossing --mission-driver agent
```

Use `--scenario centered` for the stationary obstacle. Each run takes a fresh evidence directory. `deployment.json` records the actual platform manifest, immutable images and simulation site hash.

## Commissioned bench configuration

Hardware observe starts from `compose.hardware.yaml` and `compose.hardware-observe.yaml`. To review an explicitly commissioned deployment offline, use these files in order.

```sh
docker compose -f compose.hardware.yaml -f compose.rover-maneuver.yaml \
  -f compose.rover-maneuver-bench.yaml config
```

Set the platform and ROS domain for the actual instrumented bench. The site needs a commissioned `bench.json` with the measured footprint, sensor calibration, independent position witness and separate braking witness. Set `EDGE_ROVER_COMMISSIONING_DIR` to the independent review directory. Its `review.json` and four referenced evidence files are mounted read-only at `/commissioning`. Set `EDGE_ROVER_BENCH_APPROVAL_SHA256` from the operator's trusted record. Approval binds the resource, domain, platform manifest and site profile. All three roles require bench-mode validation. Only the two ROS roles join the host network. The site-specific `hardware.*` resource also requires per-task operator authorization.

Render both configurations and check the service boundaries locally with `python3 -B tests/test_rover_maneuver_compose.py`. That test performs no container starts. Review and live braking requirements are exercised with offline fixtures in `tests/test_rover_maneuver.py`.
