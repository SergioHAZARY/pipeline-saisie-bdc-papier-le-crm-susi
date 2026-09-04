# Démarre la plateforme susi-bdc sur le port 8760 (équivalent Windows de start.sh).
$ErrorActionPreference = "Stop"
$racine = Split-Path -Parent $PSScriptRoot
$py = Join-Path $racine ".venv\Scripts\python.exe"

if (-not (Test-Path $py)) {
    Write-Host "Création du venv..." -ForegroundColor Cyan
    python -m venv (Join-Path $racine ".venv")
    & $py -m pip install --timeout 180 --retries 15 --progress-bar off -r (Join-Path $PSScriptRoot "requirements.txt")
}

# pdftoppm doit être joignable : winget install oschwartz10612.Poppler
$popplerGlob = Join-Path $env:LOCALAPPDATA "Microsoft\WinGet\Packages\*Poppler*\*\Library\bin"
$poppler = Get-Item $popplerGlob -ErrorAction SilentlyContinue | Select-Object -First 1
if ($poppler) { $env:PATH = "$env:PATH;$($poppler.FullName)" }

Set-Location $racine
& $py -m uvicorn app.app:app --host 0.0.0.0 --port 8760
