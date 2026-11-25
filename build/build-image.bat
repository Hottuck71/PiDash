@echo off
rem Rebuilds the PiDash SD-card image into build\outputs\pidash.img
cd /d "%~dp0"
if not exist outputs mkdir outputs
if exist outputs\pidash.img del outputs\pidash.img
if not exist venv\Scripts\python.exe (
    python -m venv venv
    venv\Scripts\python -m pip install -q pyfatfs "setuptools<81"
)
venv\Scripts\python -I ..\pi-dashboard\build_image.py dl\base.img.xz outputs\pidash.img
echo.
echo Done: %~dp0outputs\pidash.img
pause
