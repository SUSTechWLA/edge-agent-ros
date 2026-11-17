#requires -Version 7.0
[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$registry = 'https://registry.ollama.ai/v2/library/qwen2.5'
$manifestHash = '65ec06548149b04c096a120e4a6da9d4017ea809c91734ea5631e89f96ddc57b'
$work = Join-Path ([IO.Path]::GetTempPath()) 'edge-agent-ros-qwen2.5-1.5b'
$null = New-Item -Path $work -ItemType Directory -Force
$lock = [IO.File]::Open((Join-Path $work '.import.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
Push-Location (Join-Path $PSScriptRoot '..')
try {
    $manifest = Join-Path $work '1.5b'
    curl.exe -fsSL --retry 3 --max-time 60 "$registry/manifests/1.5b" -o $manifest
    if ($LASTEXITCODE -ne 0 -or (Get-FileHash $manifest).Hash -ne $manifestHash) {
        throw 'The model manifest did not match the verified version. Check the upstream revision before importing.'
    }
    $metadata = Get-Content -Raw $manifest | ConvertFrom-Json
    $files = foreach ($layer in (@($metadata.config) + $metadata.layers)) {
        $digest = $layer.digest.Split(':')[1]
        $blob = Join-Path $work "sha256-$digest"
        if (!(Test-Path -LiteralPath $blob) -or (Get-FileHash $blob).Hash -ne $digest) {
            $url = "$registry/blobs/$($layer.digest)"
            if ($layer.size -gt 16MB) {
                $span = 16MB
                $total = [long]$layer.size
                $count = [int][Math]::Ceiling($total / $span)
                $results = 0..($count - 1) | ForEach-Object -Parallel {
                    $start = [long]$_ * $using:span
                    $end = [Math]::Min($start + $using:span, $using:total) - 1
                    $part = "$using:blob.part.$_"
                    curl.exe -fsSL --retry 3 --retry-all-errors --max-time 200 -r "$start-$end" -o $part $using:url
                    $LASTEXITCODE -eq 0 -and (Test-Path -LiteralPath $part) -and
                        (Get-Item -LiteralPath $part).Length -eq ($end - $start + 1)
                } -ThrottleLimit 8
                if (@($results).Count -ne $count -or $results -contains $false) {
                    throw "Incomplete range while downloading $digest"
                }
                $outputFile = [IO.File]::Create($blob)
                try {
                    foreach ($index in 0..($count - 1)) {
                        $part = "$blob.part.$index"
                        $inputFile = [IO.File]::OpenRead($part)
                        try { $inputFile.CopyTo($outputFile) } finally { $inputFile.Dispose() }
                        Remove-Item -LiteralPath $part
                    }
                } finally { $outputFile.Dispose() }
            } else {
                curl.exe -fsSL --retry 3 --retry-all-errors --max-time 90 -o $blob $url
                if ($LASTEXITCODE -ne 0) { throw "Failed to download $digest" }
            }
            if ((Get-Item -LiteralPath $blob).Length -ne $layer.size -or (Get-FileHash $blob).Hash -ne $digest) {
                throw "Invalid blob $digest"
            }
        }
        $blob
    }
    docker compose --profile model up -d --no-build model
    if ($LASTEXITCODE -ne 0) { throw 'Model service failed to start.' }
    $target = '/root/.ollama/models'
    $directory = "$target/manifests/registry.ollama.ai/library/qwen2.5"
    docker compose exec -T model mkdir -p "$target/blobs" $directory
    if ($LASTEXITCODE -ne 0) { throw 'Model volume is unavailable.' }
    foreach ($blob in $files) {
        $path = "$target/blobs/$(Split-Path -Leaf $blob)"
        docker compose cp $blob "model:$path.import"
        if ($LASTEXITCODE -ne 0) { throw 'Failed to copy verified model blob.' }
        docker compose exec -T model mv "$path.import" $path
        if ($LASTEXITCODE -ne 0) { throw 'Failed to install verified model blob.' }
    }
    docker compose cp $manifest "model:$directory/1.5b.import"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to copy the model manifest.' }
    docker compose exec -T model mv "$directory/1.5b.import" "$directory/1.5b"
    if ($LASTEXITCODE -ne 0) { throw 'Failed to publish the verified model manifest.' }
    docker compose exec -T model ollama show qwen2.5:1.5b
    if ($LASTEXITCODE -ne 0) { throw 'Ollama could not read the imported model.' }
    foreach ($file in (@($files) + $manifest)) { Remove-Item -LiteralPath $file }
} finally {
    $lock.Dispose()
    Pop-Location
}
