@echo off
rem CCiteheck API 服务（HTTPS :3000），由计划任务经 run-hidden.vbs 拉起
set "ROOT=%~dp0.."
set "PYTHONPATH=%ROOT%\src;%ROOT%;%ROOT%\runtime\site-packages"
"%ROOT%\runtime\python\pythonw.exe" -m apps.api.server ^
  --cert "%USERPROFILE%\.office-addin-dev-certs\localhost.crt" ^
  --key "%USERPROFILE%\.office-addin-dev-certs\localhost.key" ^
  >> "%ROOT%\logs\api.log" 2>&1
