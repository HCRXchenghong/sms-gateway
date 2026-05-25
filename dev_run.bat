@echo off
REM ----------------------------------------------------------------------
REM Local development launcher (Windows). For UI preview without a modem.
REM ----------------------------------------------------------------------
setlocal

set "PY=C:\Users\HCRXc\AppData\Local\Programs\Python\Python311\python.exe"
if not exist "%PY%" (
    where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python not found. Install Python 3.11 first.
    exit /b 1
)

if not exist ".venv" (
    echo Creating virtual env...
    "%PY%" -m venv .venv || exit /b 1
)

call ".venv\Scripts\activate.bat"

echo Installing requirements (first run only takes a minute)...
python -m pip install --upgrade pip --quiet
python -m pip install -r requirements.txt --quiet || exit /b 1

REM Use the dev env file for Windows-friendly paths and enable mock modem.
if not exist ".env" (
    copy /Y .env.dev .env >nul
)

echo.
echo ====================================================================
echo  Open your browser at: http://127.0.0.1:8080/
echo  First-run wizard will let you create an admin and bind 2FA.
echo  Press Ctrl+C to stop.
echo ====================================================================
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload

endlocal
