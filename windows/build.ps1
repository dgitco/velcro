# Build velcro (needs Python 3 with PyInstaller: py -m pip install pyinstaller). Output: dist\velcro\velcro.exe (folder build;
# the one-file build self-extracts to %TEMP% and is flagged by Defender machine-learning heuristics more often).
Set-Location $PSScriptRoot
py -m PyInstaller --onedir --noconsole --name velcro tray_win.py --paths . --paths ..
