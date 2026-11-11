# Creates a Startup-folder shortcut that launches holler at login, no console window.
# Re-run to refresh; harmless if shortcut already exists.

param(
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"

$startup = [Environment]::GetFolderPath("Startup")
$shortcut = Join-Path $startup "holler.lnk"

if ($Uninstall) {
    if (Test-Path $shortcut) {
        Remove-Item $shortcut -Force
        Write-Output "removed: $shortcut"
    } else {
        Write-Output "not installed"
    }
    return
}

$tool = Resolve-Path (Join-Path $PSScriptRoot "..")
$pythonw = Join-Path $tool ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $pythonw)) {
    Write-Error "venv not found at $pythonw — run pip install -r requirements.txt first"
    return
}

$ws = New-Object -ComObject WScript.Shell
$lnk = $ws.CreateShortcut($shortcut)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "-m holler --silent"
$lnk.WorkingDirectory = "$tool"
$lnk.IconLocation = "$pythonw,0"
$lnk.Description = "holler (push-to-talk Claude session)"
$lnk.Save()

Write-Output "installed: $shortcut"
Write-Output "launches at next login. To remove: .\install_autostart.ps1 -Uninstall"
