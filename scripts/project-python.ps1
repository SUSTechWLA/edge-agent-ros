# Invoke from the workspace: .\scripts\project-python.ps1 -m unittest discover -s tests
# EDGE_PROJECT_PYTHON may point to a verified, independently installed PSF interpreter.
$ErrorActionPreference = 'Stop'

function Confirm-PSFSignedFile([string]$Path) {
    $signature = Get-AuthenticodeSignature -LiteralPath $Path
    if ($signature.Status -ne 'Valid' -or $null -eq $signature.SignerCertificate) {
        throw "No valid Authenticode signature on $Path ($($signature.Status))"
    }
    $signer = $signature.SignerCertificate.GetNameInfo([Security.Cryptography.X509Certificates.X509NameType]::SimpleName, $false)
    if ($signer -ne 'Python Software Foundation') {
        throw "Expected Python Software Foundation to sign $Path, found $signer"
    }
}

function Get-VerifiedInterpreter([string]$Path) {
    if (-not [IO.Path]::IsPathRooted($Path)) { throw 'Supply an absolute python.exe path' }
    $file = Get-Item -Force -LiteralPath $Path
    if ($file.PSIsContainer -or $file.Name -ine 'python.exe' -or $file.Attributes -band [IO.FileAttributes]::ReparsePoint) {
        throw "Expected the installed python.exe at $Path"
    }
    $tempPrefix = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\') + '\'
    if ($file.FullName.StartsWith($tempPrefix,[StringComparison]::OrdinalIgnoreCase) -or
        $file.FullName -match '[\\/](?:WindowsApps|uv|cache|caches|\.?venv)(?:[\\/]|$)') {
        throw "A packaged alias, temporary, cache or virtual-environment runtime is not a project interpreter: $Path"
    }
    $root = $file.Directory.FullName
    if ((Test-Path -LiteralPath (Join-Path $root 'pyvenv.cfg')) -or
        (Test-Path -LiteralPath (Join-Path $file.Directory.Parent.FullName 'pyvenv.cfg'))) {
        throw "Select the installed base interpreter at $Path"
    }
    Confirm-PSFSignedFile $file.FullName
    $minor = $file.VersionInfo.ProductMinorPart
    if ($file.VersionInfo.ProductMajorPart -ne 3 -or $minor -lt 10) { throw "Expected CPython 3.10+ at $Path" }
    Confirm-PSFSignedFile (Join-Path $root "python3$minor.dll")
    return $file.FullName
}

if ($env:EDGE_PROJECT_PYTHON) {
    # An invalid explicitly configured path must fail visibly.
    $interpreter = Get-VerifiedInterpreter $env:EDGE_PROJECT_PYTHON
} else {
    $candidates = foreach ($registryRoot in @('HKCU:\SOFTWARE\Python\PythonCore', 'HKLM:\SOFTWARE\Python\PythonCore')) {
        foreach ($key in (Get-ChildItem -LiteralPath $registryRoot -ErrorAction SilentlyContinue)) {
            if ($key.PSChildName -notmatch '^3\.\d+$') { continue }
            $install = Get-Item -LiteralPath (Join-Path $key.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if ($null -ne $install) {
                $exe = $install.GetValue('ExecutablePath')
                if (-not $exe -and $install.GetValue('')) { $exe = Join-Path $install.GetValue('') 'python.exe' }
                if ($exe) { [pscustomobject]@{ Version = [version]$key.PSChildName; Executable = $exe } }
            }
        }
    }
    $interpreter = $null
    $why = @()
    foreach ($candidate in ($candidates | Sort-Object Version -Descending)) {
        try {
            $interpreter = Get-VerifiedInterpreter $candidate.Executable
            break
        } catch { $why += $_.Exception.Message }
    }
    if (-not $interpreter) {
        throw ("Install a signed python.org CPython or set EDGE_PROJECT_PYTHON to its absolute executable. " + ($why -join ' / '))
    }
}

# Ignore ambient PYTHONHOME/PYTHONPATH and user-site packages inherited from other projects.
if ($MyInvocation.ExpectingInput) {
    $input | & $interpreter -E -s @args
} else {
    & $interpreter -E -s @args
}
exit $LASTEXITCODE
