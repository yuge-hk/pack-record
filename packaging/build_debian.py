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
    portable = output / 'pack-record-v2.0.0-debian13-amd64'
    shutil.copytree(binary_dir, portable, copy_function=os.link)
    shutil.copy2(ROOT / 'README.md', portable / 'README.md')
    license_dir = portable / 'licenses'
    license_dir.mkdir()
    import importlib.metadata
    for name in ('PySide6-Essentials', 'shiboken6', 'pyinstaller'):
        distribution = importlib.metadata.distribution(name)
        target = license_dir / name
        target.mkdir()
        for entry in distribution.files or []:
            if 'license' in str(entry).lower() or Path(str(entry)).name.lower().startswith('copying'):
                path = Path(distribution.locate_file(entry))
                if path.is_file():
                    shutil.copy2(path, target / path.name)
    (portable / 'README.txt').write_text('PackRecord 2.0.0 — Debian 13 amd64\n\n'
        '便携包：安装系统 FFmpeg 后，运行 ./pack-record。无需 Python 或 OBS。\n'
        '安装依赖：sudo apt install ffmpeg libgl1 libegl1 libxcb-cursor0 fonts-noto-cjk pulseaudio-utils\n'
        '如有 .deb 文件，优先用 sudo apt install ./pack-record_2.0.0-1_amd64.deb 安装。\n'
        '设置、日志、待恢复任务与默认录像：${XDG_DATA_HOME:-~/.local/share}/pack-record。\n'
        '首次启动配置摄像头和麦克风，按实际磁盘容量选择录像目录。\n'
        '默认 2560×1440 / 30 FPS / H.265 QSV / AAC / MKV。QSV 需要 Intel 驱动与 render 设备访问权限。\n'
        './pack-record --self-test 检查运行库，不验证摄像头或实际 QSV 编码。\n', encoding='utf-8')
    (portable / 'BUILD-INFO.json').write_text(json.dumps(dict(version='2.0.0', target='Debian 13 amd64',
        source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
        versions={name: importlib.metadata.version(name) for name in ('PySide6-Essentials', 'shiboken6', 'pyinstaller')},
        ffmpeg='System package (7+)', hardware_capture_tested=False), indent=2), encoding='utf-8')
    archive = output / (portable.name + '.tar.gz')
    with tarfile.open(archive, 'w:gz', compresslevel=9) as target:
        target.add(portable, arcname=portable.name)
    stage = output / 'debian-stage'
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
    size = sum(p.stat().st_size for p in portable.rglob('*') if p.is_file()) // 1024
    (stage / 'DEBIAN' / 'control').write_text(f'Package: pack-record\nVersion: 2.0.0-1\n'
        f'Architecture: amd64\nMaintainer: mada <madayouxi@outlook.com>\nInstalled-Size: {size}\n'
        'Depends: libc6 (>= 2.41), libstdc++6, libgcc-s1, libgl1, libegl1, ffmpeg (>= 7:7.0), fonts-noto-cjk\n'
        'Recommends: pulseaudio-utils, v4l-utils, intel-media-va-driver\nSection: video\nPriority: optional\n'
        'Homepage: http://192.168.0.175:3000/mada/pack-record\n'
        'Description: USB camera and microphone shipping recorder\n'
        ' Cross-platform scan-to-record application using FFmpeg and Intel QSV.\n', encoding='utf-8')
    deb = output / 'pack-record_2.0.0-1_amd64.deb'
    subprocess.run(['dpkg-deb', '--root-owner-group', '--build', str(stage), str(deb)], check=True)
    print(json.dumps({'deb': str(deb), 'tar': str(archive)}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--binary-dir', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    main(args.binary_dir, args.output)
