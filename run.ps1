Set-Location $PSScriptRoot

if (-not (Test-Path '.\.venv\Scripts\python.exe')) {
    Write-Host '[setup] creating virtual environment .venv ...'
    python -m venv .venv
}

Write-Host '[setup] installing dependencies ...'
& '.\.venv\Scripts\python.exe' -m pip install --upgrade pip -q
& '.\.venv\Scripts\python.exe' -m pip install h2 -q

Write-Host ''
Write-Host '[h2c] proxy listening on 127.0.0.1:34132'
Write-Host '[h2c] edit config.json to set your VPS address and mode: ipv6 / ipv4'
Write-Host '[h2c] press Ctrl+C or Ctrl+Z to stop and release port'
Write-Host ''
try {
    & '.\.venv\Scripts\python.exe' h2c_proxy.py
} finally {
    Write-Host '[h2c] script exited, port 34132 released.'
}
