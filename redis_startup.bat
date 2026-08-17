@echo off
echo Starting WSL...
wsl.exe -d Ubuntu --exec /bin/true

timeout /t 2 /nobreak >nul

echo Checking Redis...
wsl.exe -d Ubuntu -- redis-cli ping

exit