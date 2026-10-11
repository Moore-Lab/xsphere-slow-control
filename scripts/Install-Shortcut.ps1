<#
.SYNOPSIS
    Create Desktop and Start Menu shortcuts for the xsphere slow control
    service GUI.

.DESCRIPTION
    Points the shortcut at scripts\slowcontrol-gui.bat in this repo, with the
    repo root as the working directory. Re-running overwrites the existing
    shortcuts, so it is safe to run again after moving the repo.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\Install-Shortcut.ps1

.EXAMPLE
    # Desktop only, custom name
    .\scripts\Install-Shortcut.ps1 -Name "Slow Control" -NoStartMenu

.EXAMPLE
    # Remove them again
    .\scripts\Install-Shortcut.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string] $Name = "xSphere Slow Control",
    [switch] $NoDesktop,
    [switch] $NoStartMenu,
    [switch] $Uninstall
)

$ErrorActionPreference = 'Stop'

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$repoRoot  = Split-Path -Parent $scriptDir
$target    = Join-Path $scriptDir 'slowcontrol-gui.bat'

$desktopLnk   = Join-Path ([Environment]::GetFolderPath('Desktop')) "$Name.lnk"
$startMenuDir = Join-Path ([Environment]::GetFolderPath('Programs')) 'xsphere'
$startLnk     = Join-Path $startMenuDir "$Name.lnk"

if ($Uninstall) {
    foreach ($p in @($desktopLnk, $startLnk)) {
        if (Test-Path $p) { Remove-Item $p -Force; Write-Host "Removed $p" }
    }
    # -Force on the enumeration so a hidden desktop.ini is seen, and -Recurse
    # on the delete so removing a folder that holds one never throws.
    if ((Test-Path $startMenuDir) -and -not (Get-ChildItem $startMenuDir -Force)) {
        Remove-Item $startMenuDir -Recurse -Force
    }
    Write-Host "Done."
    return
}

if (-not (Test-Path $target)) {
    throw "Launcher not found at $target - is the repo complete?"
}

# The repo's own icon: the xSphere DAQ glyph with a cyan beam instead of the
# red one, so the two shortcuts read as a pair but are not mistaken for each
# other. Fall back to a stock gauge/monitor glyph if the file has gone missing.
$iconPath = Join-Path $scriptDir 'xsphere-slowcontrol.ico'
if (-not (Test-Path $iconPath)) {
    $iconPath = "$env:SystemRoot\System32\imageres.dll,109"
}

$shell = New-Object -ComObject WScript.Shell

function New-Lnk([string] $Path) {
    $parent = Split-Path -Parent $Path
    if (-not (Test-Path $parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }
    $lnk = $shell.CreateShortcut($Path)
    # Point straight at the .bat. Going via cmd.exe would need three layers of
    # quoting for no benefit; the batch file launches pythonw and exits, so the
    # shim window closes immediately and WindowStyle 7 keeps it out of sight.
    $lnk.TargetPath       = $target
    $lnk.WorkingDirectory = $repoRoot
    $lnk.IconLocation     = $iconPath
    $lnk.Description      = "Start / stop / restart the xsphere slow control services on xbox-pi"
    $lnk.WindowStyle      = 7      # minimised: the cmd shim closes immediately
    $lnk.Save()
    Write-Host "Created $Path"
}

if (-not $NoDesktop)   { New-Lnk $desktopLnk }
if (-not $NoStartMenu) { New-Lnk $startLnk }

Write-Host ""
Write-Host "Shortcut target : $target"
Write-Host "Working dir     : $repoRoot"
Write-Host ""
Write-Host "Before the GUI can control the Pi, set up key-based SSH:" -ForegroundColor Cyan
Write-Host "  ssh-keygen -t ed25519"
Write-Host "  type `$env:USERPROFILE\.ssh\id_ed25519.pub | ssh xbox@192.168.8.116 `"mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys`""
