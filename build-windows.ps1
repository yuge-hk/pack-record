$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

python -m pip install `
    --index-url "https://pypi.tuna.tsinghua.edu.cn/simple" `
    -r requirements-build.txt
python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name "ecom-love-obs" `
    --version-file "build\windows_version_info.txt" `
    --collect-all "obsws_python" `
    "main.py"

Write-Host "Build complete: dist\ecom-love-obs.exe"
