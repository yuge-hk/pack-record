"""Fetch a pinned Windows FFmpeg build for CI, never during recorder startup."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
from urllib.request import urlopen
import zipfile

VERSION = '8.1.2'
URL = f'https://www.gyan.dev/ffmpeg/builds/packages/ffmpeg-{VERSION}-essentials_build.zip'
SHA256 = 'db580001caa24ac104c8cb856cd113a87b0a443f7bdf47d8c12b1d740584a2ec'


def verify_archive(path, expected=SHA256):
    with Path(path).open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected:
        raise ValueError(f'FFmpeg SHA256 mismatch: expected {expected}, got {actual}')


def unpack_archive(path, destination):
    """Preserve release notices/docs and both required programs, omit ffplay."""
    destination = Path(destination)
    with zipfile.ZipFile(path) as archive:
        files = []
        for member in archive.infolist():
            relative = PurePosixPath(member.filename)
            if relative.is_absolute() or '..' in relative.parts or '\\' in member.filename or ':' in member.filename:
                raise ValueError('Unsafe FFmpeg archive path')
            if member.is_dir():
                continue
            if len(relative.parts) < 2:
                raise ValueError('Expected a release directory inside FFmpeg archive')
            relative = PurePosixPath(*relative.parts[1:])
            if relative.parts[0] == 'bin':
                if relative.name not in ('ffmpeg.exe', 'ffprobe.exe'):
                    continue
                relative = PurePosixPath(relative.name)
            files.append((member, relative))
        required = {'ffmpeg.exe', 'ffprobe.exe'}
        names = [str(relative) for _, relative in files]
        if not required <= set(names) or len(names) != len(set(names)):
            raise ValueError('Missing tools or duplicate FFmpeg archive entries')
        destination.mkdir(parents=True, exist_ok=True)
        for member, relative in files:
            target = destination.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(member) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parents[1] / 'tools')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='pack-record-ffmpeg-') as directory:
        archive = Path(directory) / 'ffmpeg.zip'
        with urlopen(URL, timeout=120) as response, archive.open('wb') as target:
            shutil.copyfileobj(response, target)
        verify_archive(archive)
        unpack_archive(archive, args.output)
    (args.output / 'FFMPEG-BUILD.json').write_text(json.dumps(
        dict(version=VERSION, url=URL, sha256=SHA256, provider='GyanD/codexffmpeg'), indent=2), encoding='utf-8')
    print(f'FFmpeg {VERSION} verified and extracted to {args.output}')


if __name__ == '__main__':
    main()
