@echo off
rem LAPLACE2 Pragmatic Collector - Windows launcher
rem Runs in background, headless, logs to collector_pragmatic.log
title LAPLACE2 Pragmatic Collector
cd /d %~dp0
set PYTHONUNBUFFERED=1
python collector_pragmatic.py --headless
pause
