@echo off
REM AEGIS Environment Activation Script (Windows Command Prompt)
set "VENV_PATH=%USERPROFILE%\.aegis\venv"
if exist "%VENV_PATH%\Scripts\activate.bat" (
    echo Activating AEGIS Python 3.12 Virtual Environment at: %VENV_PATH%
    call "%VENV_PATH%\Scripts\activate.bat"
) else (
    echo [ERROR] AEGIS virtual environment not found at: %VENV_PATH%
)
