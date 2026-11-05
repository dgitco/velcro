# Install velcro for the current user: copy the app, start it at sign-in, start it now. No admin needed.
# Run install.cmd (double-click) next to this file; it calls this script.
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'velcro'              # folder build (build.ps1 -> dist\velcro\velcro.exe)
if (-not (Test-Path (Join-Path $src 'velcro.exe'))) { $src = Join-Path $PSScriptRoot 'dist\velcro' }
if (-not (Test-Path (Join-Path $src 'velcro.exe'))) { throw 'velcro\velcro.exe not found next to this script (unzip the whole download first).' }
$dir = Join-Path $env:LOCALAPPDATA 'velcro\app'
$exe = Join-Path $dir 'velcro.exe'
Get-Process velcro -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Stop-Process -Force
Get-Process velcro -ErrorAction SilentlyContinue | Where-Object { $_.Path -eq $exe } | Wait-Process -Timeout 15 -ErrorAction SilentlyContinue   # 끈 직후 복사하면 exe 가 아직 잠겨 있음(11/05 윈도우 실측)
Remove-Item $dir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force $dir | Out-Null
Copy-Item (Join-Path $src '*') $dir -Recurse -Force
$lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'velcro.lnk'
$s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
$s.TargetPath = $exe; $s.WorkingDirectory = $dir; $s.Save()
Start-Process $exe
Write-Host ''
Write-Host 'velcro is installed and running: velcro icon in the notification area (bottom right). It starts at sign-in too.'
Write-Host 'Record: press ctrl+alt+r, press it again to stop. Files go to' (Join-Path $env:USERPROFILE 'Recordings')
Write-Host 'Recorder: plug it in, right-click the velcro icon > Use <drive> as recorder.'
