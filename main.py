# -*- coding: utf-8 -*-
"""Cross-platform USB camera shipping recorder. FFmpeg owns all media capture."""
import hashlib
import json
import logging
import os
import platform
import shutil
import sys
import tempfile
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Dispatch before importing Qt so the daemon works without a desktop session.
if __name__ == "__main__" and "--service" in sys.argv:
    from recorder_service import main as service_main
    raise SystemExit(service_main([value for value in sys.argv[1:] if value != "--service"]))

if "--self-test" in sys.argv:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QLockFile, QStandardPaths, QTimer, Qt, Signal, qVersion
from PySide6.QtGui import QImage, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTextEdit, QVBoxLayout, QWidget, QGridLayout, QScrollArea, QFrame)

from ffmpeg_backend import PREVIEW_HEIGHT, PREVIEW_WIDTH, available_encoders, run_tool
from recorder_settings import RecorderSettingsStore, find_tool, runtime_dir
from recording_controller import RecordingController
from settings_dialog import SettingsDialog
from speech_engine import SpeechEngine
from management_dialog import ManagementDialog
from setup_wizard import SetupWizard

APP_VERSION = "2.1.0"
STATE_LABELS = dict(testing="正在进行设备试录", idle="等待扫码", starting="正在启动录制", recording="录制中",
                    saving="正在保存", preview_starting="正在打开试拍", preview="试拍预览（不保存）",
                    disconnected="录像服务连接中断", recovering="正在恢复保存任务", fault="故障锁定", closed="已退出")


