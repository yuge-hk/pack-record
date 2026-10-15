"""Assemble official Windows binaries; this does not cross-compile an EXE."""
import argparse
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_FILES = ('recorder_service.py', 'service_client.py', 'main.py', 'app_core.py', 'recorder_settings.py', 'ffmpeg_backend.py',
             'recording_controller.py', 'settings_dialog.py')


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def build(downloads, output):
    output.mkdir(parents=True, exist_ok=True)
    bundle = output / 'pack-record-v2.0.0-windows11-x64'
    if bundle.exists():
        raise FileExistsError(f'Output already exists: {bundle}')
    runtime = bundle / 'runtime'
    runtime.mkdir(parents=True)
    python_zip = downloads / 'python-3.13.16-embed-amd64.zip'
    with zipfile.ZipFile(python_zip) as source:
        source.extractall(runtime)
    (runtime / 'python313._pth').write_text('python313.zip\n.\n..\nLib/site-packages\nimport site\n', encoding='ascii')
    packages = runtime / 'Lib' / 'site-packages'
    packages.mkdir(parents=True)
    wheel_paths = sorted(downloads.glob('*-win_amd64.whl'))
    if len(wheel_paths) != 2:
        raise RuntimeError('Expected PySide6-Essentials and shiboken6 Windows wheels')
    for wheel in wheel_paths:
        with zipfile.ZipFile(wheel) as source:
            for member in source.infolist():
                name = member.filename
                # Keep the GUI's exact native modules plus Python support/licences.
                # QtQuick, multimedia, SQL, etc. are not imported by this app.
                if name.startswith('PySide6/'):
                    relative = name[len('PySide6/'):]
                    native = relative in {'QtCore.pyd', 'QtGui.pyd', 'QtWidgets.pyd', 'Qt6Core.dll',
                                          'Qt6Gui.dll', 'Qt6Widgets.dll', 'pyside6.abi3.dll'}
                    native |= relative.lower().startswith(('msvcp', 'vcruntime')) and relative.endswith('.dll')
                    plugin = relative in {'plugins/platforms/qwindows.dll', 'plugins/platforms/qoffscreen.dll',
                                          'plugins/platforms/qminimal.dll', 'plugins/styles/qmodernwindowsstyle.dll'}
                    support = relative.endswith('.py') or 'license' in relative.lower()
                    if not (native or plugin or support):
                        continue
                source.extract(member, packages)
    tools = bundle / 'tools'
    tools.mkdir()
    ffmpeg_zip = downloads / 'ffmpeg-8.1.2-essentials_build.zip'
    with zipfile.ZipFile(ffmpeg_zip) as source:
        for member in source.infolist():
            relative = Path(member.filename)
            if relative.name in ('ffmpeg.exe', 'ffprobe.exe'):
                with source.open(member) as input_file, (tools / relative.name).open('wb') as target:
                    shutil.copyfileobj(input_file, target)
            elif 'license' in relative.name.lower() or relative.name.lower().startswith(('copying', 'readme')):
                if not member.is_dir():
                    with source.open(member) as input_file, (tools / relative.name).open('wb') as target:
                        shutil.copyfileobj(input_file, target)
    for filename in APP_FILES:
        shutil.copy2(ROOT / filename, bundle / filename)
    shutil.copy2(ROOT / 'packaging' / 'windows_bootstrap.py', bundle / 'bootstrap.py')
    shutil.copy2(ROOT / 'README.md', bundle / 'PROJECT-README.md')
    (bundle / 'Start-PackRecord.cmd').write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\nstart "" "%~dp0runtime\\pythonw.exe" "%~dp0bootstrap.py"\r\n')
    (bundle / 'Check-PackRecord.cmd').write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\n"%~dp0runtime\\python.exe" "%~dp0bootstrap.py" --self-test\r\npause\r\n')
    (bundle / 'README.txt').write_text('PackRecord 2.0.0 — Windows 11 x64\n\n'
        '解压全部文件，双击 Start-PackRecord.cmd 启动，无需安装 Python、OBS 或 FFmpeg。\n'
        '首次启动选择摄像头与麦克风；默认 2560×1440 / 30 FPS / H.265 QSV / AAC / MKV。\n'
        'Intel QSV 需要正常安装 Intel 显卡驱动。\n'
        '设置、日志、待恢复任务和默认录像位于 %LOCALAPPDATA%\\PackRecord。\n'
        '请在设置中按实际磁盘容量选择录像目录。\n'
        'Check-PackRecord.cmd 检查运行库和编码器编译支持，不验证硬件采集。\n'
        '此包使用官方 Windows 运行库组成；发布环境不是 Windows，尚未完成 Win11 实机验收。\n', encoding='utf-8-sig')
    manifest = dict(version='2.0.0', platform='windows11-x64', format='embedded-python-portable',
                    source_commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                    python='3.13.16', qt='6.12.0', ffmpeg='8.1.2', windows_runtime_tested=False,
                    inputs={p.name: sha256(p) for p in [python_zip, ffmpeg_zip, *wheel_paths]})
    (bundle / 'BUILD-INFO.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    archive = output / (bundle.name + '.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED, compresslevel=9) as target:
        for file in sorted(bundle.rglob('*')):
            if file.is_file():
                target.write(file, file.relative_to(output))
    print(json.dumps({'archive': str(archive), 'bytes': archive.stat().st_size, 'sha256': sha256(archive)}))
    return archive


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--downloads', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    build(args.downloads, args.output)
