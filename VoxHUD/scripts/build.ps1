param(
    [switch]$OneFile
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) { $python = "python" }

& $python -m pip install "pyinstaller>=6.10"
$mode = if ($OneFile) { "--onefile" } else { "--onedir" }
$paths = @("--paths", ".")
if (Test-Path "_vendor") {
    $paths += @("--paths", "_vendor")
}

& $python -m PyInstaller `
    --noconfirm --clean --windowed $mode `
    --name VoxHUD `
    --icon "assets\voxhud.ico" `
    --add-data "assets;assets" `
    --collect-all pywebrtc_audio `
    --collect-all pystray `
    --collect-all PIL `
    --hidden-import "webrtcvad" `
    --hidden-import "websockets.asyncio.client" `
    --hidden-import "websockets.asyncio.server" `
    @paths `
    "run.py"

Write-Host ""
Write-Host "构建完成：dist\VoxHUD\VoxHUD.exe" -ForegroundColor Green
