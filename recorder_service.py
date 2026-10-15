"""Local authenticated recorder service. No Qt or third-party HTTP dependency."""
import argparse
from collections import deque
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import signal
import shutil
import threading

from recorder_settings import RecorderSettingsStore, runtime_dir
from recording_controller import RecordingController
from station_api import StationAPIMixin
from urllib.parse import urlsplit

DEFAULT_PORT = 18765
MAX_REQUEST = 64 * 1024


class DataLock:
    """OS-owned lock, released even after a process crash (Linux and Windows)."""
    def __init__(self, directory):
        self.path = Path(directory) / 'service.lock'
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0)
                self.file.write(b'0')
                self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            self.file = None
            raise RuntimeError('该数据目录已有录像进程运行') from None
        return self

    def __exit__(self, *_):
        if self.file:
            self.file.close()
            self.file = None


def load_token(directory, name='service.token'):
    path = Path(directory) / name
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(secrets.token_urlsafe(32))
    if os.name != 'nt':
        path.chmod(0o640)  # Members of the service group may control this station.
    token = path.read_text().strip()
    if len(token) < 32:
        raise ValueError('service.token 无效（至少 32 个字符）')
    return token


class RecorderService:
    def __init__(self, directory, controller_factory=RecordingController, controller=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store = RecorderSettingsStore(self.directory)
        self.events = deque(maxlen=256)
        self.sequence = 0
        self.event_lock = threading.Lock()
        self.command_lock = threading.Lock()
        if controller is None:
            self.controller = controller_factory(self.store.load(), self.directory, self.notify)
        else:
            self.controller = controller
            previous_notify = controller.notify
            def relay(kind, data):
                self.notify(kind, data)
                previous_notify(kind, data)
            controller.notify = relay
        for warning in self.store.warnings + self.controller.jobs.consume_warnings():
            self.notify("log", warning)

    def notify(self, kind, data):
        if kind == 'log':
            logging.getLogger('pack-record-service').info('%s', data)
        with self.event_lock:
            self.sequence += 1
            self.events.append(dict(sequence=self.sequence, kind=kind, data=data))

    def status(self, after=0):
        with self.event_lock:
            events = [event for event in self.events if event['sequence'] > after]
            sequence = self.sequence
        with self.controller.condition:
            snapshot = self.controller.snapshot()
            snapshot['session_active'] = self.controller.session is not None
            settings = dict(self.controller.settings)
            if settings.get('upload_token'):
                settings['upload_token'] = '[已保存]'
            snapshot['can_record'] = settings['role'] in ('integrated', 'station')
        storage = {}
        for name, path in (('archive', Path(settings['record_path'])), ('staging', self.directory)):
            try:
                storage[name] = shutil.disk_usage(path).free / 1024**3
            except OSError:
                storage[name] = None
        return dict(access='admin', snapshot=snapshot, settings=settings, events=events, sequence=sequence, storage=storage,
                    recovery_count=self.controller.jobs.count())

    def command(self, action, values):
        with self.command_lock:
            if self.controller.closing:
                raise RuntimeError('录像服务正在退出')
            if action == 'scan':
                tracking = values.get('tracking')
                if not isinstance(tracking, str) or not tracking.strip() or len(tracking) > 256:
                    raise ValueError('单号必须为 1～256 个字符')
                return self.controller.scan(tracking)
            if action == 'configure':
                values = dict(values)
                if values.get('upload_token') == '[已保存]':
                    values['upload_token'] = self.controller.settings['upload_token']
                settings = self.store.validate(values)
                # Keep persistence and live configuration together against scans.
                with self.controller.condition:
                    old = dict(self.controller.settings)
                    self.controller.configure(settings)
                    try:
                        self.store.save(settings)
                    except Exception:
                        self.controller.configure(old)
                        raise
                return settings
            if action not in ('stop', 'preview', 'retry_fault', 'recover'):
                raise ValueError('未知操作')
            getattr(self.controller, action)()
            return True

    def close(self):
        self.controller.close()
        self.controller.thread.join()


class RecorderHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(self, address, service, token, viewer_token=None):
        self.service, self.token = service, token
        self.viewer_token = viewer_token or secrets.token_urlsafe(32)
        super().__init__(address, RecorderHandler)


class RecorderHandler(StationAPIMixin, BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log authentication headers or scan request bodies.

    def reply(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def authorized(self):
        self.connection.settimeout(30)
        token = self.headers.get('Authorization', '').removeprefix('Bearer ')
        self.scope, self.device_id = '', None
        if hmac.compare_digest(token.encode(), self.server.token.encode()):
            self.scope = 'admin'
        elif hmac.compare_digest(token.encode(), self.server.viewer_token.encode()):
            self.scope = 'viewer'
        else:
            self.device_id = self.server.service.controller.manager.identify_device(token)
            if self.device_id:
                self.scope = 'device'
        if not self.scope:
            self.reply(401, {'error': '需要有效的本机访问令牌'})
            return False
        path = urlsplit(self.path).path
        if self.scope == 'viewer':
            allowed = (self.command in ('GET', 'HEAD') and path in ('/v1/status', '/v1/library', '/v1/management', '/v1/node', '/v1/sync')) or (
                self.command in ('GET', 'HEAD') and path.startswith(('/v1/recordings/', '/v1/exports/'))
            ) or (self.command == 'POST' and path in ('/v1/playback', '/v1/playback/release', '/v1/exports'))
        elif self.scope == 'device':
            allowed = path == '/v1/node' or path.startswith('/v1/ingest/')
        else:
            allowed = True
        if not allowed:
            self.reply(403, {'error': '当前密钥没有此操作权限'})
        return allowed

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == '/' or path.startswith('/assets/'):
            from web_ui import asset_path
            import mimetypes
            try:
                resource = asset_path(path)
                body = resource.read_bytes()
            except (OSError, ValueError):
                self.reply(503 if path == '/' else 404, {'error': 'WebUI 尚未构建，请在源码目录执行 npm --prefix web ci 和 npm --prefix web run build'})
                return
            self.send_response(200)
            self.send_header('Content-Type', mimetypes.guess_type(resource.name)[0] or 'application/octet-stream')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-cache' if path == '/' else 'public, max-age=31536000, immutable')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; media-src 'self' blob:; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            if self.command != 'HEAD':
                self.wfile.write(body)
            return
        if path.startswith('/media/'):
            try:
                self.send_file(self.server.service.controller.manager.resolve_ticket(path.rsplit('/', 1)[1]),
                               'download=1' in urlsplit(self.path).query)
            except (OSError, PermissionError):
                self.reply(404, {'error': '播放链接无效或已过期'})
            return
        if not self.authorized():
            return
        try:
            if self.station_get():
                return
            if self.scope == 'viewer':
                result = self.server.service.status()
                result['events'] = []
                result['access'] = 'viewer'
                result['settings'] = {key: value for key, value in result['settings'].items()
                                      if key in ('station_id', 'station_name', 'setup_complete', 'preview_enabled')}
                result['settings']['role'] = 'viewer'
                if result['snapshot'].get('error'):
                    result['snapshot']['error'] = '工位出现故障，请联系管理员检查'
                result['storage'] = {}
                result['snapshot']['can_record'] = False
                self.reply(200, result)
                return
            if self.path.startswith('/v1/status?after='):
                self.reply(200, self.server.service.status(int(self.path.split('=', 1)[1])))
            elif self.path == '/v1/status':
                self.reply(200, self.server.service.status())
            elif self.path == '/v1/frame':
                controller = self.server.service.controller
                session, serial, frame = controller.latest_frame(None, 0)
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(frame) if frame else 0))
                self.send_header('Cache-Control', 'no-store')
                self.send_header('X-Frame-ID', f'{id(session)}:{serial}')
                self.end_headers()
                if frame and self.command != 'HEAD':
                    self.wfile.write(frame)
            else:
                self.reply(404, {'error': '接口不存在'})
        except (ValueError, TypeError, KeyError) as error:
            self.reply(400, {'error': str(error)})
        except (FileNotFoundError, PermissionError) as error:
            self.reply(404, {'error': str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            logging.getLogger('pack-record-service').exception('Status request failed')
            self.reply(500, {'error': '服务内部错误，请检查服务日志'})

    def do_POST(self):
        if not self.authorized():
            return
        try:
            if self.station_post():
                return
            length = int(self.headers.get('Content-Length', '0'))
            if not 0 < length <= MAX_REQUEST:
                self.reply(413, {'error': '请求大小无效'})
                return
            values = json.loads(self.rfile.read(length))
            if not isinstance(values, dict):
                raise ValueError('请求必须为 JSON 对象')
            if not self.path.startswith('/v1/commands/'):
                self.reply(404, {'error': '接口不存在'})
                return
            result = self.server.service.command(self.path[len('/v1/commands/'):], values)
            self.reply(200, {'result': result})
        except (ValueError, TypeError, KeyError) as error:
            self.reply(400, {'error': str(error)})
        except (RuntimeError, OSError) as error:
            self.reply(409, {'error': str(error)})
        except Exception:
            logging.getLogger('pack-record-service').exception('Command failed')
            self.reply(500, {'error': '服务内部错误，请检查服务日志'})


def main(argv=None):
    parser = argparse.ArgumentParser(description='PackRecord local recording service')
    parser.add_argument('--data-dir', type=Path, default=runtime_dir())
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'))
    args = parser.parse_args(argv)
    args.data_dir.mkdir(parents=True, exist_ok=True)
    with DataLock(args.data_dir):
        log_dir = args.data_dir / 'logs'
        log_dir.mkdir(exist_ok=True)
        handler = RotatingFileHandler(log_dir / 'service.log', maxBytes=5 * 1024**2,
                                      backupCount=5, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger = logging.getLogger('pack-record-service')
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        service = RecorderService(args.data_dir)
        server = None
        stopping = threading.Event()
        def stop(*_):
            if not stopping.is_set():
                stopping.set()
                # shutdown must run outside the serve_forever thread.
                threading.Thread(target=server.shutdown, daemon=True).start()
        try:
            server = RecorderHTTPServer((args.host or service.controller.settings['web_bind'], args.port), service,
                                        load_token(args.data_dir), load_token(args.data_dir, 'viewer.token'))
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            logger.info('Service listening on %s:%s', server.server_address[0], args.port)
            server.serve_forever(poll_interval=0.2)
        finally:
            if server:
                server.server_close()
            service.close()
            logger.removeHandler(handler)
            handler.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
