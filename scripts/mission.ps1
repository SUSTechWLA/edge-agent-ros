# Execute using the deployed container's ROS overlay so the host needs only Docker.
$root = Split-Path $PSScriptRoot -Parent
Push-Location $root
try {
    & docker compose exec -T core /usr/local/bin/edge-entrypoint /usr/bin/python3 /opt/edge/client/mission.py @args
    $code = $LASTEXITCODE
} finally { Pop-Location }
exit $code