class RecorderApp(QWidget):
    backend_event = Signal(str, object)

    def __init__(self, base_dir=None, controller_factory=RecordingController, settings_store=None):
        super().__init__()
        self.base_dir = Path(base_dir) if base_dir else runtime_dir()
        self.store = settings_store or RecorderSettingsStore(self.base_dir)
        self.settings = self.store.load()
        self.exiting = self.exit_ready = False
        self.previous_session, self.previous_serial = None, 0
        self.scan_first = self.scan_last = None
        self.scan_buffer = ""
        self.setWindowTitle(f"打包录像 v{APP_VERSION} · FFmpeg")
        self.resize(1280, 720)
        self.setMinimumSize(960, 540)
        self.setStyleSheet("""
            RecorderApp { background: #edf1f6; color: #17243b; }
            QLineEdit { background: white; border: 1px solid #c7d1df; border-radius: 7px; padding: 9px; }
            QPushButton { background: white; border: 1px solid #ccd5e0; border-radius: 6px; padding: 8px; }
            QPushButton:hover { background: #eaf1fc; border-color: #7498d2; }
            QPushButton:disabled { color: #91a0b3; background: #f3f5f8; }
            QTextEdit { background: #f3f6fa; border: 0; border-radius: 6px; padding: 5px; font-size: 11px; }
        """)
        root = QHBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(16)
        view = QWidget()
        view.setStyleSheet('background: #101827; border-radius: 10px; color: #d9e4f5;')
        preview_layout = QVBoxLayout(view)
        preview_layout.setContentsMargins(18, 18, 18, 14)
        title = QLabel('PACKRECORD  /  打包录像')
        title.setStyleSheet('font-size: 16px; font-weight: bold; color: #9db6d7;')
        preview_layout.addWidget(title)
        self.preview_label = QLabel('等待扫码')
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(480, 270)
        self.preview_label.setStyleSheet('color: #b6c4d8; font-size: 18px;')
        preview_layout.addWidget(self.preview_label, 1)
        hint = QLabel('实时取景 · 扫码自动开始 / 切换订单录像')
        hint.setStyleSheet('color: #7e94b3; font-size: 12px;')
        preview_layout.addWidget(hint)
        root.addWidget(view, 7)
        info = QWidget()
        info.setMinimumWidth(320)
        info.setMaximumWidth(400)
        sidebar = QScrollArea()
        sidebar.setWidgetResizable(True)
        sidebar.setFrameShape(QFrame.NoFrame)
        sidebar.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        sidebar.setMinimumWidth(340)
        sidebar.setMaximumWidth(420)
        sidebar.setWidget(info)
        root.addWidget(sidebar, 3)
        layout = QVBoxLayout(info)
        layout.setContentsMargins(0, 2, 0, 0)
        layout.setSpacing(10)
        self.state_label = QLabel('等待扫码')
        self.state_label.setStyleSheet('font-size: 25px; font-weight: bold; color: #166b55;')
        layout.addWidget(self.state_label)
        self.tracking_label = QLabel('当前单号：—')
        self.tracking_label.setTextFormat(Qt.PlainText)
        self.tracking_label.setWordWrap(True)
        self.tracking_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.tracking_label.setStyleSheet('font-size: 21px; font-weight: bold;')
        layout.addWidget(self.tracking_label)
        self.input = QLineEdit()
        self.input.setPlaceholderText('扫描或输入单号，回车确认')
        self.input.setMinimumHeight(48)
        self.input.setStyleSheet('font-size: 17px;')
        self.input.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.input.setInputMethodHints(Qt.ImhNoPredictiveText)
        self.input.returnPressed.connect(self.handle_input)
        self.input.textEdited.connect(self._scan_edited)
        layout.addWidget(self.input)
        self.pending_label = QLabel('最新待处理单号：—')
        self.pending_label.setWordWrap(True)
        self.pending_label.setStyleSheet('color: #687b94; font-size: 12px;')
        layout.addWidget(self.pending_label)
        self.announcement_label = QTextEdit()
        self.announcement_label.setReadOnly(True)
        self.announcement_label.setMinimumHeight(70)
        self.announcement_label.setMaximumHeight(140)
        self.announcement_label.setStyleSheet('background: #fff3d7; color: #89500e; border-radius: 7px; padding: 10px; font-size: 16px;')
        layout.addWidget(self.announcement_label)
        self.announcement_label.hide()
        self.fault_label = QTextEdit()
        self.fault_label.setReadOnly(True)
        self.fault_label.setMaximumHeight(130)
        self.fault_label.setStyleSheet('background: #b91c1c; color: white; border-radius: 6px; padding: 10px;')
        self.fault_label.hide()
        layout.addWidget(self.fault_label)
        buttons = QGridLayout()
        buttons.setSpacing(8)
        self.stop_button = QPushButton('停止并保存')
        self.stop_button.setStyleSheet('background: #2164c8; color: white; border: 0; padding: 10px;')
        self.stop_button.clicked.connect(self.stop)
        self.preview_button = QPushButton('试拍预览')
        self.preview_button.clicked.connect(self.preview)
        self.settings_button = QPushButton('设置 · Web')
        self.settings_button.clicked.connect(self.open_web)
        self.recover_button = QPushButton('恢复保存任务')
        self.recover_button.clicked.connect(self.recover)
        for i, button in enumerate((self.stop_button, self.preview_button, self.settings_button, self.recover_button)):
            buttons.addWidget(button, i // 2, i % 2)
        self.retry_button = QPushButton('修复后重试原单号')
        self.retry_button.clicked.connect(self.retry_fault)
        self.retry_button.hide()
        buttons.addWidget(self.retry_button, 2, 0, 1, 2)
        layout.addLayout(buttons)
        stop_hint = QLabel('扫描 STOPSTOP 也可停止并保存')
        stop_hint.setStyleSheet('font-size: 11px; color: #687b94;')
        layout.addWidget(stop_hint)
        layout.addStretch(1)
        self.config_label = QLabel()
        self.config_label.setWordWrap(True)
        self.config_label.setStyleSheet('font-size: 11px; color: #6a7b92;')
        layout.addWidget(self.config_label)
        self.disk_label = QLabel()
        self.disk_label.setWordWrap(True)
        self.disk_label.setStyleSheet('font-size: 12px;')
        layout.addWidget(self.disk_label)
        self.progress_label = QLabel('录制帧数：—')
        self.progress_label.setStyleSheet('font-size: 11px; color: #6a7b92;')
        layout.addWidget(self.progress_label)
        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.document().setMaximumBlockCount(300)
        self.logs.setMaximumHeight(100)
        layout.addWidget(self.logs)
        advanced_widget = QWidget()
        management = QVBoxLayout(advanced_widget)
        self.library_button = QPushButton('录像与存储管理')
        self.library_button.clicked.connect(self.open_management)
        management.addWidget(self.library_button)
        self.wizard_button = QPushButton('配置向导')
        self.wizard_button.clicked.connect(self.open_wizard)
        management.addWidget(self.wizard_button)
        self.web_button = QPushButton('打开本机 Web')
        self.web_button.clicked.connect(self.open_web)
        management.addWidget(self.web_button)
        layout.addWidget(advanced_widget)
        advanced_widget.hide()
        log_dir = self.base_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger(f"recorder-{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.log_handler = RotatingFileHandler(log_dir / "recorder.log", maxBytes=5 * 1024**2,
                                              backupCount=5, encoding="utf-8")
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger.addHandler(self.log_handler)
        self.speech = SpeechEngine(self)
        self.speech.unavailable.connect(self.log)
        self.backend_event.connect(self._backend_event, Qt.QueuedConnection)
        self.controller = controller_factory(self.settings, self.base_dir, self.backend_event.emit)
        self.scan_timer = QTimer(self)
        self.scan_timer.setSingleShot(True)
        self.scan_timer.timeout.connect(self._fast_scan_submit)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(100)
        self.disk_timer = QTimer(self)
        self.disk_timer.timeout.connect(self.update_storage)
        self.disk_timer.start(3000)
        QShortcut(QKeySequence("Ctrl+,"), self, activated=self.open_settings)
        QShortcut(QKeySequence('Ctrl+Shift+M'), self, activated=self.open_management)
        QShortcut(QKeySequence('Ctrl+Shift+W'), self, activated=self.open_wizard)
        QApplication.instance().installEventFilter(self)
        self._update_config()
        self.update_storage()
        self._backend_event("state", self.controller.snapshot())
        for warning in self.store.warnings + self.controller.jobs.consume_warnings():
            self.log(warning)
        legacy = self.base_dir / "records" / "failed_jobs.json"
        if legacy.exists():
            self.log("检测到旧 OBS 恢复任务文件；新版不会改写它，请用旧版或人工核查。")
        self.log("默认 2560×1440 / 30 FPS，带声音，无画面水印；先配置设备。")
        QTimer.singleShot(0, self.focus_scan)

    def log(self, message):
        cursor = self.logs.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        cursor.insertText(f"{time.strftime('%H:%M:%S')} {message}\n")
        self.logs.setTextCursor(cursor)
        self.logger.info(message)

    def handle_input(self):
        value = self.input.text().strip()
        self.input.clear()
        self.scan_timer.stop()
        self.scan_first = self.scan_last = None
        self.scan_buffer = ""
        if value:
            self.controller.scan(value)
        self.focus_scan()

    def _scan_edited(self, text):
        now = time.monotonic()
        if not text or self.scan_last is None or now - self.scan_last > 0.2:
            self.scan_first = now
        self.scan_last = now
        # A keyboard-wedge scanner may have its Enter swallowed by an input method.
        if len(text) >= 4 and self.scan_first is not None and (now - self.scan_first) / max(1, len(text) - 1) <= 0.04:
            self.scan_timer.start(180)
        else:
            self.scan_timer.stop()

    def _fast_scan_submit(self):
        if self.input.text().strip():
            self.handle_input()

    def focus_scan(self):
        if self.exiting or QApplication.activeModalWidget():
            return
        self.input.setFocus()
        if os.name == "nt":
            try:
                import ctypes
                # Apply to this UI thread rather than changing the system-wide layout.
                user32 = ctypes.windll.user32
                user32.LoadKeyboardLayoutW.restype = ctypes.c_void_p
                user32.ActivateKeyboardLayout.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                layout = user32.LoadKeyboardLayoutW("00000409", 0)
                user32.ActivateKeyboardLayout(layout, 0)
            except (AttributeError, OSError):
                pass

    def eventFilter(self, watched, event):
        if not hasattr(self, "controller"):
            return super().eventFilter(watched, event)
        if watched is self.input and event.type() == QEvent.FocusIn:
            QTimer.singleShot(0, self.focus_scan)
        if event.type() == QEvent.KeyPress and self.isActiveWindow() and not QApplication.activeModalWidget():
            if self.controller.snapshot()["state"] == "fault":
                if watched is self.input or (event.text() and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)):
                    QApplication.beep()
                    return True
            if QApplication.focusWidget() is not self.input and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier):
                if event.key() in (Qt.Key_Return, Qt.Key_Enter) and self.scan_buffer:
                    self.input.setText(self.scan_buffer)
                    self.handle_input()
                    return True
                if event.text() and event.text().isprintable() and not event.isAutoRepeat():
                    now = time.monotonic()
                    if self.scan_last is None or now - self.scan_last > 0.2:
                        self.scan_buffer = ""
                    self.scan_buffer += event.text()
                    self.input.setText(self.scan_buffer)
                    self._scan_edited(self.scan_buffer)
                    self.focus_scan()
                    return True
        return super().eventFilter(watched, event)

    def stop(self):
        self.scan_timer.stop()
        self.input.clear()
        self.controller.stop()
        self.focus_scan()

    def preview(self):
        try:
            self.controller.preview()
        except Exception as error:
            QMessageBox.warning(self, "无法试拍", str(error))
        self.focus_scan()

    def retry_fault(self):
        self.controller.retry_fault()
        self.focus_scan()

    def recover(self):
        try:
            self.controller.recover()
        except Exception as error:
            QMessageBox.warning(self, "无法恢复", str(error))

    def open_settings(self):
        snapshot = self.controller.snapshot()
        if snapshot["state"] not in ("idle", "fault") or self.controller.session or snapshot["pending"]:
            QMessageBox.information(self, "请先停止", "请先停止录像或试拍预览，再修改设置。")
            return
        dialog = SettingsDialog(self.settings, self)
        if dialog.exec() == QDialog.Accepted:
            try:
                values = self.store.validate(dialog.values())
                self.controller.configure(values)
                self.settings = self.store.save(values)
                self._update_config()
                self.update_storage()
                self.log("设置已保存")
            except Exception as error:
                self.controller.configure(self.settings)
                QMessageBox.warning(self, "设置未保存", str(error))
        self.focus_scan()

    def open_management(self):
        dialog = ManagementDialog(self.controller, self)
        dialog.exec()
        dialog.close()

    def open_wizard(self):
        if self.controller.snapshot()['state'] not in ('idle', 'fault') or self.controller.settings['role'] == 'viewer':
            QMessageBox.information(self, '请先停止', '请先停止录制，再运行管理员配置向导。')
            return
        dialog = SetupWizard(self)
        dialog.exec()
        self.settings = dict(self.controller.settings)
        self._update_config()

    def open_web(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        if hasattr(self.controller, 'client'):
            url = self.controller.client.base
        else:
            url = getattr(self, 'web_url', '')
        if url:
            QDesktopServices.openUrl(QUrl(url))
        else:
            QMessageBox.information(self, 'Web 服务', '从程序入口启动或连接后台后可打开 Web 管理。')

    def _update_config(self):
        s = self.settings
        if s.get('role') == 'viewer':
            self.config_label.setText(f"{s['station_name']} · 只读查看，请在 Web 中检索和回放录像")
            return
        role = dict(integrated='本机录像与保存', station='录像工位', center='保存中心').get(s['role'], s['role'])
        self.config_label.setText(f"{s['station_name']} · {role}\n{s['width']}×{s['height']} · {s['fps']} FPS · {s['encoder']} · "
                                  f"{'带麦克风声音' if s['audio_enabled'] else '无声音'} · MKV\n"
                                  f"摄像头：{s['camera'] or '未选择'} · 麦克风：{s['microphone'] or '未选择'}")

    def update_storage(self):
        try:
            if getattr(self.controller, 'remote', False):
                if self.settings.get('role') == 'viewer':
                    self.disk_label.setText('只读查看端')
                    return
                storage = self.controller.status['storage']
                remaining = self.controller.jobs.count()
                def space(value):
                    return '不可用' if value is None else f'{value:.1f} GB'
                self.disk_label.setText(f"归档盘 {space(storage['archive'])} · 临时盘 {space(storage['staging'])} · 待恢复 {remaining} 个")
                self.recover_button.setText(f"恢复保存任务（{remaining}）")
                return
            path = Path(self.settings["record_path"])
            if not self.controller.storage.volume_valid(path):
                self.disk_label.setText('录像目录或磁盘标识不可用，请管理员检查挂载' if self.settings.get('setup_complete') else
                                       '请通过“设置”打开 Web，完成设备和保存目录配置')
                return
            staging = self.base_dir / "recordings-in-progress"
            staging.mkdir(parents=True, exist_ok=True)
            archive_gb = shutil.disk_usage(path).free / 1024**3
            staging_gb = shutil.disk_usage(staging).free / 1024**3
            remaining = self.controller.jobs.count()
            self.disk_label.setText(f"归档盘 {archive_gb:.1f} GB · 临时盘 {staging_gb:.1f} GB · 待恢复 {remaining} 个")
            color = "#b91c1c" if min(archive_gb, staging_gb) < self.settings["disk_warn_gb"] else "#15803d"
            self.disk_label.setStyleSheet(f"color: {color};")
            self.recover_button.setText(f"恢复保存任务（{remaining}）")
        except OSError as error:
            self.disk_label.setText(f"存储检查失败：{error}")
        except Exception as error:
            self.disk_label.setText(f"恢复任务检查失败：{error}")

    def _backend_event(self, kind, data):
        if self.exit_ready:
            return
        if kind == "state":
            state = data["state"]
            if data['current'] != getattr(self, 'announcement_tracking', None):
                self.announcement_tracking = data['current']
                self.announcement_label.clear()
                self.announcement_label.hide()
                self.speech.clear()
            self.tracking_label.setText(f"当前单号：{data['current'] or data['fault_tracking'] or '—'}")
            self.pending_label.setText(f"最新待处理单号：{data['pending'] or '—'}")
            self.state_label.setText(STATE_LABELS.get(state, state))
            fault = bool(data["fault_tracking"] or data["error"])
            self.fault_label.setVisible(fault)
            self.retry_button.setVisible(fault)
            self.retry_button.setEnabled(state == "fault" and self.controller.session is None and not self.exiting)
            self.fault_label.setPlainText(f"扫码已锁定，保留单号：{data['fault_tracking'] or '—'}\n{data['error'][:1500]}")
            self.input.setEnabled(self.controller.settings["role"] in ("integrated", "station") and state not in ("fault", "disconnected", "closed", "testing") and not fault and not self.exiting)
            configurable = state in ("idle", "fault") and self.controller.session is None and not data["pending"] and not self.exiting and self.controller.settings["role"] != "viewer"
            self.settings_button.setEnabled(configurable)
            self.recover_button.setEnabled(configurable)
            self.preview_button.setEnabled(state == "idle" and configurable and self.controller.settings["role"] in ("integrated", "station"))
            self.stop_button.setEnabled(self.controller.settings["role"] in ("integrated", "station") and state not in ("fault", "closed", "recovering", "disconnected", "testing") and not self.exiting)
            if state == "disconnected":
                self.preview_label.setText("与录像服务断开，正在重连…")
            if state in ("idle", "fault") and self.controller.session is None:
                self.preview_label.clear()
                self.preview_label.setText("等待扫码" if state == "idle" else "采集故障，请检查设备和设置")
        elif kind == 'announcement':
            if self.settings.get('role') in ('integrated', 'station'):
                self.announcement_label.setPlainText(data['text'])
                self.announcement_label.show()
                self.speech.say(data)
                if data.get('warning'):
                    QApplication.beep()
        elif kind == "log":
            self.log(str(data))
        elif kind == "beep":
            QApplication.beep()
        elif kind == "progress":
            audio = " · 音频已输出" if "audio_time" in data else ""
            self.progress_label.setText(f"帧数 {data.get('frame', '—')} · FPS {data.get('fps', '—')} · "
                                        f"速度 {data.get('speed', '—')} · 丢帧 {data.get('drop_frames', '—')}{audio}")
        elif kind == "saved":
            self.update_storage()
        elif kind == "preview_error":
            QMessageBox.warning(self, "试拍失败", str(data))

    def tick(self):
        self.previous_session, self.previous_serial, frame = self.controller.latest_frame(self.previous_session, self.previous_serial)
        if frame is not None:
            image = QImage(frame, PREVIEW_WIDTH, PREVIEW_HEIGHT, PREVIEW_WIDTH * 3, QImage.Format_RGB888).copy()
            self.preview_label.setPixmap(QPixmap.fromImage(image).scaled(self.preview_label.size(), Qt.KeepAspectRatio, Qt.FastTransformation))
        live_settings = self.controller.settings
        if live_settings != self.settings:
            self.settings = dict(live_settings)
            self._update_config()
        snapshot = self.controller.snapshot()
        if snapshot["started_monotonic"] and snapshot["state"] == "recording":
            elapsed = int(time.monotonic() - snapshot["started_monotonic"])
            self.state_label.setText(f"录制中 · {elapsed // 60:02d}:{elapsed % 60:02d}")
        if self.exiting and not self.controller.thread.is_alive():
            self.exit_ready = True
            self.close()

    def closeEvent(self, event):
        if self.exit_ready:
            self.controller.notify = lambda *_: None
            QApplication.instance().removeEventFilter(self)
            self.tick_timer.stop()
            self.disk_timer.stop()
            self.speech.close()
            self.log_handler.close()
            self.logger.removeHandler(self.log_handler)
            event.accept()
            return
        if getattr(self.controller, 'remote', False):
            self.controller.close()
            self.exit_ready = True
            self.closeEvent(event)
            return
        event.ignore()
        if self.exiting:
            return
        snapshot = self.controller.snapshot()
        try:
            remaining = self.controller.jobs.count()
        except Exception as error:
            QMessageBox.critical(self, "无法检查恢复任务", str(error))
            return
        if snapshot["fault_tracking"] or remaining:
            answer = QMessageBox.question(self, "退出确认", "仍有未可靠录制的单号或未完成任务。\n"
                                           "正在录制的内容会先停止保存，恢复任务会保留。确认退出？",
                                           QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return
        self.exiting = True
        self.scan_timer.stop()
        self.input.clear()
        self.controller.close()
        self.state_label.setText("正在停止并保存，请稍候…")
        for button in (self.settings_button, self.retry_button, self.recover_button, self.preview_button, self.stop_button):
            button.setEnabled(False)
        self.input.setEnabled(False)


def self_test(app):
    """Headless package check; it never opens a camera or changes real settings."""
    report = dict(version=APP_VERSION, system=platform.system(), machine=platform.machine(),
                  python=platform.python_version(), qt=qVersion(), hardware_capture_tested=False)
    try:
        with tempfile.TemporaryDirectory(prefix="pack-record-check-") as directory:
            window = RecorderApp(directory)
            window.show()
            QTimer.singleShot(100, window.close)
            QTimer.singleShot(5000, app.quit)
            app.exec()
            window.controller.close()
            window.controller.thread.join(timeout=5)
            if window.controller.thread.is_alive():
                raise RuntimeError("界面关闭后录制线程未停止")
            report["gui"] = "ok"
            settings = RecorderSettingsStore(directory).defaults()
            for tool in ("ffmpeg", "ffprobe"):
                executable = find_tool(tool)
                code, output = run_tool([executable, "-version"])
                if code:
                    raise RuntimeError(f"{tool} 无法运行")
                report[tool] = output.splitlines()[0]
            encoders = available_encoders(settings)
            report["encoders"] = sorted(encoders & {"h264_qsv", "hevc_qsv", "aac", "libx264", "libx265"})
            if not {"h264_qsv", "hevc_qsv", "aac"} <= encoders:
                raise RuntimeError("FFmpeg 缺少 QSV 或 AAC 编译支持")
        report["status"] = "ok"
    except Exception as error:
        report.update(status="failed", error=str(error))
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "ok" else 1


def main():
    import argparse
    parser = argparse.ArgumentParser(description="PackRecord desktop")
    parser.add_argument('--connect', action='store_true', help='连接本机后台服务')
    parser.add_argument('--data-dir', type=Path, default=runtime_dir())
    parser.add_argument('--port', type=int, default=18765)
    parser.add_argument('--token-file', type=Path)
    parser.add_argument('--server-url', help='查看或管理其他 PackRecord 节点')
    parser.add_argument('--host', choices=('127.0.0.1', '0.0.0.0'))
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    app = QApplication([sys.argv[0]])
    app.setApplicationName("pack-record")
    app.setApplicationVersion(APP_VERSION)
    if "--self-test" in sys.argv:
        return self_test(app)
    base = args.data_dir
    key = hashlib.sha256(str(base).encode()).hexdigest()[:20]
    lock = QLockFile(str(Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation)) / f"ecom-recorder-{key}.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.warning(None, "程序已运行", "这个目录中的发货录像程序已经运行。")
        return 1
    data_lock = None
    server = server_thread = None
    try:
        if args.connect:
            from service_client import ServiceClient, RemoteController, RemoteSettingsStore
            token = (args.token_file or base / 'service.token').read_text().strip()
            client = ServiceClient(token, args.port, args.server_url)
            window = RecorderApp(base, lambda settings, directory, notify:
                                 RemoteController(settings, directory, notify, client),
                                 RemoteSettingsStore(client, base))
        else:
            from recorder_service import DataLock
            data_lock = DataLock(base)
            data_lock.__enter__()
            window = RecorderApp(base)
            import threading
            from recorder_service import RecorderService, RecorderHTTPServer, load_token
            service = RecorderService(base, controller=window.controller)
            server = RecorderHTTPServer((args.host or window.settings['web_bind'], args.port), service,
                                        load_token(base), load_token(base, 'viewer.token'))
            server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .2}, daemon=True)
            server_thread.start()
            window.web_url = f'http://127.0.0.1:{server.server_port}'
            window.controller.manager.viewer_access = lambda: dict(viewer_token=server.viewer_token)
    except Exception as error:
        if 'window' in locals():
            window.controller.close()
            window.controller.thread.join()
        if data_lock:
            data_lock.__exit__()
        QMessageBox.critical(None, "无法启动", f"请检查服务连接或数据目录权限。\n{error}")
        return 1
    window.show()
    try:
        return app.exec()
    finally:
        if server:
            server.shutdown()
            server.server_close()
            server_thread.join()
        window.controller.close()
        window.controller.thread.join()
        lock.unlock()
        if data_lock:
            data_lock.__exit__()


if __name__ == "__main__":
    sys.exit(main())
