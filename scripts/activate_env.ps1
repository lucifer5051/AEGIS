# SAGE Environment Activation Script (PowerShell)
# Note: Venv path %USERPROFILE%\.aegis\venv is retained for legacy storage compatibility.
$venvPath = "$env:USERPROFILE\.aegis\venv"
if (Test-Path "$venvPath\Scripts\Activate.ps1") {
    Write-Host "Activating SAGE Python 3.12 Virtual Environment at: $venvPath" -ForegroundColor Cyan
    & "$venvPath\Scripts\Activate.ps1"
} else {
    Write-Error "SAGE virtual environment not found at $venvPath."
}
