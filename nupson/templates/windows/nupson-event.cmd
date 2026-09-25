@echo off
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "C:\ProgramData\NUPSON\Client\nupson-event.ps1"
exit /b %ERRORLEVEL%
