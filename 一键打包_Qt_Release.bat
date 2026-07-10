@echo off
cd /d %~dp0
echo ============================================
echo   Release Build - PhotoCuller Qt (onefile)
echo ============================================
set PY=E:\python-envs\photo-culler-qt\Scripts\python.exe
set TEMP=E:\Temp
set TMP=E:\Temp
if not exist %PY% (echo [ERROR] venv not found && pause && exit /b 1)
echo [*] Full rebuild for release...
if exist build_qt_rel rmdir /s /q build_qt_rel
if exist dist_release rmdir /s /q dist_release 2>nul
if exist dist_release (
    echo [ERROR] Cannot delete dist_release - close any running instance first
    pause
    exit /b 1
)
echo [*] Building single EXE to dist_release ...
%PY% -m PyInstaller PhotoCullerQt_release.spec --distpath dist_release --workpath build_qt_rel --noconfirm --log-level WARN
if %errorlevel% equ 0 (echo   Done - dist_release\QPhotoCuller_Qt.exe) else (echo [ERROR] Build failed)
pause
