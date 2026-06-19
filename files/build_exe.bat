@echo off
REM ============================================================
REM  Build RoRejoin into a SINGLE .exe (Windows)
REM  PyInstaller follows the imports in rorejoin.py and bundles
REM  every rr_*.py module into the one file automatically.
REM  Just keep all the .py files together in this folder.
REM ============================================================

echo Installing/updating build dependencies...
python -m pip install --upgrade customtkinter pyinstaller

echo.
echo Building single-file RoRejoin.exe ...
python -m PyInstaller --onefile --windowed --clean ^
    --collect-all customtkinter ^
    --name RoRejoin ^
    rorejoin.py

echo.
echo Done. Your single exe is in:  dist\RoRejoin.exe
pause
