"""Headless GUI checks: actual scan events and real recording during shutdown."""
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
try:
    from PySide6.QtCore import QEvent, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from main import RecorderApp
    from recorder_settings import RecorderSettingsStore
    from settings_dialog import SettingsDialog
    from recording_controller import RecordingController
    from ffmpeg_backend import CaptureSession
    QT_AVAILABLE = True
except ImportError:
    QT_AVAILABLE = False

TEST_APP = None


@unittest.skipUnless(QT_AVAILABLE, 'Qt and its system libraries are required')
class GuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        global TEST_APP
        TEST_APP = QApplication.instance() or QApplication([])
        cls.app = TEST_APP

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(dir='/tmp')
        self.addCleanup(self.directory.cleanup)
        self.window = RecorderApp(self.directory.name)
        self.window_disposed = False
        self.window.show()
        self.app.processEvents()
        self.addCleanup(self.close_window)

    def close_window(self):
        if self.window_disposed:
            return
        with patch.object(QMessageBox, 'question', return_value=QMessageBox.Yes):
            self.window.close()
        self.wait_for(lambda: not self.window.controller.thread.is_alive(), 30)
        # The worker can finish before the GUI's 100ms shutdown tick. Finalize
        # the window before releasing its Qt wrappers or temporary directory.
        self.window.tick()
        self.app.processEvents()
        self.window.deleteLater()
        self.app.sendPostedEvents(None, QEvent.DeferredDelete)
        self.window_disposed = True

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            QTest.qWait(10)
        self.fail('GUI did not reach expected state')

    def test_keyboard_scanner_submits_once_with_enter(self):
        with patch.object(self.window.controller, 'scan') as scan:
            QTest.keyClicks(self.window.input, 'TRACK-123')
            QTest.keyClick(self.window.input, Qt.Key_Return)
            QTest.qWait(220)
            self.assertEqual(scan.call_args_list[0].args, ('TRACK-123',))
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(self.window.input.text(), '')

    def test_fast_scanner_fallback_without_enter(self):
        with patch.object(self.window.controller, 'scan') as scan:
            QTest.keyClicks(self.window.input, 'TRACK-456')
            QTest.qWait(240)
            self.assertEqual(scan.call_count, 1)
            self.assertEqual(scan.call_args.args, ('TRACK-456',))

    def test_stop_button_cancels_pending_fast_input(self):
        with patch.object(self.window.controller, 'scan') as scan:
            QTest.keyClicks(self.window.input, 'TRACK-789')
            self.window.stop()
            QTest.qWait(240)
            scan.assert_not_called()

    def test_settings_manually_edited_identifier_does_not_use_stale_selection(self):
        dialog = SettingsDialog(dict(self.window.settings, camera='old camera'), self.window)
        dialog.fields['camera'].setEditText('new camera')
        self.assertEqual(dialog.values()['camera'], 'new camera')
        self.assertEqual(dialog.values()['width'], 2560)
        self.assertEqual(dialog.values()['encoder'], 'hevc_qsv')
        dialog.close()

    @unittest.skipUnless(os.environ.get('ECOM_TEST_FFMPEG'), 'Real FFmpeg required')
    def test_real_capture_preview_scan_and_shutdown_archive(self):
        self.close_window()
        settings = RecorderSettingsStore(self.directory.name).defaults()
        settings.update(width=640, height=360, encoder='libx264', disk_critical_gb=1, disk_warn_gb=2,
                        ffmpeg_path=os.environ['ECOM_TEST_FFMPEG'], ffprobe_path=os.environ['ECOM_TEST_FFPROBE'])
        RecorderSettingsStore(self.directory.name).save(settings)

        def factory(settings, directory, notify):
            return RecordingController(settings, directory, notify,
                                       lambda s, path: CaptureSession(s, path, synthetic=True))

        self.window = RecorderApp(self.directory.name, factory)
        self.window_disposed = False
        self.window.show()
        self.app.processEvents()
        QTest.keyClicks(self.window.input, 'REAL-123')
        QTest.keyClick(self.window.input, Qt.Key_Return)
        self.wait_for(lambda: self.window.controller.snapshot()['state'] == 'recording', 15)
        self.wait_for(lambda: (image := self.window.preview_label.pixmap()) is not None and not image.isNull())
        self.close_window()
        videos = list((Path(self.directory.name) / 'recordings').rglob('REAL-123.mkv'))
        self.assertEqual(len(videos), 1)
        self.assertGreater(videos[0].stat().st_size, 1024)
        self.assertEqual(self.window.controller.jobs.count(), 0)
        self.assertFalse(self.window.controller.thread.is_alive())
