@echo off
echo.
echo  LAPLACE Setup
echo  ========================
echo.

:: Install OpenSSH Client if not present
where ssh >nul 2>&1
if errorlevel 1 (
  echo  [INFO] Installing OpenSSH Client...
  powershell -Command "Add-WindowsCapability -Online -Name OpenSSH.Client~~~~0.0.1.0" >nul 2>&1
  echo  [OK] OpenSSH Client installed
) else (
  echo  [OK] OpenSSH Client already installed
)

:: Place SSH key
set SSH_DIR=%USERPROFILE%\.ssh
set KEY_SRC=%~dp0laplace_vps
set KEY_DST=%SSH_DIR%\laplace_vps

if not exist "%KEY_SRC%" (
  echo  [ERROR] laplace_vps file not found.
  echo  Please place laplace_vps in the same folder as this setup.bat.
  pause
  exit /b 1
)

if not exist "%SSH_DIR%" mkdir "%SSH_DIR%"
copy /Y "%KEY_SRC%" "%KEY_DST%" > nul
echo  [OK] SSH key placed: %KEY_DST%

:: Set strict permissions for SSH key
powershell -Command "& { $k='%KEY_DST%'; $acl=Get-Acl $k; $acl.SetAccessRuleProtection($true,$false); $r=New-Object System.Security.AccessControl.FileSystemAccessRule('%USERNAME%','FullControl','Allow'); $acl.SetAccessRule($r); Set-Acl $k $acl }" > nul 2>&1
echo  [OK] SSH key permissions set

:: Test VPS connection
:: [2026-09-12] 旧VPS 210.131.215.116 は解約済み。Xserver が別契約者へ再割当するため、
:: ハードコード + StrictHostKeyChecking=no のままだと見知らぬ第三者のホストへ接続し、
:: そのホスト鍵を known_hosts に焼き付けてしまう。既定値は持たせない。
if "%LAPLACE_SSH_HOST%"=="" (
  echo  [SKIP] LAPLACE_SSH_HOST is not set - skipping VPS connection test.
  echo         旧VPS 210.131.215.116 は解約済みです。新しい踏み台を LAPLACE_SSH_HOST に設定してください。
) else (
  echo  [INFO] Testing VPS connection to %LAPLACE_SSH_HOST% ...
  ssh -i "%KEY_DST%" -o BatchMode=yes -o ConnectTimeout=10 %LAPLACE_SSH_HOST% "echo OK" > nul 2>&1
  if errorlevel 1 (
    echo  [WARNING] VPS connection failed. Please check your network.
  ) else (
    echo  [OK] VPS connection verified
  )
)

echo.
echo  Setup complete! Please run LAPLACE.exe
echo.
pause
