"""Resumable own-protocol centre uploads and verified mounted-NAS copies."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import threading
import time
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener, HTTPRedirectHandler
import uuid

from recording_library import file_sha256, utc_now

CHUNK_BYTES = 4 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a station credential to another URL.


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class TransferManager:
    def __init__(self, library, storage, settings, base_dir):
        self.library, self.storage, self.settings = library, storage, settings
        self.base_dir = Path(base_dir)
        self.lock = threading.RLock()
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.stop_event = threading.Event()

    def target(self):
        s = self.settings()
        return dict(kind=s['upload_kind'], path=s['upload_path'], url=s['upload_url'], token=s['upload_token'])

    def queue_all(self):
        target = self.target()
        if target['kind'] == 'none':
            raise ValueError('请先设置上传目录或中心地址')
        with self.library.connect() as db:
            ids = [row[0] for row in db.execute("SELECT id FROM recordings WHERE deleted_at IS NULL AND origin='local' AND upload_state!='verified'")]
        for identifier in ids:
            self.library.enqueue(identifier, target)
        return dict(queued=len(ids))

    def request(self, target, path, values=None, data=None, headers=None):
        if values is not None:
            data = json.dumps(values).encode()
        request = Request(target['url'] + path, data=data,
                          headers={'Authorization': 'Bearer ' + target['token'],
                                   'Content-Type': 'application/json' if values is not None else 'application/octet-stream',
                                   **(headers or {})})
        try:
            with self.opener.open(request, timeout=15) as response:
                return json.load(response)
        except HTTPError as error:
            raise RuntimeError(f'中心返回 HTTP {error.code}') from None

    def run_one(self):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM transfers WHERE state='queued' AND next_attempt<=? ORDER BY created_at LIMIT 1", (time.time(),)).fetchone()
            if not row:
                return False
            job = dict(row)
            db.execute("UPDATE transfers SET state='uploading' WHERE id=?", (job['id'],))
            db.execute("UPDATE recordings SET upload_state='uploading' WHERE id=?", (job['record_id'],))
        try:
            with self.library.borrow(job['record_id']) as record:
                digest = file_sha256(record['path'])
                target = json.loads(job['target'])
                if target['kind'] == 'directory':
                    remote = self.copy_directory(job, record, digest, target)
                elif target['kind'] == 'center':
                    remote = self.upload_center(job, record, digest, target)
                else:
                    raise ValueError('未知上传类型')
                with self.library.connect() as db:
                    db.execute("UPDATE transfers SET state='verified',sha256=?,offset=?,remote_url=?,error='' WHERE id=?",
                               (digest, record['size'], remote, job['id']))
                    db.execute("UPDATE recordings SET upload_state='verified',sha256=?,remote_url=? WHERE id=?",
                               (digest, remote, record['id']))
                self.library.audit('upload_verified', record['id'], remote)
        except Exception as error:
            attempts = job['attempts'] + 1
            state = 'error' if attempts >= 8 else 'queued'
            with self.library.connect() as db:
                db.execute('UPDATE transfers SET state=?,attempts=?,next_attempt=?,error=? WHERE id=?',
                           (state, attempts, time.time() + min(1800, 2 ** attempts * 5), str(error), job['id']))
                db.execute("UPDATE recordings SET upload_state=? WHERE id=? AND upload_state!='verified'",
                           (state, job['record_id']))
        return True

    def copy_directory(self, job, record, digest, target):
        root = Path(target['path'])
        if not self.storage.volume_valid(root, register=True):
            raise OSError('上传目标未挂载或不存在')
        volume_device = root.stat().st_dev
        source = Path(record['path'])
        destination = root / record['station_id'] / (record['start_time'] or utc_now())[:10] / (record['id'] + source.suffix)
        if destination.resolve() == source.resolve():
            raise ValueError('上传目录不能与原始录像位置相同')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if destination.stat().st_size == record['size'] and file_sha256(destination) == digest:
                sync_directory(destination.parent)
                return str(destination)
            raise FileExistsError('远端同名文件内容不同，拒绝覆盖')
        if shutil.disk_usage(destination.parent).free < record['size'] + self.settings()['disk_copy_reserve_gb'] * 1024**3:
            raise OSError('上传目标剩余空间不足')
        partial = destination.with_name(destination.name + '.' + job['id'] + '.part')
        try:
            with source.open('rb') as left, partial.open('wb') as right:
                while chunk := left.read(CHUNK_BYTES):
                    if self.stop_event.is_set():
                        raise RuntimeError('上传已暂停，重启后恢复')
                    right.write(chunk)
                right.flush()
                os.fsync(right.fileno())
            if file_sha256(partial) != digest:
                raise ValueError('远端文件校验失败')
            if root.stat().st_dev != volume_device or not self.storage.volume_valid(root):
                raise OSError('上传目标磁盘已改变')
            os.replace(partial, destination)
            sync_directory(destination.parent)
            if file_sha256(destination) != digest:
                raise ValueError('远端文件校验失败')
            return str(destination)
        finally:
            partial.unlink(missing_ok=True)

    def upload_center(self, job, record, digest, target):
        metadata = {key: record[key] for key in ('id', 'barcode', 'start_time', 'end_time', 'duration', 'size',
                    'resolution', 'fps', 'station_id', 'station_name', 'capture')}
        metadata.update(sha256=digest, extension=Path(record['path']).suffix.lower())
        response = self.request(target, '/v1/ingest/start', values=metadata)
        upload_id, offset = response['upload_id'], response['offset']
        if not isinstance(offset, int) or not 0 <= offset <= record['size']:
            raise ValueError('中心返回了无效偏移')
        with open(record['path'], 'rb') as source:
            source.seek(offset)
            while chunk := source.read(CHUNK_BYTES):
                if self.stop_event.is_set():
                    raise RuntimeError('上传暂停，重启后断点恢复')
                result = self.request(target, '/v1/ingest/' + upload_id + '/chunk', data=chunk,
                                      headers={'X-Offset': str(offset), 'X-Chunk-SHA256': hashlib.sha256(chunk).hexdigest()})
                if result.get('offset') != offset + len(chunk):
                    raise ValueError('中心上传偏移确认不一致')
                offset += len(chunk)
                with self.library.connect() as db:
                    db.execute('UPDATE transfers SET offset=? WHERE id=?', (offset, job['id']))
        receipt = self.request(target, '/v1/ingest/' + upload_id + '/complete', values={})
        if (receipt.get('status') != 'verified' or receipt.get('sha256') != digest or
                receipt.get('source_id') != record['id'] or receipt.get('size') != record['size']):
            raise ValueError('中心未返回匹配的完整接收回执')
        return target['url'] + '/v1/recordings/' + receipt['record_id'] + '/file'

    def start_incoming(self, values, device_id):
        s = self.settings()
        if s['role'] not in ('integrated', 'center'):
            raise ValueError('当前节点不是保存中心')
        for name in ('id', 'station_id'):
            value = values.get(name, '')
            if not isinstance(value, str) or not 1 <= len(value) <= 80 or not all(c.isalnum() or c in '-_' for c in value):
                raise ValueError('录像或工位 ID 格式错误')
        if device_id and values['station_id'] != device_id:
            raise PermissionError('工位身份与密钥不匹配')
        digest, size = values.get('sha256', ''), values.get('size')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise ValueError('完整文件 SHA256 格式错误')
        if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= 1024**4:
            raise ValueError('文件大小无效')
        if values.get('extension') not in ('.mkv', '.mp4'):
            raise ValueError('只接收 MKV/MP4 录像')
        for name, limit in (('barcode', 256), ('station_name', 80), ('start_time', 40), ('end_time', 40), ('resolution', 40)):
            if not isinstance(values.get(name, ''), str) or len(values.get(name, '')) > limit:
                raise ValueError('录像元数据格式无效：' + name)
        for name in ('duration', 'fps'):
            value = values.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError('录像元数据格式无效：' + name)
        capture = values.get('capture', {})
        if not isinstance(capture, dict) or not isinstance(capture.get('interrupted', False), bool):
            raise ValueError('录像完整性标记格式错误')
        values = dict(values)
        values['capture'] = {key: capture[key] for key in ('interrupted', 'encoder', 'audio_enabled') if key in capture}
        with self.lock:
            with self.library.connect() as db:
                row = db.execute('SELECT * FROM incoming WHERE station_id=? AND source_id=?', (values['station_id'], values['id'])).fetchone()
            if row:
                row = dict(row)
                if row['sha256'] != digest or row['size'] != size:
                    raise ValueError('同一录像 ID 的内容发生变化')
                source = Path(row['path'])
                if row['state'] == 'verified':
                    record = self.library.record(row['record_id'])
                    if not Path(record['path']).is_file() or file_sha256(record['path']) != digest:
                        raise ValueError('中心已确认文件缺失或改变，需要人工检查')
                    offset = size
                elif source.exists():
                    offset = source.stat().st_size
                elif self.incoming_destination(row).is_file():
                    # A previous completion renamed the file before the database commit.
                    offset = size
                else:
                    if not self.storage.volume_valid(source.parent.parent):
                        raise OSError('接收盘不可用')
                    source.touch(exist_ok=False)
                    offset = 0
                return dict(upload_id=row['id'], offset=offset)
            archive = Path(self.storage.select(s))
            staging = archive / '.incoming'
            staging.mkdir(exist_ok=True)
            if shutil.disk_usage(staging).free < size + s['disk_critical_gb'] * 1024**3:
                raise OSError('中心空间不足')
            identifier = uuid.uuid4().hex
            path = staging / (identifier + '.part')
            path.touch(exist_ok=False)
            sync_directory(staging)
            with self.library.connect() as db:
                db.execute('INSERT INTO incoming(id,station_id,source_id,sha256,size,metadata,path,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                           (identifier, values['station_id'], values['id'], digest, size, json.dumps(values), str(path), time.time()))
            return dict(upload_id=identifier, offset=0)

    def incoming(self, identifier, device_id):
        with self.library.connect() as db:
            row = db.execute('SELECT * FROM incoming WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise FileNotFoundError('上传任务不存在')
        row = dict(row)
        if device_id and row['station_id'] != device_id:
            raise PermissionError('无权访问其他工位的上传任务')
        return row

    @staticmethod
    def incoming_destination(row):
        metadata = json.loads(row['metadata'])
        record_id = 'remote-' + hashlib.sha256((row['station_id'] + ':' + row['source_id']).encode()).hexdigest()
        return Path(row['path']).parent.parent / 'received' / row['station_id'] / (record_id + metadata['extension'])

    def receive_chunk(self, identifier, device_id, offset, chunk, digest):
        if not chunk or len(chunk) > CHUNK_BYTES or hashlib.sha256(chunk).hexdigest() != digest:
            raise ValueError('分块大小或校验不正确')
        with self.lock:
            row = self.incoming(identifier, device_id)
            path = Path(row['path'])
            if not self.storage.volume_valid(path.parent.parent):
                raise OSError('接收盘不可用')
            actual = path if path.exists() else self.incoming_destination(row)
            current = actual.stat().st_size
            if 0 <= offset and offset + len(chunk) <= current:
                with actual.open('rb') as source:
                    source.seek(offset)
                    if source.read(len(chunk)) == chunk:
                        return dict(offset=offset + len(chunk))
                raise ValueError('重复分块内容不一致')
            if row['state'] != 'receiving' or offset != current or offset + len(chunk) > row['size']:
                raise ValueError('上传偏移不匹配，请重新获取续传位置')
            if shutil.disk_usage(path.parent).free < len(chunk) + self.settings()['disk_critical_gb'] * 1024**3:
                raise OSError('中心剩余空间不足')
            with path.open('ab') as target:
                target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            offset += len(chunk)
            with self.library.connect() as db:
                db.execute('UPDATE incoming SET offset=?,updated_at=? WHERE id=?', (offset, time.time(), identifier))
            return dict(offset=offset)

    def complete_incoming(self, identifier, device_id):
        with self.lock:
            row = self.incoming(identifier, device_id)
            metadata = json.loads(row['metadata'])
            # The planned destination is deterministic, allowing recovery after rename.
            source = Path(row['path'])
            if not self.storage.volume_valid(source.parent.parent):
                raise OSError('接收盘不可用')
            record_id = 'remote-' + hashlib.sha256((row['station_id'] + ':' + row['source_id']).encode()).hexdigest()
            destination = self.incoming_destination(row)
            if row['state'] == 'verified':
                record = self.library.record(record_id)
                if not Path(record['path']).is_file():
                    raise FileNotFoundError('中心录像已清理或缺失，不能确认已备份')
                if file_sha256(record['path']) != row['sha256']:
                    raise ValueError('中心录像校验不匹配')
            else:
                path = source if source.exists() else destination
                if not path.is_file() or path.stat().st_size != row['size'] or file_sha256(path) != row['sha256']:
                    raise ValueError('完整文件校验失败，保留任务供检查')
                # Probe streams before the centre accepts a video as playable evidence.
                from ffmpeg_backend import run_tool
                from recorder_settings import find_tool
                code, output = run_tool([find_tool('ffprobe', self.settings()['ffprobe_path']), '-v', 'error',
                                         '-show_streams', '-show_format', '-of', 'json', str(path)], timeout=30)
                data = json.loads(output) if not code else {}
                videos = [stream for stream in data.get('streams', []) if stream.get('codec_type') == 'video']
                duration = float(data.get('format', {}).get('duration', 0))
                if not videos or not math.isfinite(duration) or duration <= 0:
                    raise ValueError('上传文件不是可验证的完整录像')
                destination.parent.mkdir(parents=True, exist_ok=True)
                if source.exists():
                    os.replace(source, destination)
                    sync_directory(destination.parent)
                    sync_directory(source.parent)
                metadata.update(id=record_id, path=str(destination), origin='remote', duration=duration)
                self.library.add(metadata)
                with self.library.connect() as db:
                    db.execute("UPDATE incoming SET state='verified',record_id=?,offset=size,updated_at=? WHERE id=?",
                               (record_id, time.time(), identifier))
                self.library.audit('received_verified', record_id, row['station_id'])
            return dict(status='verified', record_id=record_id, source_id=row['source_id'], sha256=row['sha256'], size=row['size'])
