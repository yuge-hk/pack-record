#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
if [[ "$(uname -s)" != "Linux" ]]; then
    echo '请在 Linux 上构建 Linux 可执行文件' >&2
    exit 1
fi
command -v ffmpeg >/dev/null
command -v ffprobe >/dev/null
npm --prefix web ci --no-fund --no-audit
npm --prefix web run build
python3 -m pip install -r requirements-build.txt -c .github/build-constraints.txt
python3 -m PyInstaller --noconfirm --clean --onedir --specpath build --name pack-record --add-data "$PWD/web/dist:webui" main.py
dist/pack-record/pack-record --self-test
python3 packaging/build_debian.py --binary-dir dist/pack-record --output dist/releases
echo 'Build complete: dist/releases（Debian 13 amd64，使用系统 FFmpeg）'
