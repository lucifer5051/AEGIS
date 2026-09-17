# AEGIS Environment Activation Script (PowerShell)
$venvPath = "$env:USERPROFILE\.aegis\venv"
if (Test-Path "$venvPath\Scripts\Activate.ps1") {
    Write-Host "Activating AEGIS Python 3.12 Virtual Environment at: $venvPath" -ForegroundColor Cyan
    & "$venvPath\Scripts\Activate.ps1"
} else {
    Write-Error "AEGIS virtual environment not found at $venvPath."
}
