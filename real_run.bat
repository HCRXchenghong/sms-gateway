@echo off
REM ----------------------------------------------------------------------
REM Local real-hardware launcher (Windows). Uses the inserted SIMCom module.
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

set "SMSGW_HOST=127.0.0.1"
set "SMSGW_PORT=8080"
set "SMSGW_DATA_DIR=./_realdata"
set "SMSGW_LOG_DIR=./_realdata/log"
set "SMSGW_CONFIG_DIR=./_realdata/etc"
set "SMSGW_SESSION_SECRET_FILE=./_realdata/etc/secret.key"
set "SMSGW_MOCK_MODEM=false"
set "SMSGW_MOCK_MODEM_COUNT="
set "SMSGW_MODEM_AT_PORT="

echo.
echo ====================================================================
echo  Real modem mode. Open: http://127.0.0.1:8080/
echo  The app will auto-detect the SIMCom AT Port, e.g. COM15.
echo  Press Ctrl+C to stop.
echo ====================================================================
echo.

python -m uvicorn app.main:app --host 127.0.0.1 --port 8080

endlocal
