@echo off
setlocal DisableDelayedExpansion
cd /d "%~dp0"
title HSITX Ruckus Tool - Uninstall
chcp 949 >nul

echo.
echo ============================================================
echo   HSITX Ruckus Tool - Uninstall
echo ============================================================
echo.
set /p ASK=설치된 Python과 관련 모듈을 uninstall 하시나요? [Y/N]: 
if /I not "%ASK%"=="Y" (
    echo 취소했습니다.
    goto END
)

echo.
echo 설치된 Python과 관련 모듈을 제거합니다.
echo.

set "PYDIR=%LocalAppData%\Programs\Python\Python312"
set "PY="
if exist "%PYDIR%\python.exe" set "PY=%PYDIR%\python.exe"

echo ------------------------------------------------------------
echo 1. pip packages
echo ------------------------------------------------------------
if defined PY (
    echo Using: %PY%
    "%PY%" -m pip uninstall -y paramiko requests urllib3 openpyxl qrcode pillow bcrypt cryptography invoke pynacl charset_normalizer idna certifi et-xmlfile colorama cffi pycparser
    echo [OK] pip uninstall attempted
) else (
    echo [SKIP] python.exe not found
)
echo.

echo ------------------------------------------------------------
echo 2. winget uninstall Python
echo ------------------------------------------------------------
where winget >nul 2>&1
if errorlevel 1 (
    echo [ERROR] winget not found.
    goto CHECK_GONE
)
echo - Python.Python.3.12
winget uninstall --id Python.Python.3.12 --source winget --accept-source-agreements
echo.

:CHECK_GONE
echo ------------------------------------------------------------
echo 3. leftover folders
echo ------------------------------------------------------------
if exist "%PYDIR%\python.exe" (
    echo [SKIP] python.exe still exists. Folders were NOT deleted.
    goto DONE
)

echo [OK] python.exe is gone. Removing leftover folders...
if exist "%LocalAppData%\Programs\Python" (
    rmdir /s /q "%LocalAppData%\Programs\Python"
    echo - removed %LocalAppData%\Programs\Python
) else (
    echo - no %LocalAppData%\Programs\Python
)
if exist "%LocalAppData%\pip" (
    rmdir /s /q "%LocalAppData%\pip"
    echo - removed %LocalAppData%\pip
) else (
    echo - no %LocalAppData%\pip
)
if exist "%APPDATA%\Python" (
    rmdir /s /q "%APPDATA%\Python"
    echo - removed %APPDATA%\Python
) else (
    echo - no %APPDATA%\Python
)

:DONE
echo.
echo ============================================================
echo [DONE]
echo ============================================================

:END
echo.
echo Press any key to close...
pause
endlocal
