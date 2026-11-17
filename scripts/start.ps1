param([switch]$SkipBuild)
$ErrorActionPreference = "Stop"
Push-Location (Split-Path $PSScriptRoot -Parent)
try {
    if (-not $SkipBuild) { & (Join-Path $PSScriptRoot "build.ps1") }
    $config = docker compose config --format json | ConvertFrom-Json
    if ($LASTEXITCODE) { throw "Could not resolve Compose configuration." }
    if ($config.services.agent.environment.EDGE_OLLAMA_URL -match '^http://model:11434/?$') {
        docker compose --profile model up -d model
        if ($LASTEXITCODE) { throw "Local model service startup failed." }
    }
    docker compose up -d --no-build sim bridge witness gateway core perception scene agent console evidence
    if ($LASTEXITCODE) { throw "Compose deployment failed." }
    docker compose run --rm --no-deps -T evidence-admin
    if ($LASTEXITCODE) { throw "Evidence tool registration failed." }
    docker compose exec -T core /usr/local/bin/edge-entrypoint /usr/bin/python3 /opt/edge/client/ready.py
    if ($LASTEXITCODE) { throw "ROS interfaces or live simulated measurements are unavailable. Inspect docker compose logs." }
    $port = $config.services.console.ports[0].published
    $url = "http://127.0.0.1:$port"
    $until = (Get-Date).AddSeconds(25)
    do {
        try { $response = Invoke-WebRequest -UseBasicParsing "$url/api/state" -TimeoutSec 2; break }
        catch { Start-Sleep -Milliseconds 500 }
    } while ((Get-Date) -lt $until)
    if (-not $response -or $response.StatusCode -ne 200) { throw "Console unavailable at $url" }
    Write-Host "Edge console: $url"
    docker compose ps
} finally {
    Pop-Location
}
