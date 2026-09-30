@echo off
REM SAGE Environment Activation Script (Windows Command Prompt)
REM Note: Venv path %USERPROFILE%\.aegis\venv is retained for legacy storage compatibility.
set "VENV_PATH=%USERPROFILE%\.aegis\venv"
if exist "%VENV_PATH%\Scripts\activate.bat" (
    echo Activating SAGE Python 3.12 Virtual Environment at: %VENV_PATH%
    call "%VENV_PATH%\Scripts\activate.bat"
) else (
    echo [ERROR] SAGE virtual environment not found at: %VENV_PATH%
)
