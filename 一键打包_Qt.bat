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

if "%1"=="clean" (
    echo [*] Full rebuild (cleaning cache)...
    if exist build_qt rmdir /s /q build_qt
) else (
    echo [*] Incremental build (keep cache for speed)
    echo     Use: 一键打包_Qt.bat clean   to force full rebuild
)

echo [*] Building...
"%PY%" -m PyInstaller PhotoCullerQt.spec --distpath dist_release --workpath build_qt --noconfirm

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   Build OK -^> dist_release\照片筛选_Qt.exe
    echo ============================================
) else (
    echo.
    echo [ERROR] Build failed, check log above.
)
pause
