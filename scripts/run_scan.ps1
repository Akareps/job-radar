# Wrapper invoked by Task Scheduler every hour.
# Exits quietly when offline so a closed laptop lid never produces an error toast.

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

# The scanner does its own connectivity check, but bailing here avoids
# spinning up Python at all on a disconnected machine.
if (-not (Test-Connection -ComputerName 1.1.1.1 -Count 1 -Quiet -ErrorAction SilentlyContinue)) {
    Add-Content -Path (Join-Path $root "data\scan.log") -Encoding utf8 `
        -Value "$(Get-Date -Format s) INFO    scheduler              offline, skipping"
    exit 0
}

& $python -m jobradar scan --sync
exit $LASTEXITCODE
