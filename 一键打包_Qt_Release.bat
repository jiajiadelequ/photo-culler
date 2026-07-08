@echo off
cd /d %~dp0
echo ============================================
echo   Release Build - PhotoCuller Qt (onefile)
echo ============================================

set "PY=E:\python-envs\photo-culler-qt\Scripts\python.exe"
set "TEMP=E:\Temp"
set "TMP=E:\Temp"

if not exist "%PY%" (
    echo [ERROR] venv not found
    pause
    exit /b 1
)

set DIST=E:\picture_tool\dist_release
set WORK=E:\picture_tool\build_qt_rel

echo [*] Full rebuild for release...
if exist "%WORK%" rmdir /s /q "%WORK%"
if exist "%DIST%" rmdir /s /q "%DIST%"

echo [*] Building single EXE...
"%PY%" -m PyInstaller PhotoCullerQt.spec ^
    --distpath "%DIST%" ^
    --workpath "%WORK%" ^
    --noconfirm ^
    --onefile ^
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
