@echo off
rem ============================================================
rem  Media Manager 启动脚本
rem  自动探测 media-manager conda 环境，找不到时给出提示
rem ============================================================
cd /d "%~dp0"
setlocal

set "ENV_PY="

rem --- 常见 Miniconda / Anaconda 安装位置 ---
if exist "%USERPROFILE%\miniconda3\envs\media-manager\python.exe" set "ENV_PY=%USERPROFILE%\miniconda3\envs\media-manager\python.exe"
if "%ENV_PY%"=="" if exist "%USERPROFILE%\Miniconda3\envs\media-manager\python.exe" set "ENV_PY=%USERPROFILE%\Miniconda3\envs\media-manager\python.exe"
if "%ENV_PY%"=="" if exist "%ProgramData%\Miniconda3\envs\media-manager\python.exe" set "ENV_PY=%ProgramData%\Miniconda3\envs\media-manager\python.exe"
if "%ENV_PY%"=="" if exist "%ProgramData%\Anaconda3\envs\media-manager\python.exe" set "ENV_PY=%ProgramData%\Anaconda3\envs\media-manager\python.exe"
if "%ENV_PY%"=="" if exist "%USERPROFILE%\anaconda3\envs\media-manager\python.exe" set "ENV_PY=%USERPROFILE%\anaconda3\envs\media-manager\python.exe"
if "%ENV_PY%"=="" if exist "D:\anaconda3\envs\media-manager\python.exe" set "ENV_PY=D:\anaconda3\envs\media-manager\python.exe"

if not "%ENV_PY%"=="" (
    "%ENV_PY%" main.py
) else (
    echo.
    echo [错误] 未找到 media-manager 环境。
    echo 请确认已安装 Miniconda 并创建了 media-manager 环境：
    echo     conda create -n media-manager python=3.10
    echo 然后再运行本脚本。
)

pause
