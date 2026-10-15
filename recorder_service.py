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


def load_token(directory):
    path = Path(directory) / 'service.token'
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
    def __init__(self, directory, controller_factory=RecordingController):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.store = RecorderSettingsStore(self.directory)
        self.events = deque(maxlen=256)
        self.sequence = 0
        self.event_lock = threading.Lock()
        self.command_lock = threading.Lock()
        self.controller = controller_factory(self.store.load(), self.directory, self.notify)
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
        storage = {}
        for name, path in (('archive', Path(settings['record_path'])), ('staging', self.directory)):
            try:
                storage[name] = shutil.disk_usage(path).free / 1024**3
            except OSError:
                storage[name] = None
        return dict(snapshot=snapshot, settings=settings, events=events, sequence=sequence, storage=storage,
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

    def __init__(self, address, service, token):
        self.service, self.token = service, token
        super().__init__(address, RecorderHandler)


class RecorderHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass  # Never log authentication headers or scan request bodies.

    def reply(self, code, value):
        body = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(body)

    def authorized(self):
        self.connection.settimeout(5)
        if not hmac.compare_digest(self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + self.server.token).encode('utf-8')):
            self.reply(401, {'error': '需要有效的本机访问令牌'})
            return False
        return True

    def do_GET(self):
        if not self.authorized():
            return
        try:
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
                if frame:
                    self.wfile.write(frame)
            else:
                self.reply(404, {'error': '接口不存在'})
        except (ValueError, TypeError) as error:
            self.reply(400, {'error': str(error)})
        except Exception:
            logging.getLogger('pack-record-service').exception('Status request failed')
            self.reply(500, {'error': '服务内部错误，请检查服务日志'})

    def do_POST(self):
        if not self.authorized():
            return
        try:
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
        except (ValueError, TypeError) as error:
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
            server = RecorderHTTPServer(('127.0.0.1', args.port), service, load_token(args.data_dir))
            signal.signal(signal.SIGINT, stop)
            signal.signal(signal.SIGTERM, stop)
            logger.info('Service listening on 127.0.0.1:%s', args.port)
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
