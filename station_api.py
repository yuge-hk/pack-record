"""Versioned library/centre API. PackingProof-Mobile is deliberately not advertised."""
import json
import mimetypes
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit, quote

from transfer_manager import CHUNK_BYTES


class StationAPIMixin:
    def params(self):
        parsed = urlsplit(self.path)
        return parsed.path, {key: values[-1] for key, values in parse_qs(parsed.query).items()}

    def send_file(self, path, download=False):
        path = Path(path)
        with path.open('rb') as source:
            size = path.stat().st_size
            start, end = 0, size - 1
            header = self.headers.get('Range')
            code = 200
            if header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)', header)
                if not match or not any(match.groups()):
                    self.send_error(416)
                    return
                left, right = match.groups()
                if left:
                    start = int(left)
                    end = min(int(right), size - 1) if right else size - 1
                else:
                    length = int(right)
                    start = max(0, size - length)
                if start >= size or end < start or (not left and not int(right)):
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                code = 206
            self.send_response(code)
            self.send_header('Content-Type', mimetypes.guess_type(path.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(end - start + 1))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            if code == 206:
                self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
            if download:
                self.send_header('Content-Disposition', "attachment; filename*=UTF-8''" + quote(path.name))
            self.end_headers()
            if self.command == 'HEAD':
                return
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def station_get(self):
        path, query = self.params()
        manager = self.server.service.controller.manager
        if path == '/v1/node':
            s = self.server.service.controller.settings
            capabilities = ['recording-library', 'durable-sync', 'range-playback', 'clip-export']
            if s['role'] in ('integrated', 'center'):
                capabilities.append('verified-backup')
            if s['role'] in ('integrated', 'station'):
                capabilities.extend(['local-capture', 'order-announcements'])
            self.reply(200, dict(protocol='pack-record-v1', station_id=s['station_id'], name=s['station_name'],
                                 role=s['role'], capabilities=capabilities,
                                 protocol_version=1, mobile_compatibility=False))
        elif path == '/v1/sync':
            result = manager.library.changes(query.get('after', 0), query.get('limit', 200))
            for item in result['items']:
                self.public_record(item)
            self.reply(200, result)
        elif path == '/v1/library':
            result = manager.search(search=query.get('search', ''), date_from=query.get('from', ''),
                       date_to=query.get('to', ''), station=query.get('station', ''), page=int(query.get('page', 1)),
                       page_size=int(query.get('page_size', 50)), include_deleted=query.get('deleted') == '1')
            for item in result['items']:
                self.public_record(item)
            self.reply(200, result)
        elif path == '/v1/management':
            status = manager.status()
            if self.scope != 'admin':
                status = {key: status[key] for key in ('exports', 'role', 'station_id')}
                for item in status['exports']:
                    item.pop('path', None)
                    item['error'] = '导出失败，请联系管理员' if item['error'] else ''
            self.reply(200, status)
        elif re.fullmatch(r'/v1/recordings/[^/]+/file', path):
            identifier = path.split('/')[3]
            with manager.library.borrow(identifier) as record:
                self.send_file(record['path'], query.get('download') == '1')
        elif re.fullmatch(r'/v1/exports/[^/]+', path):
            job = manager.export(path.rsplit('/', 1)[1])
            job.pop('path', None)
            if self.scope != 'admin' and job['error']:
                job['error'] = '导出失败，请联系管理员'
            self.reply(200, job)
        elif re.fullmatch(r'/v1/exports/[^/]+/file', path):
            job = manager.export(path.split('/')[3])
            if job['state'] != 'ready':
                raise ValueError('导出尚未完成')
            self.send_file(job['path'], query.get('download') == '1')
        elif re.fullmatch(r'/v1/tests/[^/]+', path):
            result = dict(manager.test_result(path.rsplit('/', 1)[1]))
            result.pop('path', None)
            self.reply(200, result)
        else:
            return False
        return True

    @staticmethod
    def public_record(item):
        for key in ('path', 'remote_url', 'mtime_ns'):
            item.pop(key, None)
        item['capture'] = {key: value for key, value in item.get('capture', {}).items()
                           if key in ('interrupted', 'encoder', 'audio_enabled')}

    def read_json(self, limit=64 * 1024):
        length = int(self.headers.get('Content-Length', 0))
        if not 0 < length <= limit:
            raise ValueError('请求过大或请求体为空')
        value = json.loads(self.rfile.read(length))
        if not isinstance(value, dict):
            raise ValueError('请求必须为 JSON 对象')
        return value

    def station_post(self):
        path, query = self.params()
        manager = self.server.service.controller.manager
        if path == '/v1/ingest/start':
            result = manager.transfers.start_incoming(self.read_json(), self.device_id)
        elif re.fullmatch(r'/v1/ingest/[a-f0-9]{32}/chunk', path):
            size = int(self.headers.get('Content-Length', 0))
            if not 0 < size <= CHUNK_BYTES:
                raise ValueError('上传分块大小无效')
            result = manager.transfers.receive_chunk(path.split('/')[3], self.device_id,
                     int(self.headers.get('X-Offset', -1)), self.rfile.read(size), self.headers.get('X-Chunk-SHA256', ''))
        elif re.fullmatch(r'/v1/ingest/[a-f0-9]{32}/complete', path):
            self.read_json()
            result = manager.transfers.complete_incoming(path.split('/')[3], self.device_id)
        elif path == '/v1/playback':
            values = self.read_json()
            if values.get('test_id') and self.scope != 'admin':
                raise PermissionError('试录仅供管理员检查')
            result = manager.media_ticket(values.get('record_id'), values.get('export_id'), values.get('test_id'))
            result['url'] = '/media/' + result.pop('ticket')
            result.pop('path', None)
        elif path == '/v1/playback/release':
            result = manager.release_media(self.read_json())
        elif path == '/v1/exports':
            values = self.read_json()
            result = manager.create_export(values['record_id'], values.get('start', 0), values.get('end'))
        elif path == '/v1/orders':
            result = manager.orders.upsert(self.read_json().get('orders'))
        elif path == '/v1/cleanup':
            self.read_json()
            result = manager.cleanup()
        elif path == '/v1/uploads/queue':
            self.read_json()
            result = manager.transfers.queue_all()
            manager.wake.set()
        elif path == '/v1/uploads/retry':
            self.read_json()
            manager.library.retry_transfers()
            manager.wake.set()
            result = True
        elif path == '/v1/devices':
            values = self.read_json()
            result = manager.issue_device(values.get('station_id'), values.get('name'))
        elif path == '/v1/devices/revoke':
            result = manager.revoke_device(self.read_json()['station_id'])
        elif path == '/v1/access':
            self.read_json()
            result = dict(viewer_token=self.server.viewer_token)
        elif path == '/v1/setup/test':
            self.read_json()
            result = manager.diagnostic()
        elif path == '/v1/setup/speech':
            self.read_json()
            from recording_library import utc_now
            self.server.service.controller.notify('announcement', dict(text='语音测试：本单有三件商品，请检查买家留言。',
                                                    warning=False, time=utc_now()))
            result = True
        elif path == '/v1/setup/devices':
            values = self.read_json()
            from ffmpeg_backend import available_encoders, camera_modes, discover_devices
            settings = dict(self.server.service.controller.settings)
            if values.get('kind') == 'modes':
                settings['camera'] = str(values.get('camera') or settings['camera'])
                result = dict(modes=camera_modes(settings))
            else:
                result = discover_devices(settings)
                result['encoders'] = sorted(available_encoders(settings) & {'h264_qsv', 'hevc_qsv', 'libx264', 'libx265'})
        elif path == '/v1/setup/complete':
            result = manager.complete_setup(self.read_json().get('test_id'))
        else:
            return False
        self.reply(200, result)
        return True
