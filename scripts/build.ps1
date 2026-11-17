$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    $base = "edge-agent-ros/base:jazzy-harmonic"
    if (-not $env:EDGE_BASE_IMAGE -or $env:EDGE_BASE_IMAGE -eq $base) {
        $local = docker image ls --format '{{.ID}}' $base
        if ($LASTEXITCODE) { throw "Docker Engine unavailable." }
        if (-not $local) {
            docker build --file docker/Dockerfile.base --tag $base .
            if ($LASTEXITCODE) { throw "Could not prepare ROS/Gazebo base image." }
        }
    }
    docker compose build sim
    if ($LASTEXITCODE) { throw "Application image build failed." }
} finally { Pop-Location }
