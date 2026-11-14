param(
    [string]$Python = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Push-Location $root
try {
    if (-not (Test-Path ".venv")) {
        if ($Python) {
            & $Python -m venv .venv
        } elseif (Get-Command py -ErrorAction SilentlyContinue) {
            & py -3 -m venv .venv
        } elseif (Get-Command python -ErrorAction SilentlyContinue) {
            & python -m venv .venv
        } else {
            throw "未找到 Python。请安装 Python 3.10+ 并勾选 Add to PATH。"
        }
    }
    & ".venv\Scripts\python.exe" -m pip install --upgrade pip
    & ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    Write-Host ""
    Write-Host "安装完成。运行：" -ForegroundColor Green
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\run.ps1"
    Write-Host "  powershell -ExecutionPolicy Bypass -File scripts\run.ps1 -Console"
} finally {
    Pop-Location
}
