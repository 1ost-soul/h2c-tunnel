@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [setup] creating virtual environment .venv ...
    python -m venv .venv
)

echo [setup] installing dependencies ...
".venv\Scripts\python.exe" -m pip install --upgrade pip -q
".venv\Scripts\python.exe" -m pip install h2 -q

echo.
echo [h2c] proxy listening on 127.0.0.1:34132
echo [h2c] edit config.json to set your VPS address and mode: ipv6 / ipv4
echo [h2c] press Ctrl+C or Ctrl+Z to stop and release port
echo.
".venv\Scripts\python.exe" h2c_proxy.py
echo [h2c] script exited, port 34132 released.
