$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$repoRoot = Resolve-Path (Join-Path $PSScriptRoot '..')
Set-Location $repoRoot

$venvPython = Join-Path $repoRoot '.venv\Scripts\python.exe'
if (-not (Test-Path $venvPython)) {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        py -m venv (Join-Path $repoRoot '.venv')
    }
    else {
        python -m venv (Join-Path $repoRoot '.venv')
    }
}

& $venvPython (Join-Path $PSScriptRoot 'build_release.py')
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
