"""Choose a volume between recordings, and clean only explicitly eligible files."""
from datetime import datetime, timedelta
from pathlib import Path
import shutil
import os
import uuid


class StorageManager:
    def __init__(self, library, base_dir):
        self.library, self.base_dir = library, Path(base_dir)

    @staticmethod
    def roots(settings):
        return list(dict.fromkeys([settings['record_path'], *settings.get('record_paths', [])]))

    def volume_valid(self, path, register=False):
        path = Path(path).resolve()
        marker = path / '.pack-record-volume'
        if not path.is_dir() or marker.is_symlink():
            return False
        with self.library.connect() as db:
            existing = db.execute('SELECT marker FROM volumes WHERE path=?', (str(path),)).fetchone()
        if existing:
            return marker.is_file() and marker.read_text().strip() == existing[0]
        if register:
            try:
                descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                value = marker.read_text().strip()
                if len(value) != 32 or any(c not in '0123456789abcdef' for c in value):
                    return False
            else:
                value = uuid.uuid4().hex
                with os.fdopen(descriptor, 'w') as stream:
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
            with self.library.connect() as db:
                db.execute('INSERT OR IGNORE INTO volumes VALUES(?,?)', (str(path), value))
        return True

    def status(self, settings):
        rows = []
        for value in self.roots(settings):
            path = Path(value)
            try:
                # Do not recreate a missing secondary mount and silently fill the system disk.
                if not self.volume_valid(path):
                    rows.append(dict(path=str(path), free_gb=None, error='目录或磁盘标识不可用，请检查挂载'))
                    continue
                free = shutil.disk_usage(path).free / 1024**3
                rows.append(dict(path=str(path), free_gb=free, error='', writable=__import__('os').access(path, 2)))
            except OSError as error:
                rows.append(dict(path=str(path), free_gb=None, error=str(error)))
        return rows

    def select(self, settings):
        roots = self.roots(settings)
        # The default local archive is created; secondary configured mounts must already exist.
        Path(roots[0]).mkdir(parents=True, exist_ok=True)
        for row in self.status(settings):
            if row['free_gb'] is not None and row['free_gb'] >= settings['disk_critical_gb'] and row.get('writable', False):
                try:
                    if self.volume_valid(row['path'], register=True):
                        return row['path']
                except OSError:
                    continue
        self.cleanup(settings, pressure=True)
        for row in self.status(settings):
            if row['free_gb'] is not None and row['free_gb'] >= settings['disk_critical_gb'] and row.get('writable', False):
                try:
                    if self.volume_valid(row['path'], register=True):
                        return row['path']
                except OSError:
                    continue
        raise OSError('所有录像目录均不可用或低于剩余空间阈值；未确认上传的录像已保留')

    def cleanup(self, settings, pressure=False):
        policy = settings.get('cleanup_policy', 'disabled')
        if policy == 'disabled':
            return dict(deleted=0, bytes=0)
        cutoff = (datetime.now() - timedelta(days=settings.get('retention_days', 30))).isoformat()[:19].replace('T', ' ')
        deleted, reclaimed = 0, 0
        with self.library.connect() as db:
            rows = [dict(row) for row in db.execute('SELECT * FROM recordings WHERE deleted_at IS NULL ORDER BY start_time,id')]
        for root in self.roots(settings):
            directory = Path(root).resolve()
            if not self.volume_valid(directory):
                continue
            for record in rows:
                if not Path(record['path']).resolve().is_relative_to(directory):
                    continue
                free = shutil.disk_usage(directory).free / 1024**3
                old = (record['end_time'] or record['start_time'] or record['created_at']).replace('T', ' ')[:19] < cutoff
                low = pressure and free < settings['disk_warn_gb']
                if not old and not low:
                    continue
                # Upload caches always require verification, including when local policy is selected.
                allow_local = policy == 'local' and settings.get('upload_kind', 'none') == 'none'
                try:
                    if self.library.delete(record['id'], 'capacity' if low else 'retention', directory, allow_local):
                        deleted += 1
                        reclaimed += record['size']
                except (OSError, ValueError, FileNotFoundError) as error:
                    self.library.audit('cleanup_failed', record['id'], str(error))
        return dict(deleted=deleted, bytes=reclaimed)
