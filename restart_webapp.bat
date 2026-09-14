@echo off
echo Restarting Mountain House dashboard...
schtasks /End /TN "MountainHouseWebapp"
timeout /t 2 /nobreak >nul
schtasks /Run /TN "MountainHouseWebapp"
echo Done.
pause