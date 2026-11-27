# Operate the ten daily campus services on the seeded phase-one r08 project.
# EDGE_CAMPUS_RUN selects an alternative previously seeded site.
param([ValidateSet('up','ps','logs','stop','config')][string]$Action = 'ps')
$ErrorActionPreference = 'Stop'
$originalRun = $env:EDGE_CAMPUS_RUN
if (-not $originalRun) { $env:EDGE_CAMPUS_RUN = 'r08' }
$services = @('sim','bridge','perception','core','gateway','agent','console',
              'survey-readonly','survey-session','survey-maps')
try {
    if ($env:EDGE_CAMPUS_RUN -cnotmatch '^[a-z0-9][a-z0-9-]{0,23}$') {
        throw 'EDGE_CAMPUS_RUN must be a short lowercase Compose project suffix.'
    }
    if ($Action -in @('up','stop') -and $args.Count) {
        throw "campus-core.ps1 $Action operates on the ten daily services and accepts no extra arguments."
    }
    if ($Action -eq 'up') {
        $project = "campus-smooth-phase1-$env:EDGE_CAMPUS_RUN"
        docker volume inspect "${project}_survey-maps" "${project}_survey-state" --format '{{.Name}}' | Out-Null
        if ($LASTEXITCODE) { throw "The site $project needs its published map and session volumes. Seed the chosen site with the phase1 launcher first." }
        $command = @('up','--detach','--no-deps')
    } else {
        $command = @($Action)
    }
    $overlay = Join-Path $PSScriptRoot '../compose/campus-core.yaml'
    & (Join-Path $PSScriptRoot 'campus-navigation.ps1') phase1 -f $overlay @command @args @services
    exit $LASTEXITCODE
} finally {
    $env:EDGE_CAMPUS_RUN = $originalRun
}
