@echo off
cd /d %~dp0

echo ============================================
echo   PhotoCuller - PySide6 Build
echo ============================================

set "PY=E:\python-envs\photo-culler-qt\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] venv not found: E:\python-envs\photo-culler-qt
    pause
    exit /b 1
)

if "%1"=="clean" (
    echo [*] Full rebuild...
    if exist build_qt rmdir /s /q build_qt
) else (
    echo [*] Incremental build
)

echo [*] Building...
"%PY%" -m PyInstaller PhotoCullerQt.spec --distpath dist_release --workpath build_qt --noconfirm

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   Build OK - dist_release\PhotoCuller_Qt.exe
    echo ============================================
) else (
    echo.
    echo [ERROR] Build failed.
)
pause
