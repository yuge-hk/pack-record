"""Resolve the offline Vue build for both source runs and PyInstaller packages."""
from pathlib import Path
import sys
from urllib.parse import unquote


def web_root():
    if getattr(sys, 'frozen', False):
        return Path(sys._MEIPASS) / 'webui'
    return Path(__file__).resolve().parent / 'web' / 'dist'


def asset_path(url_path):
    root = web_root().resolve()
    path = root / ('index.html' if url_path == '/' else unquote(url_path).lstrip('/'))
    path = path.resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise FileNotFoundError('页面资源不存在')
    return path
