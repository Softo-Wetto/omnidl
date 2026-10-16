@echo off
rem ==========================================================================
rem  OmniDL - double-click this to start it. This is the only file you run.
rem  (Safe to drag into RocketDock or pin. Running it again while OmniDL is
rem  already open just opens it in your browser.)
rem  Keep this window open while you use OmniDL; close it to stop OmniDL.
rem ==========================================================================
title OmniDL
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python isn't installed or isn't on PATH, so OmniDL can't start.
    echo Install Python from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^),
    echo then run:  pip install -r requirements.txt
    pause
    exit /b 1
)

python run.py %*

rem Only keep the window open if something went wrong, so the message can be read.
if errorlevel 1 pause
