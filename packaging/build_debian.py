"""Package an already built native PyInstaller directory for Debian 13 amd64."""
import argparse
import json
import os
import shutil
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(binary_dir, output):
    output.mkdir(parents=True, exist_ok=True)
    portable = output / 'pack-record-v2.1.0-debian13-amd64'
    if portable.exists():
        shutil.rmtree(portable)
    shutil.copytree(binary_dir, portable, copy_function=os.link)
    shutil.copy2(ROOT / 'README.md', portable / 'README.md')
    shutil.copytree(ROOT / 'docs', portable / 'docs')
    license_dir = portable / 'licenses'
    license_dir.mkdir()
    shutil.copy2(ROOT / 'web/node_modules/vue/LICENSE', license_dir / 'Vue-LICENSE')
    import importlib.metadata
    for name in ('PySide6-Essentials', 'PySide6-Addons', 'shiboken6', 'pyinstaller'):
        distribution = importlib.metadata.distribution(name)
        target = license_dir / name
        target.mkdir()
        for entry in distribution.files or []:
            if 'license' in str(entry).lower() or Path(str(entry)).name.lower().startswith('copying'):
                path = Path(distribution.locate_file(entry))
                if path.is_file():
                    shutil.copy2(path, target / path.name)
    (portable / 'README.txt').write_text('PackRecord 2.1.0 — Debian 13 amd64\n\n'
        '便携包：安装系统 FFmpeg 后，运行 ./pack-record。无需 Python 或 OBS。\n'
        '安装依赖：sudo apt install ffmpeg libgl1 libegl1 libxcb-cursor0 fonts-noto-cjk pulseaudio-utils libspeechd2 speech-dispatcher-espeak-ng\n'
        '如有 .deb 文件，优先用 sudo apt install ./pack-record_2.1.0-1_amd64.deb 安装。\n'
        '设置、日志、待恢复任务与默认录像：${XDG_DATA_HOME:-~/.local/share}/pack-record。\n'
        '首次启动后在 http://127.0.0.1:18765 配置设备、目录并试录，密钥见数据目录 service.token。\n'
        '默认 2560×1440 / 30 FPS / H.265 QSV / AAC / MKV。QSV 需要 Intel 驱动与 render 设备访问权限。\n'
        './pack-record --self-test 检查运行库，不验证摄像头或实际 QSV 编码。\n', encoding='utf-8')
    (portable / 'BUILD-INFO.json').write_text(json.dumps(dict(version='2.1.0', target='Debian 13 amd64',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        working_tree_dirty=bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
        versions={name: importlib.metadata.version(name) for name in ('PySide6-Essentials', 'PySide6-Addons', 'shiboken6', 'pyinstaller')},
        ffmpeg='System package (7+)', hardware_capture_tested=False), indent=2), encoding='utf-8')
    archive = output / (portable.name + '.tar.gz')
    with tarfile.open(archive, 'w:gz', compresslevel=9) as target:
        target.add(portable, arcname=portable.name)
    stage = output / 'debian-stage-2.1.0-1'
    if stage.exists():
        shutil.rmtree(stage)
    (stage / 'DEBIAN').mkdir(parents=True)
    (stage / 'opt').mkdir()
    shutil.copytree(portable, stage / 'opt' / 'pack-record', copy_function=os.link)
    (stage / 'usr' / 'bin').mkdir(parents=True)
    (stage / 'usr' / 'bin' / 'pack-record').symlink_to('/opt/pack-record/pack-record')
    desktops = stage / 'usr' / 'share' / 'applications'
    desktops.mkdir(parents=True)
    (desktops / 'pack-record.desktop').write_text('[Desktop Entry]\nType=Application\nName=PackRecord\n'
        'Name[zh_CN]=打包录像\nComment=USB camera shipping recorder\nExec=pack-record\n'
        'Icon=camera-video\nTerminal=false\nCategories=AudioVideo;Video;\n', encoding='utf-8')
    (desktops / 'pack-record-service.desktop').write_text(
        '[Desktop Entry]\nType=Application\nName=PackRecord Service Client\n'
        'Name[zh_CN]=打包录像（后台服务）\n'
        'Exec=pack-record --connect --token-file /var/lib/pack-record/service.token\n'
        'Icon=camera-video\nTerminal=false\nCategories=AudioVideo;Video;\n', encoding='utf-8')
    size = sum(p.stat().st_size for p in portable.rglob('*') if p.is_file()) // 1024
    (stage / 'DEBIAN' / 'control').write_text(f'Package: pack-record\nVersion: 2.1.0-1\n'
        f'Architecture: amd64\nMaintainer: mada <madayouxi@outlook.com>\nInstalled-Size: {size}\n'
        'Depends: adduser, libxkbcommon0, libxcb-cursor0, libc6 (>= 2.41), libstdc++6, libgcc-s1, libgl1, libegl1, libgtk-3-0t64, libspeechd2, ffmpeg (>= 7:7.0), fonts-noto-cjk\n'
        'Recommends: pulseaudio-utils, v4l-utils, intel-media-va-driver, speech-dispatcher, speech-dispatcher-espeak-ng\nSection: video\nPriority: optional\n'
        'Homepage: https://github.com/yuge-hk/pack-record\n'
        'Description: USB camera and microphone shipping recorder\n'
        ' Debian scan-to-record application with a local web console using FFmpeg and Intel QSV.\n', encoding='utf-8')
    units = stage / 'usr' / 'lib' / 'systemd' / 'system'
    units.mkdir(parents=True)
    shutil.copy2(ROOT / 'packaging/systemd/pack-record.service', units / 'pack-record.service')
    scripts = {
        'postinst': """#!/bin/sh
set -e
if [ "$1" = configure ]; then
    for group in pack-record video render audio; do
        getent group "$group" >/dev/null || addgroup --system "$group"
    done
    id pack-record >/dev/null 2>&1 || adduser --system --ingroup pack-record --home /var/lib/pack-record --no-create-home pack-record
    install -d -o pack-record -g pack-record -m 0750 /var/lib/pack-record
    if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
fi
""",
        'prerm': """#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then
    if [ "$1" = remove ]; then systemctl disable --now pack-record.service; fi
    if [ "$1" = upgrade ]; then systemctl stop pack-record.service; fi
fi
""",
        'postrm': """#!/bin/sh
set -e
if [ -d /run/systemd/system ]; then systemctl daemon-reload; fi
# User configuration and recordings are deliberately preserved on removal.
""",
    }
    for name, content in scripts.items():
        script = stage / 'DEBIAN' / name
        script.write_text(content, encoding='utf-8')
        script.chmod(0o755)
    deb = output / 'pack-record_2.1.0-1_amd64.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(deb)], check=True)
    print(json.dumps({'deb': str(deb), 'tar': str(archive)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    main(args.binary_dir, args.output)
