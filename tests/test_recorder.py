import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from ffmpeg_backend import (CaptureSession, FRAME_BYTES, SessionResult, available_encoders,
                            build_command, input_arguments, parse_dshow_devices, probe_recording)
from recorder_settings import RecorderSettingsStore
from recording_controller import RecordingController


def wait_for(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('Timed out waiting for state')


class SettingsAndCommandTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = RecorderSettingsStore(self.directory.name)
        self.settings = self.store.defaults()
        self.settings.update(ffmpeg_path=sys.executable, camera='USB Camera', microphone='USB Microphone')

    def test_defaults_and_obs_settings_are_preserved(self):
        legacy = Path(self.directory.name) / 'settings.json'
        legacy.write_text('{"obs_ws_port":4455}')
        saved = self.store.save(self.settings)
        self.assertEqual(self.store.load(), saved)
        self.assertEqual((saved['width'], saved['height'], saved['fps']), (2560, 1440, 30))
        self.assertEqual(saved['encoder'], 'hevc_qsv')
        self.assertEqual(legacy.read_text(), '{"obs_ws_port":4455}')

    def test_invalid_settings_and_corrupt_backup(self):
        for updates in ({'width': 2559}, {'disk_critical_gb': 40}, {'audio_enabled': 'false'},
                        {'duplicate_scan_seconds': float('nan')}, {'encoder': 'invalid'}):
            with self.assertRaises(ValueError):
                self.store.validate(dict(self.settings, **updates))
        self.store.path.write_text('{broken')
        self.assertEqual(self.store.load()['fps'], 30)
        self.assertEqual(len(list(Path(self.directory.name).glob('*.corrupt-*'))), 1)

    def test_windows_single_device_open_audio_mapping_and_qsv(self):
        cmd = build_command(self.settings, 'parcel.mkv', 'windows')
        self.assertEqual(cmd.count('-i'), 1)
        self.assertIn('video=USB Camera:audio=USB Microphone', cmd)
        self.assertIn('0:a:0', cmd)
        self.assertIn('hevc_qsv', cmd)
        self.assertNotIn('libx265', cmd)
        self.assertIn('2560x1440', cmd)
        self.assertIn('pipe:1', cmd)
        self.assertFalse(any('drawtext' in part for part in cmd))

    def test_linux_camera_audio_and_preview_disabled(self):
        self.settings.update(camera='/dev/video0', microphone='default', preview_enabled=False)
        cmd = build_command(self.settings, 'parcel.mkv', 'linux')
        self.assertEqual(cmd.count('-i'), 2)
        self.assertIn('v4l2', cmd)
        self.assertIn('pulse', cmd)
        self.assertIn('1:a:0', cmd)
        self.assertNotIn('pipe:1', cmd)
        self.settings['audio_backend'] = 'alsa'
        self.assertIn('alsa', build_command(self.settings, 'parcel.mkv', 'linux'))

    def test_microphone_must_be_explicit_and_device_names_are_arguments(self):
        self.settings['microphone'] = ''
        with self.assertRaisesRegex(ValueError, '麦克风'):
            input_arguments(self.settings, 'windows')
        self.settings['audio_enabled'] = False
        self.settings['camera'] = 'Camera $(touch /tmp/should-not-exist) & name'
        cmd = build_command(self.settings, 'parcel.mkv', 'windows')
        self.assertIn('video=' + self.settings['camera'], cmd)
        self.assertNotIn('-c:a', cmd)

    def test_dshow_duplicate_names_use_alternative_identifiers(self):
        devices = parse_dshow_devices('''[dshow] "USB Camera" (video)
[dshow] Alternative name "@device_pnp_1"
[dshow] "USB Camera" (video)
[dshow] Alternative name "@device_pnp_2"
[dshow] "麦克风" (audio)
[dshow] Alternative name "@device_audio_1"''')
        self.assertEqual(devices['video'], [('USB Camera', '@device_pnp_1'), ('USB Camera', '@device_pnp_2')])
        self.assertEqual(devices['audio'][0][1], '@device_audio_1')

    def test_probe_rejects_header_only_audio(self):
        self.settings['ffprobe_path'] = sys.executable
        media = dict(streams=[dict(index=0, codec_type='video', codec_name='hevc', width=2560, height=1440),
                              dict(index=1, codec_type='audio')], format=dict(duration='1.5'))
        responses = [subprocess.CompletedProcess([], 0, json.dumps(media).encode(), b''),
                     subprocess.CompletedProcess([], 0, b'{"packets":[{"stream_index":0}]}', b'')]
        with patch('ffmpeg_backend.subprocess.run', side_effect=responses):
            with self.assertRaisesRegex(RuntimeError, '数据包'):
                probe_recording('test.mkv', self.settings)


class FakeSession:
    def __init__(self, settings, output=None, finish_gate=None, failure=''):
        self.output, self.failure = output, failure
        self.stop_event, self.started_event = threading.Event(), threading.Event()
        self.finish_gate = finish_gate

    def stop(self):
        self.stop_event.set()

    def latest_frame(self, serial=0):
        return serial, None

    def run(self, started, progress):
        if self.output:
            Path(self.output).write_bytes(b'fake media' * 2048)
        if self.failure:
            self.started_event.set()
            return SessionResult(False, self.failure, 1)
        started()
        self.started_event.set()
        if not self.stop_event.wait(5):
            raise RuntimeError('Test failed to stop fake session')
        if self.finish_gate and not self.finish_gate.wait(5):
            raise RuntimeError('Test failed to release finalization')
        return SessionResult(True, '', 0)


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = RecorderSettingsStore(self.root).defaults()
        self.settings.update(disk_critical_gb=1, disk_warn_gb=2)
        self.encoders = patch('recording_controller.available_encoders', return_value={'hevc_qsv'})
        self.encoders.start()
        self.addCleanup(self.encoders.stop)
        self.tools = patch('recording_controller.find_tool', return_value=sys.executable)
        self.tools.start()
        self.addCleanup(self.tools.stop)
        self.probe = patch('recording_controller.probe_recording', return_value=1.5)
        self.probe.start()
        self.addCleanup(self.probe.stop)
        self.space = patch('recording_controller.shutil.disk_usage', return_value=shutil_usage())
        self.space.start()
        self.addCleanup(self.space.stop)
        self.sessions, self.events = [], []
        self.gate = threading.Event()
        self.fail_next = ''

        def factory(settings, output=None):
            session = FakeSession(settings, output, self.gate if not self.sessions else None, self.fail_next)
            self.fail_next = ''
            self.sessions.append(session)
            return session

        self.controller = RecordingController(self.settings, self.root, lambda *e: self.events.append(e), factory)
        self.addCleanup(self.shutdown)

    def shutdown(self):
        self.gate.set()
        self.controller.close()
        self.controller.thread.join(6)
        self.assertFalse(self.controller.thread.is_alive())

    def start(self, tracking='A'):
        self.controller.scan(tracking)
        wait_for(lambda: self.controller.snapshot()['state'] == 'recording')

    def records(self):
        return [json.loads(line) for file in (self.root / 'records').glob('*.jsonl')
                for line in file.read_text().splitlines()]

    def test_rapid_scans_keep_only_latest_and_duplicate_does_not_stop(self):
        self.start()
        self.assertFalse(self.controller.scan('A'))
        self.assertFalse(self.sessions[0].stop_event.is_set())
        self.controller.scan('B')
        wait_for(self.sessions[0].stop_event.is_set)
        self.controller.scan('C')
        self.controller.scan('D')
        self.gate.set()
        wait_for(lambda: self.controller.snapshot()['current'] == 'D' and self.controller.snapshot()['state'] == 'recording')
        self.controller.scan('STOPSTOP')
        wait_for(lambda: self.controller.snapshot()['state'] == 'idle')
        self.assertEqual([r['barcode'] for r in self.records()], ['A', 'D'])
        self.assertEqual(self.controller.jobs.count(), 0)

    def test_stop_during_preflight_is_not_lost(self):
        entered, release = threading.Event(), threading.Event()

        def preflight(_):
            entered.set()
            release.wait(5)
            return {'hevc_qsv'}

        with patch('recording_controller.available_encoders', side_effect=preflight):
            self.controller.scan('A')
            self.assertTrue(entered.wait(2))
            self.controller.scan('STOPSTOP')
            self.gate.set()
            release.set()
            wait_for(lambda: len(self.records()) == 1 and self.controller.snapshot()['state'] == 'idle')
        self.assertTrue(self.sessions[0].stop_event.is_set())
        self.assertEqual(self.records()[0]['barcode'], 'A')

    def test_failure_locks_scans_and_preserves_original_for_retry(self):
        self.fail_next = 'QSV startup failed'
        self.controller.scan('FAULT-A')
        wait_for(lambda: self.controller.snapshot()['state'] == 'fault' and self.controller.session is None)
        self.assertEqual(self.controller.snapshot()['fault_tracking'], 'FAULT-A')
        self.assertFalse(self.controller.scan('B'))
        self.assertEqual(self.records()[0]['capture']['interrupted'], True)
        self.gate.set()
        self.controller.retry_fault()
        wait_for(lambda: self.controller.snapshot()['state'] == 'recording')
        self.assertEqual(self.controller.snapshot()['current'], 'FAULT-A')
        self.controller.stop()
        wait_for(lambda: self.controller.snapshot()['state'] == 'idle')
        self.assertEqual([r['barcode'] for r in self.records()], ['FAULT-A', 'FAULT-A'])
        self.assertNotEqual(self.records()[0]['video_path'], self.records()[1]['video_path'])

    def test_archive_error_keeps_recovery_job_and_blocks_next_scan(self):
        self.gate.set()
        with patch('recording_controller.probe_recording', side_effect=RuntimeError('missing audio')):
            self.start()
            self.controller.scan('B')
            wait_for(lambda: self.controller.snapshot()['state'] == 'fault' and self.controller.session is None)
        self.assertEqual(self.controller.snapshot()['fault_tracking'], 'A')
        self.assertIsNone(self.controller.snapshot()['pending'])
        self.assertEqual(self.controller.jobs.count(), 1)
        self.assertEqual(self.records(), [])
        self.controller.recover()
        wait_for(lambda: self.controller.jobs.count() == 0 and self.controller.snapshot()['state'] == 'fault')
        self.assertFalse(self.controller.scan('B'))
        self.assertEqual(self.records()[0]['barcode'], 'A')

    def test_journal_exists_before_capture_and_shutdown_saves(self):
        self.start()
        job = self.controller.jobs.list()[0]
        self.assertEqual(job['state'], 'capturing')
        self.assertEqual(job['tracking_number'], 'A')
        self.gate.set()
        self.controller.close()
        self.controller.thread.join(5)
        self.assertFalse(self.controller.thread.is_alive())
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(self.controller.jobs.count(), 0)

    def test_recovery_after_move_is_idempotent(self):
        self.start()
        job = self.controller.jobs.list()[0]
        self.gate.set()
        self.controller.stop()
        wait_for(lambda: self.controller.snapshot()['state'] == 'idle')
        record = self.records()[0]
        job.update(planned_destination=record['video_path'], state='moving')
        self.controller.jobs.upsert(job)
        self.controller.recover()
        wait_for(lambda: self.controller.jobs.count() == 0)
        self.assertEqual(len(self.records()), 1)
        self.assertEqual(len(list(Path(self.settings['record_path']).rglob('*.mkv'))), 1)

    def test_recovery_after_cross_volume_copy_deletes_only_identical_source(self):
        self.start()
        job = self.controller.jobs.list()[0]
        self.gate.set()
        self.controller.stop()
        wait_for(lambda: self.controller.snapshot()['state'] == 'idle')
        destination = self.records()[0]['video_path']
        Path(job['source_path']).write_bytes(Path(destination).read_bytes())
        job.update(planned_destination=destination, state='moving')
        self.controller.jobs.upsert(job)
        self.controller.recover()
        wait_for(lambda: self.controller.jobs.count() == 0)
        self.assertFalse(Path(job['source_path']).exists())
        self.assertEqual(len(self.records()), 1)

    def test_unsupported_encoder_does_not_fall_back(self):
        with patch('recording_controller.available_encoders', return_value={'libx264'}):
            self.controller.scan('A')
            wait_for(lambda: self.controller.snapshot()['state'] == 'fault')
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.controller.snapshot()['fault_tracking'], 'A')

    def test_missing_device_selection_does_not_create_a_phantom_recovery_job(self):
        self.controller.settings['ffmpeg_path'] = '/bin/echo'
        self.controller.session_factory = CaptureSession
        self.controller.scan('A')
        wait_for(lambda: self.controller.snapshot()['state'] == 'fault' and self.controller.session is None)
        self.assertEqual(self.controller.jobs.count(), 0)
        self.assertEqual(self.controller.snapshot()['fault_tracking'], 'A')

    def test_low_disk_blocks_capture_without_spawning_ffmpeg(self):
        usage = shutil_usage()._replace(free=256 * 1024**2)
        with patch('recording_controller.shutil.disk_usage', return_value=usage):
            self.controller.scan('LOW-DISK')
            wait_for(lambda: self.controller.snapshot()['state'] == 'fault')
        self.assertEqual(self.sessions, [])
        self.assertEqual(self.controller.jobs.count(), 0)
        self.assertEqual(self.controller.snapshot()['fault_tracking'], 'LOW-DISK')


