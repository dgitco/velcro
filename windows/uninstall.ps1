# Remove velcro (app and autostart). Settings, log and imported recordings stay.
Get-Process velcro -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item (Join-Path ([Environment]::GetFolderPath('Startup')) 'velcro.lnk') -ErrorAction SilentlyContinue
Remove-Item (Join-Path $env:LOCALAPPDATA 'velcro\app') -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'velcro removed.'
