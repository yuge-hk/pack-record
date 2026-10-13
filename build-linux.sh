#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$(uname -s)" != "Linux" ]]; then
    echo '请在 Linux 上构建 Linux 可执行文件' >&2
    exit 1
fi
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
python3 -m pip install -r requirements-build.txt
python3 -m PyInstaller --noconfirm --clean --onefile --name pack-record main.py
echo 'Build complete: dist/pack-record（使用本机 FFmpeg 和图形库）'
