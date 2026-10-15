"""Checks for externally downloaded build inputs before release packaging."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import zipfile

spec = importlib.util.spec_from_file_location('fetch_windows_ffmpeg',
        Path(__file__).resolve().parents[1] / 'packaging/fetch_windows_ffmpeg.py')
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)


class WindowsBuildInputTests(unittest.TestCase):
    def test_corrupted_download_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'download.zip'
            archive.write_bytes(b'original release')
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            fetch.verify_archive(archive, digest)
            archive.write_bytes(b'changed download')
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                fetch.verify_archive(archive, digest)

    def test_release_keeps_notices_and_required_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'download.zip'
            with zipfile.ZipFile(archive, 'w') as output:
                for name in ('bin/ffmpeg.exe', 'bin/ffprobe.exe', 'bin/ffplay.exe', 'LICENSE', 'doc/ffmpeg.html'):
                    output.writestr('release/' + name, name)
            destination = Path(directory) / 'tools'
            fetch.unpack_archive(archive, destination)
            self.assertTrue((destination / 'ffmpeg.exe').is_file())
            self.assertTrue((destination / 'ffprobe.exe').is_file())
            self.assertTrue((destination / 'LICENSE').is_file())
            self.assertTrue((destination / 'doc/ffmpeg.html').is_file())
            self.assertFalse((destination / 'ffplay.exe').exists())

    def test_invalid_archive_does_not_write_outside_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'download.zip'
            destination = Path(directory) / 'tools'
            with zipfile.ZipFile(archive, 'w') as output:
                output.writestr('release/bin/ffmpeg.exe', b'program')
                output.writestr('release/../../outside', b'bad')
            with self.assertRaises(ValueError):
                fetch.unpack_archive(archive, destination)
            self.assertFalse(destination.exists())
            self.assertFalse((Path(directory) / 'outside').exists())
