@echo off
title LAPLACE GUI
echo ================================================
echo LAPLACE GUI Starting
echo ================================================
echo.

set "NODE_PATH=C:\Program Files\nodejs"
set "PATH=%NODE_PATH%;C:\Program Files\Git\cmd;C:\Python314;%PATH%"

echo [1/3] Checking Node.js...
"%NODE_PATH%\node.exe" --version
if errorlevel 1 goto :error_node

echo [2/3] Changing to gui directory...
cd /d C:\dev\ba\gui
if errorlevel 1 goto :error_dir

echo [3/3] Starting Electron...
echo.
call "%NODE_PATH%\npm.cmd" run dev

echo.
echo GUI exited.
pause
exit /b 0

:error_node
echo ERROR: Node.js not found at %NODE_PATH%
pause
exit /b 1

:error_dir
echo ERROR: Cannot find C:\dev\ba\gui
pause
exit /b 1
