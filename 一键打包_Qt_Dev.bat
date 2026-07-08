@echo off
cd /d %~dp0
echo ============================================
echo   Dev Build - PhotoCuller Qt (onedir, fast)
echo ============================================

set "PY=E:\python-envs\photo-culler-qt\Scripts\python.exe"
set "TEMP=E:\Temp"
set "TMP=E:\Temp"

if not exist "%PY%" (
    echo [ERROR] venv not found
    pause
    exit /b 1
)

set DIST=E:\picture_tool\dist_dev
set WORK=E:\picture_tool\build_qt

if not "%1"=="clean" (
    echo [*] Incremental build (build_qt cache kept)
    echo     Use: 一键打包_Qt_Dev.bat clean   to force rebuild
) else (
    echo [*] Full rebuild...
    if exist "%WORK%" rmdir /s /q "%WORK%"
)

echo [*] Building to %DIST% ...
"%PY%" -m PyInstaller PhotoCullerQt.spec ^
    --distpath "%DIST%" ^
    --workpath "%WORK%" ^
    --noconfirm ^
    --log-level WARN

if %errorlevel% equ 0 (
    echo.
    echo ============================================
    echo   Done - %DIST%\照片筛选_Qt.exe
    echo ============================================
) else (
    echo [ERROR] Build failed
)
pause
