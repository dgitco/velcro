# Build velcro.exe (needs Python 3 with PyInstaller: py -m pip install pyinstaller). Output: dist\velcro.exe
Set-Location $PSScriptRoot
py -m PyInstaller --onefile --noconsole --name velcro tray_win.py --paths .
