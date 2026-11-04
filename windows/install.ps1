# Install velcro for the current user: copy velcro.exe, start it at sign-in, start it now. No admin needed.
$ErrorActionPreference = 'Stop'
$exe = Join-Path $PSScriptRoot 'velcro.exe'
if (-not (Test-Path $exe)) { $exe = Join-Path $PSScriptRoot 'dist\velcro.exe' }
if (-not (Test-Path $exe)) { throw 'velcro.exe not found next to this script (run build.ps1 first).' }
$dir = Join-Path $env:LOCALAPPDATA 'velcro\app'
New-Item -ItemType Directory -Force $dir | Out-Null
Get-Process velcro -ErrorAction SilentlyContinue | Stop-Process -Force
Get-Process velcro -ErrorAction SilentlyContinue | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue   # 끈 직후 복사하면 exe 가 아직 잠겨 있음(11/05 윈도우 실측)
Copy-Item $exe (Join-Path $dir 'velcro.exe') -Force
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'velcro.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = Join-Path $dir 'velcro.exe'; $s.Save()
Start-Process (Join-Path $dir 'velcro.exe')
Write-Host 'velcro installed. Right-click the tray icon for settings. Settings file: ' (Join-Path $env:APPDATA 'velcro\settings')
