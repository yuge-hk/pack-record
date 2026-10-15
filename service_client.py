"""Local service adapter for the existing Qt controller interface."""
import json
import threading
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

from recorder_service import DEFAULT_PORT
from ffmpeg_backend import FRAME_BYTES


class ServiceClient:
    def __init__(self, token, port=DEFAULT_PORT, base_url=None):
        self.base = (base_url or f'http://127.0.0.1:{port}').rstrip('/')
        self.token = token
        self.opener = build_opener(ProxyHandler({}))

    def request(self, path, values=None, raw=False):
        body = None if values is None else json.dumps(values).encode()
        request = Request(self.base + path, data=body, headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        try:
            with self.opener.open(request, timeout=2) as response:
                data = response.read()
                return (response.headers.get('X-Frame-ID'), data) if raw else json.loads(data)
        except HTTPError as error:
            try:
                message = json.loads(error.read()).get('error', str(error))
            except (ValueError, OSError):
                message = str(error)
            raise RuntimeError(message) from None
        except (URLError, OSError) as error:
            raise RuntimeError(f'无法连接本机录像服务：{error}') from None

    def command(self, action, values=None):
        return self.request('/v1/commands/' + action, values or {})['result']


class RemoteJobs:
    def __init__(self, controller):
        self.controller = controller

    def count(self):
        return self.controller.status['recovery_count']

    def consume_warnings(self):
        return []


class RemoteController:
    """Polling never blocks Qt painting; closing this adapter leaves capture running."""
    remote = True

    def __init__(self, settings, base_dir, notify, client):
        self.client, self.notify = client, notify
        self.status = client.request('/v1/status')
        self.settings = self.status['settings']
        self.jobs = RemoteJobs(self)
        self.manager = RemoteManager(client)
        self.sequence = self.status['sequence']
        self.session = object() if self.status['snapshot']['session_active'] else None
        self.frame, self.frame_id = None, None
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._poll, name='service-client', daemon=True)
        self.thread.start()

    def _poll(self):
        disconnected = False
        self.disconnected = False
        while not self.stop_event.wait(0.2):
            try:
                status = self.client.request(f'/v1/status?after={self.sequence}')
                self.status = status
                self.settings = status['settings']
                self.sequence = status['sequence']
                self.session = True if status['snapshot']['session_active'] else None
                for event in status['events']:
                    if event['kind'] != 'state':
                        self.notify(event['kind'], event['data'])
                self.notify('state', status['snapshot'])
                if disconnected:
                    self.notify('log', '已重新连接本机录像服务')
                disconnected = False
                self.disconnected = False
                if self.settings.get('role') != 'viewer':
                    frame_id, frame = self.client.request('/v1/frame', raw=True)
                    self.frame_id, self.frame = frame_id, frame if len(frame) == FRAME_BYTES else None
            except Exception as error:
                self.frame = None
                if not disconnected:
                    self.notify('log', str(error))
                disconnected = True
                self.disconnected = True
                snapshot = dict(self.status['snapshot'], state='disconnected', error='与录像服务断开，正在重连；请勿重复扫码',
                                started_monotonic=None)
                self.notify('state', snapshot)

    def snapshot(self):
        snapshot = dict(self.status['snapshot'])
        if getattr(self, 'disconnected', False):
            snapshot.update(state='disconnected', started_monotonic=None)
        return snapshot

    def latest_frame(self, previous_session, previous_serial):
        frame_id, frame = self.frame_id, self.frame
        return self, frame_id, frame if frame_id != previous_serial else None

    def _command(self, action, values=None):
        try:
            return self.client.command(action, values)
        except RuntimeError as error:
            self.notify('log', str(error))
            self.notify('beep', None)
            return False

    def scan(self, tracking):
        return self._command('scan', {'tracking': tracking})

    def stop(self):
        return self._command('stop')

    def preview(self):
        return self._command('preview')

    def recover(self):
        return self._command('recover')

    def retry_fault(self):
        return self._command('retry_fault')

    def configure(self, settings):
        self.settings = self.client.command('configure', settings)

    def close(self):
        self.stop_event.set()


class RemoteSettingsStore:
    """Service owns persistence; desktop must not write service files."""
    warnings = []

    def __init__(self, client, base_dir):
        from recorder_settings import RecorderSettingsStore
        self.client = client
        self.validator = RecorderSettingsStore(base_dir)

    def load(self):
        return self.client.request('/v1/status')['settings']

    def validate(self, values):
        return self.validator.validate(values)

    def save(self, values):
        return dict(values)  # configure() already persisted through the service.


class RemoteManager:
    def __init__(self, client):
        self.client = client

    def search(self, **kwargs):
        from urllib.parse import urlencode
        names = {'date_from': 'from', 'date_to': 'to', 'include_deleted': 'deleted'}
        return self.client.request('/v1/library?' + urlencode({names.get(k, k): v for k, v in kwargs.items() if v != ''}))

    def status(self):
        return self.client.request('/v1/management')

    def create_export(self, record_id, start=0, end=None):
        return self.client.request('/v1/exports', dict(record_id=record_id, start=start, end=end))

    def export(self, identifier):
        return self.client.request('/v1/exports/' + identifier)

    def media_ticket(self, record_id=None, export_id=None, test_id=None):
        result = self.client.request('/v1/playback', dict(record_id=record_id, export_id=export_id, test_id=test_id))
        result['url'] = self.client.base + result['url']
        return result

    def release_media(self, ticket):
        return self.client.request('/v1/playback/release', dict(ticket=ticket['url'].rsplit('/', 1)[1]))

    def cleanup(self):
        return self.client.request('/v1/cleanup', {})

    def queue_uploads(self):
        return self.client.request('/v1/uploads/queue', {})

    def retry_uploads(self):
        return self.client.request('/v1/uploads/retry', {})

    def save_orders(self, orders):
        return self.client.request('/v1/orders', dict(orders=orders))

    def issue_device(self, station_id, name):
        return self.client.request('/v1/devices', dict(station_id=station_id, name=name))

    def revoke_device(self, station_id):
        return self.client.request('/v1/devices/revoke', dict(station_id=station_id))

    def viewer_access(self):
        return self.client.request('/v1/access', {})

    def diagnostic(self):
        return self.client.request('/v1/setup/test', {})

    def test_result(self, identifier):
        return self.client.request('/v1/tests/' + identifier)

    def complete_setup(self, test_id=None):
        return self.client.request('/v1/setup/complete', dict(test_id=test_id))
