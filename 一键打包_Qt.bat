@echo off
chcp 65001 >nul
cd /d %~dp0

echo ============================================
echo   PhotoCuller - PySide6 Build
echo ============================================

set "PY=E:\python-envs\photo-culler-qt\Scripts\python.exe"

if not exist "%PY%" (
    echo [ERROR] Python venv not found: E:\python-envs\photo-culler-qt
    pause
    exit /b 1
)

echo [1/2] Cleaning old build...
if exist build_qt rmdir /s /q build_qt

echo [2/2] Building with PyInstaller...
"%PY%" -m PyInstaller PhotoCullerQt.spec --distpath dist_release --workpath build_qt

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   Build OK -^> dist_release\PhotoCuller_Qt.exe
    echo ============================================
) else (
    echo.
    echo [ERROR] Build failed, check log above.
)
pause
