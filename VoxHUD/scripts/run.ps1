param(
    [switch]$Console,
    [switch]$Demo,
    [switch]$Settings
)

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$python = Join-Path $root ".venv\Scripts\python.exe"
$pythonw = Join-Path $root ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $python)) {
    $python = "python"
    $pythonw = "pythonw"
}

$appArgs = @("run.py")
if ($Demo) { $appArgs += "--demo" }
if ($Settings) { $appArgs += "--settings" }

if ($Console) {
    & $python @appArgs
} else {
    Start-Process -FilePath $pythonw -ArgumentList $appArgs -WorkingDirectory $root
    Write-Host "VoxHUD 已启动。默认热键：Ctrl+Alt+Space（呼出/隐藏），Ctrl+Alt+X（打断）。"
}
