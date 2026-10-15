"""Exercise the actual HTTP boundary, service ownership and desktop detachment."""
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import threading
import time
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from recorder_service import DataLock, RecorderHTTPServer, RecorderService, load_token
from service_client import RemoteController, ServiceClient


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.service = RecorderService(self.directory.name)
        self.token = load_token(self.directory.name)
        self.server = RecorderHTTPServer(('127.0.0.1', 0), self.service, self.token)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01})
        self.thread.start()
        self.addCleanup(self.shutdown)
        self.client = ServiceClient(self.token, self.server.server_port)

    def shutdown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.service.close()

    def test_authentication_is_required_for_reads_and_commands(self):
        for data in (None, b'{}'):
            request = Request(self.client.base + '/v1/status', data=data)
            with self.assertRaises(HTTPError) as result:
                urlopen(request, timeout=2)
            self.assertEqual(result.exception.code, 401)
        with self.assertRaisesRegex(RuntimeError, '令牌'):
            ServiceClient('wrong', self.server.server_port).request('/v1/status')

    def test_settings_are_persisted_by_service_and_invalid_change_is_rejected(self):
        settings = self.client.request('/v1/status')['settings']
        settings['encoder'] = 'libx264'
        self.client.command('configure', settings)
        saved = json.loads((Path(self.directory.name) / 'recorder-settings.json').read_text())
        self.assertEqual(saved['encoder'], 'libx264')
        with self.assertRaises(RuntimeError):
            self.client.command('configure', dict(settings, width=321))
        self.assertEqual(self.service.controller.settings['width'], 2560)

    def test_detaching_desktop_does_not_close_or_stop_service(self):
        client = RemoteController({}, self.directory.name, lambda *_: None, self.client)
        client.close()
        client.thread.join(3)
        self.assertFalse(client.thread.is_alive())
        self.assertTrue(self.service.controller.thread.is_alive())
        self.assertFalse(self.service.controller.closing)
        self.assertEqual(self.client.request('/v1/status')['snapshot']['state'], 'idle')

    def test_command_validation_and_empty_preview(self):
        with self.assertRaises(RuntimeError):
            self.client.command('scan', {'tracking': ['not a string']})
        with self.assertRaises(RuntimeError):
            self.client.command('shutdown')
        _, frame = self.client.request('/v1/frame', raw=True)
        self.assertEqual(frame, b'')
        self.assertTrue(self.client.command('stop'))

    def test_fault_is_visible_and_scans_are_locked(self):
        self.service.controller._fault('ORIGINAL', 'camera disconnected')
        self.assertFalse(self.client.command('scan', {'tracking': 'NEXT'}))
        status = self.client.request('/v1/status')
        self.assertEqual(status['snapshot']['fault_tracking'], 'ORIGINAL')
        self.assertIsNone(status['snapshot']['pending'])
        sequence = status['sequence']
        self.assertEqual(self.client.request(f'/v1/status?after={sequence}')['events'], [])

    def test_client_marks_disconnect_and_recovers(self):
        events = []
        client = RemoteController({}, self.directory.name, lambda *event: events.append(event), self.client)
        original = client.client.request
        client.client.request = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError('offline'))
        try:
            self.wait_for(lambda: client.snapshot()['state'] == 'disconnected')
            client.client.request = original
            self.wait_for(lambda: client.snapshot()['state'] == 'idle')
        finally:
            client.close()
            client.thread.join(3)
        self.assertTrue(any(event[0] == 'log' for event in events))

    @unittest.skipUnless(os.environ.get('ECOM_TEST_FFMPEG') and os.environ.get('ECOM_TEST_FFPROBE'), 'Real FFmpeg required')
    def test_recording_continues_after_client_detaches_and_stops_via_api(self):
        from ffmpeg_backend import CaptureSession
        self.service.controller.session_factory = lambda settings, output: CaptureSession(settings, output, synthetic=True)
        settings = self.client.request('/v1/status')['settings']
        settings.update(width=320, height=180, encoder='libx264', disk_warn_gb=2, disk_critical_gb=1,
                        ffmpeg_path=os.environ['ECOM_TEST_FFMPEG'], ffprobe_path=os.environ['ECOM_TEST_FFPROBE'])
        self.client.command('configure', settings)
        desktop = RemoteController({}, self.directory.name, lambda *_: None, self.client)
        try:
            self.assertTrue(desktop.scan('HTTP-RECORDING'))
            self.wait_for(lambda: self.service.controller.snapshot()['state'] == 'recording')
            desktop.close()
            desktop.thread.join(3)
            time.sleep(.4)
            self.assertEqual(self.client.request('/v1/status')['snapshot']['state'], 'recording')
            self.client.command('stop')
            self.wait_for(lambda: self.service.controller.snapshot()['state'] == 'idle')
            records = list((Path(self.directory.name) / 'records').glob('*.jsonl'))
            self.assertEqual(len(records), 1)
            record = json.loads(records[0].read_text().strip())
            self.assertEqual(record['tracking_number'] if 'tracking_number' in record else record['barcode'], 'HTTP-RECORDING')
            self.assertEqual(self.service.controller.jobs.count(), 0)
        finally:
            desktop.close()
            desktop.thread.join(3)

    def wait_for(self, predicate):
        deadline = time.monotonic() + 10
        while not predicate():
            if time.monotonic() > deadline:
                self.fail('Timed out')
            time.sleep(.02)


class LockTests(unittest.TestCase):
    def test_same_data_directory_cannot_have_two_owners(self):
        with tempfile.TemporaryDirectory() as directory:
            with DataLock(directory):
                with self.assertRaises(RuntimeError):
                    with DataLock(directory):
                        pass
            with DataLock(directory):
                pass

    def test_token_is_stable_between_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(load_token(directory), load_token(directory))


class EntryPointTests(unittest.TestCase):
    def test_main_service_dispatch_and_graceful_sigterm(self):
        if os.name == 'nt':
            self.skipTest('POSIX graceful SIGTERM check')
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen([sys.executable, 'main.py', '--service', '--data-dir', directory, '--port', '0'],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                deadline = time.monotonic() + 5
                while not (Path(directory) / 'service.token').exists():
                    if process.poll() is not None:
                        self.fail(process.communicate()[1].decode())
                    if time.monotonic() > deadline:
                        self.fail('Service did not start')
                    time.sleep(.02)
                # Wait until signal handlers are installed and listener is running.
                log = Path(directory) / 'logs/service.log'
                while not log.exists() or 'Service listening' not in log.read_text():
                    if time.monotonic() > deadline:
                        self.fail('Service did not start listening')
                    time.sleep(.02)
                process.terminate()
                _, errors = process.communicate(timeout=5)
                self.assertEqual(process.returncode, 0, errors.decode())
                with DataLock(directory):
                    pass
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
