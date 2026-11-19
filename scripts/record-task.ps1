# PowerShell entry point for the same Docker operator commands used on Linux.
$ErrorActionPreference = 'Stop'
$gitExe = (Get-Command git.exe -CommandType Application | Select-Object -First 1).Source
$gitRoot = Split-Path (Split-Path $gitExe -Parent) -Parent
$shell = Join-Path $gitRoot 'bin/sh.exe'
if (-not (Test-Path -LiteralPath $shell -PathType Leaf)) {
    throw 'Install Git for Windows or run scripts/record-task.sh in a POSIX shell.'
}
& $shell (Join-Path $PSScriptRoot 'record-task.sh') @args
exit $LASTEXITCODE
