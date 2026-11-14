param(
    [switch]$Startup
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    throw "未找到 .venv。请先运行 scripts\setup.ps1。"
}
$target = "powershell.exe"
$arguments = "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$root\scripts\run.ps1`""

$folder = if ($Startup) {
    [Environment]::GetFolderPath("Startup")
} else {
    Join-Path ([Environment]::GetFolderPath("Programs")) "VoxHUD"
}
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$linkPath = Join-Path $folder "VoxHUD.lnk"
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($linkPath)
$shortcut.TargetPath = $target
$shortcut.Arguments = $arguments
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = (Join-Path $root "assets\voxhud.ico")
$shortcut.Description = "VoxHUD 全双工语音助手"
$shortcut.Save()
Write-Host "快捷方式已创建：$linkPath"
