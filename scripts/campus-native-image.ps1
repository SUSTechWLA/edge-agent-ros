#Requires -Version 7.4
param([ValidateSet('site','continuous')][string]$Variant = 'site')
$ErrorActionPreference = 'Stop'
$nativeRef = 'refs/heads/archive/rover-local-before-consolidation-20261125'
$image = @{site='edge-rover-local:sdd058-campus-site-r1'
           continuous='edge-rover-local:sdd058-campus-continuous-r3'}[$Variant]
Push-Location -LiteralPath (Split-Path -Parent $PSScriptRoot)
try {
    $revision = git rev-parse --verify "${nativeRef}^{commit}"
    if ($LASTEXITCODE) { throw "Campus native source snapshot is unavailable: $nativeRef" }
    # Native-to-native piping preserves the binary tar context in PowerShell 7.4+.
    git archive --format=tar --add-file=docker/Dockerfile.campus-continuous "${revision}:src" |
        docker build --build-arg "EDGE_REVISION=$revision" -f Dockerfile.campus-continuous -t $image -
    if ($LASTEXITCODE) { throw "Campus native image build failed: $image" }
} finally {
    Pop-Location
}
