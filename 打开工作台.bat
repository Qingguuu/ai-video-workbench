@echo off
chcp 65001 >nul
title 视频工作台

REM 已在项目根（本文件所在目录）下的 studio 里运行服务
cd /d "%~dp0studio"

REM 1) 服务已在运行则直接打开页面
netstat -ano | findstr ":8600" | findstr "LISTENING" >nul 2>&1
if %errorlevel%==0 goto open

echo 正在启动视频工作台服务（首次约需几秒）…
start "视频工作台服务（关闭此窗口即停止服务）" /min cmd /c "python -m uvicorn app:app --host 127.0.0.1 --port 8600"

REM 2) 等待服务就绪（最多约 20 秒）
set /a n=0
:wait
timeout /t 1 /nobreak >nul
curl -s -m 2 http://127.0.0.1:8600/api/health >nul 2>&1
if %errorlevel%==0 goto open
set /a n+=1
if %n% lss 20 goto wait
echo 启动失败：请检查 Python 环境或在本目录手动运行
echo   python -m uvicorn app:app --host 127.0.0.1 --port 8600
pause
exit /b 1

:open
start http://127.0.0.1:8600/
exit /b 0
