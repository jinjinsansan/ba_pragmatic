@echo off
setlocal
rem == BACOPY engine swap (user06 Pragmatic uId fix) ==
rem Run AFTER pressing STOP in the GUI (engine exe must not be running).
set "SRC=%USERPROFILE%\bacopy_engine_uidfix.exe"
set "DST=%USERPROFILE%\AppData\Local\Programs\bacopy-copytrade-gui\resources\engine\bacopy_engine.exe"
echo ============================================
echo  BACOPY engine swap (uId fix)
echo ============================================
if not exist "%SRC%" (
  echo ERROR: new engine not found: %SRC%
  echo The transfer may still be running. Wait and retry.
  pause
  exit /b 1
)
echo Backing up current engine to bacopy_engine.exe.bak_uidfix ...
copy /Y "%DST%" "%DST%.bak_uidfix" >nul
if errorlevel 1 (
  echo ERROR: cannot write engine folder. Is the bot still running?
  echo Press STOP in the GUI first, then run this again.
  pause
  exit /b 1
)
echo Installing new engine ...
copy /Y "%SRC%" "%DST%" >nul
if errorlevel 1 (
  echo ERROR: copy failed. The engine is probably still running.
  echo Press STOP in the GUI, wait 5 seconds, then run this again.
  pause
  exit /b 1
)
echo.
echo DONE. New engine installed.
echo Next: press START in the GUI, then place ONE manual bet to teach
echo the bot your own Pragmatic uId. After that, auto bets will be accepted.
pause
