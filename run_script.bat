@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
title HSITX Ruckus Technical Tool
chcp 949 >nul

echo.
echo ============================================================
echo   HSITX Ruckus Technical Tool
echo ============================================================
echo.
echo Folder: %CD%
echo.

set "PY="
if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"

if defined PY goto PYTHON_OK

echo 필수 프로그램 Python과 관련 모듈을 설치해야 합니다.
set /p ASKPY=설치하시겠습니까? [Y/N]: 
if /I not "%ASKPY%"=="Y" goto CANCEL

echo.
echo 필수 프로그램 Python 3.12 와 관련 모듈을 설치합니다.
echo.

where winget >nul 2>&1
if errorlevel 1 (
    echo [ERROR] winget not found.
    goto END
)

echo [*] Removing stale winget registration if any...
winget uninstall --id Python.Python.3.12 --source winget --accept-source-agreements
echo.
echo [*] winget install Python 3.12 ...
winget install --id Python.Python.3.12 --source winget --scope user --accept-package-agreements --accept-source-agreements
echo.

if exist "%LocalAppData%\Programs\Python\Python312\python.exe" set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not defined PY if exist "%ProgramFiles%\Python312\python.exe" set "PY=%ProgramFiles%\Python312\python.exe"

if not defined PY (
    echo [ERROR] python.exe not found after winget install.
    goto END
)

echo [OK] Python installed.
echo     %PY%
"%PY%" --version
echo.
goto CHECK_LIBS

:PYTHON_OK
echo [OK] Python already installed. Skip.
echo     %PY%
"%PY%" --version
echo.

:CHECK_LIBS
if not defined PY (
    echo [ERROR] Python path is empty.
    goto END
)

echo [*] Checking libraries...
"%PY%" -c "import paramiko, requests, openpyxl, qrcode" >nul 2>&1
if not errorlevel 1 goto LIBS_OK

echo 관련 모듈을 자동 설치합니다.
echo.
"%PY%" -m pip --version >nul 2>&1
if errorlevel 1 (
    echo [*] pip missing. ensurepip...
    "%PY%" -m ensurepip --upgrade
)
"%PY%" -m pip install --upgrade pip
"%PY%" -m pip install -r requirements.txt
echo.
"%PY%" -c "import paramiko, requests, openpyxl, qrcode" >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Library install failed.
    goto END
)

:LIBS_OK
echo [OK] Libraries ready.
echo.

if not exist "gui_app.py" (
    echo [ERROR] gui_app.py not found.
    goto END
)

echo ------------------------------------------------------------
echo Starting GUI...
echo ------------------------------------------------------------
echo.
"%PY%" gui_app.py
set "EXITCODE=%ERRORLEVEL%"
echo.
echo ------------------------------------------------------------
if "%EXITCODE%"=="0" echo Program finished OK.
if not "%EXITCODE%"=="0" echo Program exited with code %EXITCODE%
echo ------------------------------------------------------------
goto END

:CANCEL
echo 설치를 취소했습니다.

:END
echo.
echo Press any key to close...
pause
endlocal
