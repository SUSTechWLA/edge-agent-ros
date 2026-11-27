# PowerShell 7. Forward Docker Compose arguments to one isolated campus simulation.
# With no arguments, show the phase-one indoor/outdoor navigation station.
param([ValidateSet('phase1','crossing','blocked','doorway')][string]$Case = 'phase1')
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$project = "campus-smooth-$Case"
if ($env:EDGE_CAMPUS_RUN) {
    if ($env:EDGE_CAMPUS_RUN -cnotmatch '^[a-z0-9][a-z0-9-]{0,23}$') {
        throw 'EDGE_CAMPUS_RUN must be a short lowercase Compose project suffix.'
    }
    $project += '-' + $env:EDGE_CAMPUS_RUN
}
$settings = @{
    EDGE_SDD058_CASE = $Case
    EDGE_SDD058_DOMAIN = @{crossing='188'; blocked='189'; doorway='190'; phase1='191'}[$Case]
    EDGE_SDD058_PORT = @{crossing='8907'; blocked='8908'; doorway='8909'; phase1='8910'}[$Case]
    EDGE_SDD058_PARTITION = "sdd058_campus_$Case"
}
if (-not $env:EDGE_IMAGE) { $settings.EDGE_IMAGE = 'edge-rover-local:sdd058-dynamic-recovery' }
if (-not $env:EDGE_SDD058_WITNESS_DIR) {
    $settings.EDGE_SDD058_WITNESS_DIR = Join-Path $root '.ci-results/campus-smooth'
}
$ComposeArgs = $args
if (-not $ComposeArgs) { $ComposeArgs = @('ps') }
$previous = @{}
Push-Location -LiteralPath $root
try {
    foreach ($key in $settings.Keys) {
        $previous[$key] = [Environment]::GetEnvironmentVariable($key)
        [Environment]::SetEnvironmentVariable($key, $settings[$key])
    }
    $layers = @('-f', 'compose.yaml', '-f', 'compose/campus-dynamic.yaml')
    docker compose -p $project @layers @ComposeArgs
    exit $LASTEXITCODE
} finally {
    foreach ($key in $previous.Keys) { [Environment]::SetEnvironmentVariable($key, $previous[$key]) }
    Pop-Location
}