def shutil_usage():
    from collections import namedtuple
    return namedtuple('usage', 'total used free')(100 * 1024**3, 10 * 1024**3, 90 * 1024**3)


@unittest.skipUnless(os.environ.get('ECOM_TEST_FFMPEG') and os.environ.get('ECOM_TEST_FFPROBE'),
                     'Set ECOM_TEST_FFMPEG and ECOM_TEST_FFPROBE for real media integration tests')
class MediaIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = RecorderSettingsStore(self.directory.name).defaults()
        self.settings.update(width=640, height=360, encoder='libx264',
                             ffmpeg_path=os.environ['ECOM_TEST_FFMPEG'],
                             ffprobe_path=os.environ['ECOM_TEST_FFPROBE'])

    def capture(self, encoder='libx264', audio=True, preview=True):
        self.settings.update(encoder=encoder, audio_enabled=audio, preview_enabled=preview)
        output = Path(self.directory.name) / f'{encoder}-{audio}-{preview}.mkv'
        session = CaptureSession(self.settings, output, synthetic=True)
        confirmed = threading.Event()
        timers = []

        def started():
            confirmed.set()
            timer = threading.Timer(1, session.stop)
            timer.start()
            timers.append(timer)

        result = session.run(started)
        for timer in timers:
            timer.join()
        self.assertTrue(confirmed.is_set(), result.error)
        self.assertEqual(result.error, '')
        self.assertGreater(probe_recording(output, self.settings), 0)
        _, frame = session.latest_frame()
        self.assertEqual(len(frame or b''), FRAME_BYTES if preview else 0)
        return output

    def test_real_h264_audio_preview_and_clean_stop(self):
        self.capture()

    def test_real_h265_audio_without_preview(self):
        self.capture('libx265', preview=False)

    def test_real_video_without_microphone(self):
        self.capture(audio=False)

    def test_real_1440p_30fps_with_audio_and_aligned_timestamps(self):
        self.settings.update(width=2560, height=1440, preset='veryfast')
        output = self.capture()
        result = subprocess.run([self.settings['ffprobe_path'], '-v', 'error', '-show_streams',
                                 '-show_packets', '-show_entries', 'packet=stream_index,pts_time',
                                 '-of', 'json', str(output)], capture_output=True, check=True)
        data = json.loads(result.stdout)
        video = next(stream for stream in data['streams'] if stream['codec_type'] == 'video')
        audio = next(stream for stream in data['streams'] if stream['codec_type'] == 'audio')
        self.assertEqual(video['r_frame_rate'], '30/1')
        video_times = [float(p['pts_time']) for p in data['packets'] if p['stream_index'] == video['index']]
        audio_times = [float(p['pts_time']) for p in data['packets'] if p['stream_index'] == audio['index']]
        self.assertLess(abs(min(video_times) - min(audio_times)), 0.2)
        self.assertLess(abs(max(video_times) - max(audio_times)), 0.2)

    def test_no_hardware_encoder_does_not_produce_false_success(self):
        if 'hevc_qsv' in available_encoders(self.settings):
            self.skipTest('This test needs a build without QSV')
        self.settings['encoder'] = 'hevc_qsv'
        result = CaptureSession(self.settings, Path(self.directory.name) / 'fail.mkv', synthetic=True).run()
        self.assertFalse(result.confirmed)
        self.assertTrue(result.error)


