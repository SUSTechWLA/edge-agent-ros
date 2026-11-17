$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
$oldRevision=$env:EDGE_REVISION
try {
    if (-not $oldRevision -and (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path .git)) {
        $env:EDGE_REVISION=(git rev-parse HEAD).Trim()
        if (git status --porcelain) { $env:EDGE_REVISION += "-dirty" }
    }
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
} finally { $env:EDGE_REVISION=$oldRevision; Pop-Location }
