@echo off
title LAPLACE Watchdog

set "PYTHON=C:\Python314\python.exe"
if exist "%PYTHON%" (
  "%PYTHON%" C:\dev\ba\cloud_scripts\watchdog_gui.py
) else (
  python C:\dev\ba\cloud_scripts\watchdog_gui.py
)