class SessionFaultTests(unittest.TestCase):
    """Use real child processes to exercise watchdogs and forced shutdown."""
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = RecorderSettingsStore(self.directory.name).defaults()
        self.settings.update(stall_timeout_seconds=3, stop_timeout_seconds=3, start_timeout_seconds=3)

    def session_with_script(self, body):
        import sys
        script = Path(self.directory.name) / 'fake_ffmpeg.py'
        script.write_text(body)
        return patch('ffmpeg_backend.build_command', return_value=[sys.executable, str(script)])

    def test_microphone_stall_is_detected_while_video_keeps_advancing(self):
        body = '''import sys, time, threading
stopped=threading.Event()
def input_reader():
 for line in sys.stdin:
  if 'q' in line: stopped.set(); return
threading.Thread(target=input_reader,daemon=True).start()
sys.stderr.write('audio_time=0.1\\n'); sys.stderr.flush()
t=0.1
while not stopped.is_set():
 sys.stderr.write(f'video_time={t}\\n'); sys.stderr.flush()
 t+=0.1
 time.sleep(0.05)
'''
        with self.session_with_script(body):
            result = CaptureSession(self.settings).run()
        self.assertTrue(result.confirmed)
        self.assertIn('麦克风声音输出停滞', result.error)
        self.assertFalse(result.forced)

    def test_stop_timeout_kills_unresponsive_process(self):
        body = '''import sys, time
t=0.1
while True:
 sys.stderr.write(f'video_time={t}\\naudio_time={t}\\n'); sys.stderr.flush()
 t+=0.1
 time.sleep(0.05)
'''
        session = CaptureSession(self.settings)
        with self.session_with_script(body):
            result = session.run(session.stop)
        self.assertTrue(result.confirmed)
        self.assertTrue(result.forced)
        self.assertIn('停止超时', result.error)

    def test_startup_error_is_not_reported_as_recording(self):
        body = "import sys\nsys.stderr.write('Encoder initialization failed\\n')\nsys.exit(1)\n"
        with self.session_with_script(body):
            result = CaptureSession(self.settings).run()
        self.assertFalse(result.confirmed)
        self.assertIn('Encoder initialization failed', result.error)

class PackagedPathsTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'Linux loader behavior')
    def test_frozen_process_restores_system_library_path(self):
        import sys
        from ffmpeg_backend import process_options
        with patch.object(sys, 'frozen', True, create=True):
            with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/bundled', 'LD_LIBRARY_PATH_ORIG': '/system'}):
                self.assertEqual(process_options()['env']['LD_LIBRARY_PATH'], '/system')
            with patch.dict(os.environ, {'LD_LIBRARY_PATH': '/bundled'}, clear=True):
                self.assertNotIn('LD_LIBRARY_PATH', process_options()['env'])

    @unittest.skipIf(os.name == 'nt', 'Linux installation paths')
    def test_installed_linux_application_uses_user_data_directory(self):
        import sys
        from recorder_settings import application_dir, runtime_dir
        with patch.object(sys, 'frozen', True, create=True), patch.object(sys, 'executable', '/opt/pack-record/pack-record'):
            with patch.dict(os.environ, {'XDG_DATA_HOME': '/tmp/test-user-data'}):
                self.assertEqual(application_dir(), Path('/opt/pack-record'))
                if os.name != 'nt':
                    self.assertEqual(runtime_dir(), Path('/tmp/test-user-data/pack-record'))

    def test_embedded_portable_runtime_uses_profile_data(self):
        import sys
        from recorder_settings import runtime_dir
        with patch.object(sys, '_pack_record_bundle', True, create=True):
            if os.name == 'nt':
                with patch.dict(os.environ, {'LOCALAPPDATA': '/tmp/test-local-data'}):
                    self.assertEqual(runtime_dir(), Path('/tmp/test-local-data/PackRecord'))
            else:
                with patch.dict(os.environ, {'XDG_DATA_HOME': '/tmp/test-user-data'}):
                    self.assertEqual(runtime_dir(), Path('/tmp/test-user-data/pack-record'))


if __name__ == '__main__':
    unittest.main()
