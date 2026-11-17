param([switch]$SkipBuild)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$id = (Get-Date).ToUniversalTime().ToString("yyyyMMdd-HHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,6)
$outputDir = Join-Path $root "artifacts/verification/$id"
$null = New-Item -ItemType Directory -Path $outputDir
$priorOutput = $env:EDGE_EVIDENCE_DIR
$env:EDGE_EVIDENCE_DIR = $outputDir
$stacks = @()
$primary = "edge-test-$id"
$fault = "$primary-link"
$passed = $false
$failure = ""
function Invoke-Compose([string]$project, [string[]]$arguments) {
    & docker compose -p $project @arguments
    if ($LASTEXITCODE) { throw "Compose failed ($LASTEXITCODE): $project $($arguments -join ' ')" }
}
function Probe([string]$project, [string]$phase) {
    Invoke-Compose $project @("run","--rm","--no-deps","-T","probe","python3","/opt/edge/tests/verify.py",$phase)
}
function Start-Witness([string]$project, [string]$kind) {
    Invoke-Compose $project @("run","-d","--no-deps","-T","--name","$project-watch","probe",
        "python3","/opt/edge/tests/stop_witness.py",$kind)
    $ready = Join-Path $outputDir "watch-$kind.ready"
    $limit = (Get-Date).AddSeconds(45)
    while (-not (Test-Path -LiteralPath $ready)) {
        if ((Get-Date) -ge $limit) { throw "No live independent world witness for $kind" }
        Start-Sleep -Milliseconds 150
    }
}
function Stop-Component([string]$project, [string]$kind, [string]$service) {
    [IO.File]::WriteAllText((Join-Path $outputDir "watch-$kind.inject"),$kind)
    Invoke-Compose $project @("kill","-s","SIGKILL",$service)
    $code = docker wait "$project-watch"
    if ($LASTEXITCODE -or $code -ne "0") {
        docker logs "$project-watch"
        throw "World-pose stopping verification failed for $kind"
    }
}
function Export-Ledger([string]$project, [string]$filename) {
    $snapshot = docker compose -p $project exec -T core /usr/bin/python3 /opt/edge/tests/export_ledger.py
    if ($LASTEXITCODE) { throw "SQLite audit failed: $project" }
    $snapshot | Set-Content (Join-Path $outputDir $filename) -Encoding utf8
}
Push-Location $root
try {
    if (-not $SkipBuild) { & (Join-Path $PSScriptRoot "build.ps1") }
    $image = if ($env:EDGE_IMAGE) { $env:EDGE_IMAGE } else { "edge-agent-ros:v0.1" }
    $imageId = docker image inspect $image --format '{{.Id}}'
    if ($LASTEXITCODE) { throw "Image unavailable: $image" }
    $sourceFiles = @(Get-ChildItem src,sim,client,tests,docker,console,perception,agent -Recurse -File) +
                   @(Get-Item compose.yaml,.dockerignore,scripts/build.ps1,scripts/verify.ps1)
    $sourceFiles | Where-Object { $_.FullName -notmatch '[\\/]__pycache__[\\/]' } |
        Sort-Object FullName | ForEach-Object {
            $relative = $_.FullName.Substring($root.Length + 1).Replace('\','/')
            "$( (Get-FileHash -Algorithm SHA256 -LiteralPath $_.FullName).Hash.ToLowerInvariant() )  $relative"
        } | Set-Content (Join-Path $outputDir "source-sha256.txt") -Encoding utf8
    & docker run --rm --network none $image bash -c 'uname -m; gz sim --versions; c++ --version; dpkg-query -W ros-jazzy-rclcpp ros-jazzy-ros-gz-bridge libsqlite3-dev' | Set-Content (Join-Path $outputDir "environment.txt") -Encoding utf8
    if ($LASTEXITCODE) { throw "Environment fingerprint failed." }
    & docker run --rm --network none $image ctest --test-dir /opt/edge/build/edge_core --output-on-failure --no-tests=error | Tee-Object -FilePath (Join-Path $outputDir "core-tests.txt")
    if ($LASTEXITCODE) { throw "Core strategy or ledger self-test failed." }
    & docker run --rm --network none $image /usr/bin/python3 /opt/edge/src/edge_gateway/test/watchdogs.py | Tee-Object -FilePath (Join-Path $outputDir "gateway-test.txt")
    if ($LASTEXITCODE) { throw "ROS action watchdog self-test failed." }
    $services = @("up","-d","--no-build","sim","bridge","witness","gateway","core")
    $stacks += $primary
    Invoke-Compose $primary $services
    Probe $primary "mission"
    Probe $primary "cancel"
    Invoke-Compose $primary @("restart","core")
    Probe $primary "history"
    Start-Witness $primary "crash"
    Probe $primary "prime-crash"
    Stop-Component $primary "crash" "core"
    Invoke-Compose $primary @("up","-d","--no-build","core")
    Probe $primary "crash"
    Export-Ledger $primary "ledger-main.json"
    $stacks += $fault
    Invoke-Compose $fault $services
    Start-Witness $fault "bridge"
    Probe $fault "prime-bridge"
    Stop-Component $fault "bridge" "bridge"
    Probe $fault "bridge-loss"
    Export-Ledger $fault "ledger-link.json"
    $passed = $true
} catch {
    $failure = $_.Exception.Message
    throw
} finally {
    foreach ($p in $stacks) {
        & docker compose -p $p logs --no-color --timestamps | Out-File (Join-Path $outputDir "$p.log") -Encoding utf8
        # Only ephemeral stacks created with this run's timestamp and UUID are removed.
        & docker compose -p $p down --volumes --remove-orphans --timeout 3
    }
    [ordered]@{run_id=$id; passed=$passed; error=$failure; image=$image; image_id=$imageId;
        engine=(docker version --format '{{.Server.Version}}'); compose=(docker compose version --short);
        platform=(docker info --format '{{.OSType}}/{{.Architecture}}')
    } | ConvertTo-Json | Set-Content (Join-Path $outputDir "result.json") -Encoding utf8
    Write-Host "Evidence: $outputDir"
    $env:EDGE_EVIDENCE_DIR = $priorOutput
    Pop-Location
}
