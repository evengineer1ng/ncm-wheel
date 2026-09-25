<#
.SYNOPSIS
  Build the NCM wheel companion into ONE executable, so a player never installs Python.

  The companion process cannot be removed -- NCM's client Lua has no sockets and no HID access, so nothing
  inside the game can touch a wheel. What CAN be removed is every sign of it: Python, pip, a venv, a console
  window and a manual start. This script does the first four; `-InstallStartup` does the fifth.

.NOTES
  Output: dist\NCM Wheel Support.exe  (windowed, no console)

  **Force output stays disarmed in the packaged build.** `--arm` is still required, exactly as it is when
  running from source, so shipping a build can never be the thing that first moves someone's wheel.

.USAGE
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-ffb-companion.ps1
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-ffb-companion.ps1 -InstallStartup
  powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build-ffb-companion.ps1 -RemoveStartup
#>
[CmdletBinding()]
param(
    [switch]$InstallStartup,
    [switch]$RemoveStartup,
    [string]$Venv = (Join-Path $PSScriptRoot '.venv')
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
$source = Join-Path $root 'ncm-wheel.py'
$name = 'NCM Wheel Support'
$exe = Join-Path $root "dist\$name.exe"

# The Startup-folder shortcut is the whole "no manual start" story, and it is deliberately a shortcut rather
# than a registry Run key or a service: a player can see it, and delete it, without being told how.
$startupLink = Join-Path ([Environment]::GetFolderPath('Startup')) "$name.lnk"

if ($RemoveStartup) {
    if (Test-Path $startupLink) { Remove-Item $startupLink -Force; Write-Output "removed: $startupLink" }
    else { Write-Output 'no startup shortcut present' }
    return
}

$python = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $python)) { throw "no venv python at $python. Create one first: py -3.12 -m venv .venv  then  .venv\Scripts\python -m pip install -r requirements.txt" }
if (-not (Test-Path $source)) { throw "companion source not found at $source" }

# PowerShell 5.1 turns a native executable's STDERR into a NativeCommandError, and with
# $ErrorActionPreference = 'Stop' that aborts the script on PyInstaller's very first INFO line -- which is
# progress output, not a failure. Exit code is the only honest signal from these tools.
function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments, [string]$What)
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $Exe @Arguments } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "$What failed (exit $LASTEXITCODE)" }
}

Write-Output 'installing build dependency (pyinstaller)...'
Invoke-Native -Exe $python -Arguments @('-m','pip','install','--quiet','--upgrade','pyinstaller') -What 'pip install pyinstaller'

# --windowed: no console. The companion is meant to be invisible; its readouts belong in the NCM panel.
# --collect-all sdl2dll: pysdl2-dll ships SDL2.dll as package data, which PyInstaller will not find alone,
# and without it the packaged build silently loses hardware discovery.
$args = @(
    '-m', 'PyInstaller', '--noconfirm', '--clean', '--onefile', '--windowed',
    '--name', $name,
    '--distpath', (Join-Path $root 'dist'),
    '--workpath', (Join-Path $env:TEMP 'ncm-ffb-build'),
    '--specpath', (Join-Path $env:TEMP 'ncm-ffb-build'),
    '--collect-all', 'sdl2dll',
    '--collect-all', 'vgamepad',
    '--add-data', ((Join-Path $root 'wheel-profiles') + ';wheel-profiles'),
    '--hidden-import', 'sdl2',
    $source
)
Write-Output "building $name..."
Invoke-Native -Exe $python -Arguments $args -What 'PyInstaller'
if (-not (Test-Path $exe)) { throw "build reported success but $exe is missing" }

$size = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Output "built: $exe  ($size MB)"

if ($InstallStartup) {
    $shell = New-Object -ComObject WScript.Shell
    $link = $shell.CreateShortcut($startupLink)
    $link.TargetPath = $exe
    $link.WorkingDirectory = Split-Path -Parent $exe
    $link.Description = 'Force feedback companion for NCM Online'
    $link.Save()
    Write-Output "startup shortcut: $startupLink"
    Write-Output 'remove it with -RemoveStartup, or just delete that file.'
}

Write-Output ''
Write-Output 'Force output is DISARMED in this build. It needs --arm, exactly as running from source does.'
Write-Output 'In game: open the NCM panel (F6) and pick WHEEL.'
