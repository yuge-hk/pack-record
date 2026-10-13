param([string]$FFmpegDirectory = (Join-Path $PSScriptRoot "tools"))
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
if ($env:OS -ne "Windows_NT") { throw "请在 Windows 上构建 Windows EXE" }
foreach ($tool in @("ffmpeg.exe", "ffprobe.exe")) {
    if (-not (Test-Path (Join-Path $FFmpegDirectory $tool))) {
        throw "缺少 $tool，请将 FFmpeg 7+ 工具放入 tools 文件夹，或指定 -FFmpegDirectory"
    }
}
$ffmpeg = Join-Path $FFmpegDirectory "ffmpeg.exe"
$version = & $ffmpeg -version 2>&1
if ($LASTEXITCODE -ne 0) { throw "FFmpeg 无法运行" }
$options = (& $ffmpeg -hide_banner -h full 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0 -or $options -notmatch 'stats_mux_pre_fmt') {
    throw "FFmpeg 缺少音视频数据包监测选项，请使用 FFmpeg 7+"
}
$encoders = (& $ffmpeg -hide_banner -encoders 2>&1) -join "`n"
if ($LASTEXITCODE -ne 0 -or $encoders -notmatch '\bhevc_qsv\b' -or $encoders -notmatch '\bh264_qsv\b') {
    throw "此 FFmpeg 构建缺少 H.264/H.265 QSV 编码器"
}
python -m pip install -r requirements-build.txt
if ($LASTEXITCODE -ne 0) { throw "构建依赖安装失败" }
python -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "pack-record" --version-file "build\windows_version_info.txt" "main.py"
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败" }
$release = Join-Path "dist" "pack-record-v2.0.0-windows-x64"
New-Item -ItemType Directory -Force -Path $release | Out-Null
Copy-Item "dist\pack-record.exe" $release -Force
Copy-Item "README.md" $release -Force
$releaseTools = Join-Path $release "tools"
New-Item -ItemType Directory -Force -Path $releaseTools | Out-Null
Copy-Item (Join-Path $FFmpegDirectory "*") $releaseTools -Recurse -Force
Compress-Archive -Path "$release\*" -DestinationPath "dist\pack-record-v2.0.0-windows-x64.zip" -Force
Write-Host "Build complete: dist\pack-record-v2.0.0-windows-x64.zip"
