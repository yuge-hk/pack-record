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
        self.directory = tempfile.TemporaryDirectory()
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

    def test_first_run_keeps_workbench_and_management_out_of_operator_flow(self):
        QTest.qWait(150)
        self.assertTrue(self.window.input.isVisible())
        self.assertTrue(self.window.preview_label.isVisible())
        self.assertFalse(self.window.library_button.isVisible())
        self.assertFalse(self.window.wizard_button.isVisible())
        self.assertFalse(self.window.web_button.isVisible())
        self.assertIsNone(self.app.activeModalWidget())

    def test_wide_workbench_preserves_size_for_long_plain_text_reminders(self):
        self.assertEqual((self.window.width(), self.window.height()), (1280, 720))
        text = '<b>买家留言</b>' + '请检查商品数量。' * 100
        self.window._backend_event('announcement', dict(text=text, warning=True))
        self.app.processEvents()
        self.assertEqual(self.window.announcement_label.toPlainText(), text)
        self.assertEqual((self.window.width(), self.window.height()), (1280, 720))
        self.assertLess(self.window.preview_label.mapTo(self.window, self.window.preview_label.rect().topLeft()).x(),
                        self.window.input.mapTo(self.window, self.window.input.rect().topLeft()).x())

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

        # Media integration needs only a small synthetic file. Check the disk
        # threshold separately rather than requiring 1 GB of free CI tmpfs.
        from collections import namedtuple
        usage = namedtuple('usage', 'total used free')(2 * 1024**3, 0, 2 * 1024**3)
        disk_patch = patch('recording_controller.shutil.disk_usage', return_value=usage)
        disk_patch.start()
        self.addCleanup(disk_patch.stop)
        self.addCleanup(self.close_window)

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


@unittest.skipUnless(QT_AVAILABLE, 'Qt and its system libraries are required')
class ServiceGuiTests(unittest.TestCase):
    def test_closing_connected_window_leaves_service_alive(self):
        import threading
        from recorder_service import RecorderService, RecorderHTTPServer
        from service_client import ServiceClient, RemoteController, RemoteSettingsStore
        app = QApplication.instance() or QApplication([])
        with tempfile.TemporaryDirectory() as directory:
            service = RecorderService(Path(directory) / 'service')
            server = RecorderHTTPServer(('127.0.0.1', 0), service, 'test-token')
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
            thread.start()
            window = None
            try:
                client = ServiceClient('test-token', server.server_port)
                base = Path(directory) / 'desktop'
                window = RecorderApp(base, lambda settings, directory, notify:
                                     RemoteController(settings, directory, notify, client),
                                     RemoteSettingsStore(client, base))
                window.show()
                app.processEvents()
                window.close()
                app.processEvents()
                self.assertTrue(window.exit_ready)
                self.assertFalse(service.controller.closing)
                self.assertTrue(service.controller.thread.is_alive())
                self.assertFalse((base / 'recorder-settings.json').exists())
            finally:
                if window:
                    window.controller.close()
                    window.controller.thread.join(3)
                    window.close()
                    window.deleteLater()
                    app.sendPostedEvents(None, QEvent.DeferredDelete)
                server.shutdown()
                server.server_close()
                thread.join()
                service.close()
