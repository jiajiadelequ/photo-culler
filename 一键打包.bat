@echo off
cd /d %~dp0
py -3 -m PyInstaller PhotoCuller.spec --distpath dist_release
pause
