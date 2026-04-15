@echo off
setlocal enabledelayedexpansion
REM LAPLACE 更新 + 再起動スクリプト (クラウドPC用)
REM 1. Electron停止
REM 2. R2 ZIP ダウンロード
REM 3. 展開 + 上書き
REM 4. GUI再起動

for %%I in ("%~dp0..") do set "BASE_DIR=%%~fI"

echo ================================================
echo LAPLACE Update and Restart
echo ================================================

echo [1/4] Stopping existing Electron processes...
taskkill /F /IM electron.exe 2>nul
timeout /t 2 /nobreak >nul

if "%LAPLACE_UPDATE_URL%"=="" (
  if exist "%BASE_DIR%\.env" (
    for /f "usebackq tokens=1* delims==" %%A in ("%BASE_DIR%\.env") do (
      if /I "%%A"=="LAPLACE_UPDATE_URL" set "LAPLACE_UPDATE_URL=%%B"
      if /I "%%A"=="LAPLACE_UPDATE_VERSION" set "LAPLACE_UPDATE_VERSION=%%B"
    )
  )
)

if "%LAPLACE_UPDATE_URL%"=="" (
  echo ERROR: LAPLACE_UPDATE_URL missing
  pause
  exit /b 1
)

set "TMP_DIR=%BASE_DIR%\_update_tmp"
set "ZIP_PATH=%TMP_DIR%\laplace.zip"
set "EXTRACT_DIR=%TMP_DIR%\extract"

echo [2/4] Downloading update...
if exist "%TMP_DIR%" rmdir /s /q "%TMP_DIR%" >nul 2>&1
mkdir "%EXTRACT_DIR%" >nul 2>&1
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri '%LAPLACE_UPDATE_URL%' -OutFile '%ZIP_PATH%'" || goto :error_download

echo [3/4] Extracting...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Expand-Archive -Force '%ZIP_PATH%' '%EXTRACT_DIR%'" || goto :error_extract

set "SOURCE_DIR=%EXTRACT_DIR%"
for /d %%D in ("%EXTRACT_DIR%\*") do (
  if exist "%%D\gui" set "SOURCE_DIR=%%D"
)

echo [4/4] Applying update...
robocopy "%SOURCE_DIR%" "%BASE_DIR%" /E /XD "auth_state" "data" "screenshots" "browser_profile" "browser_data" ".git" "_update_tmp" /XF ".env" >nul

echo Starting GUI...
if exist "%BASE_DIR%\cloud_scripts\run.bat" (
  start "" "%BASE_DIR%\cloud_scripts\run.bat"
) else (
  start "" "%BASE_DIR%\gui\node_modules\.bin\electron.cmd"
)

echo.
echo Done! GUI should be starting now.
timeout /t 3 /nobreak >nul
exit /b 0

:error_download
echo ERROR: Download failed
pause
exit /b 1

:error_extract
echo ERROR: Extract failed
pause
exit /b 1
