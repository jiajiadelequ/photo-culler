@echo off
cd /d "%~dp0"
echo ============================================
echo   Release Build - PhotoCuller Qt (onefile)
echo ============================================
set "PY=E:\python-envs\photo-culler-qt\Scripts\python.exe"
set "TEMP=E:\Temp"
set "TMP=E:\Temp"
if not exist "%PY%" (echo [ERROR] venv not found && pause && exit /b 1)
echo [*] Full rebuild for release...
if exist build_qt_rel rmdir /s /q build_qt_rel
echo [*] Building single EXE to dist_release ...
"%PY%" -m PyInstaller PhotoCullerQt_release.spec --distpath dist_release --workpath build_qt_rel --noconfirm --log-level WARN
if %errorlevel% equ 0 (echo   Done - dist_release\照片筛选_Qt.exe) else (echo [ERROR] Build failed)
pause
