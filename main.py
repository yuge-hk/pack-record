# -*- coding: utf-8 -*-
import sys
import os
import csv
import logging
import time
import threading
import shutil
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLineEdit,
    QLabel, QTextEdit, QPushButton, QCheckBox, QHBoxLayout,
    QMessageBox, QInputDialog
)
from PySide6.QtCore import QTimer, Qt, Signal, QEvent
from PySide6.QtGui import QFont
import obsws_python as obs
from app_core import (
    FailedJobStore,
    VideoDB,
    ensure_copy_space,
    move_recording_file,
    sanitize_tracking_filename,
    unique_filename,
)

# ================= 配置 =================
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = "83vMnnkOgq0nHu6l"
BASE_DIR = Path(__file__).resolve().parent
RECORD_PATH = str(BASE_DIR / "recordings")   # 基础目录
MAX_RECORD_DURATION_MS = 20 * 60 * 1000       # 最长 20 分钟
DISK_WARN_GB = 40                             # 磁盘小于40GB时变红色
DISK_CRITICAL_GB = 10                         # 磁盘小于10GB时禁止新录制
DISK_COPY_RESERVE_GB = 1                      # 跨盘保存时额外预留1GB
RECORD_START_CONFIRM_INTERVAL_MS = 100
RECORD_START_CONFIRM_ATTEMPTS = 50
DUPLICATE_SCAN_WINDOW_SECONDS = 1.0
JSON_DIR = BASE_DIR / "records"
LOG_DIR = BASE_DIR / "logs"
FAILED_JOBS_PATH = JSON_DIR / "failed_jobs.json"
OBS_EXECUTABLE_CANDIDATES = (
    Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"),
    Path(r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe"),
)

os.makedirs(RECORD_PATH, exist_ok=True)
JSON_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("ecom-love-obs")
logger.setLevel(logging.INFO)
if not logger.handlers:
    file_handler = RotatingFileHandler(
        LOG_DIR / "app.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    )
    logger.addHandler(file_handler)

def find_obs_executable():
    """查找 Windows 上的 OBS 主程序，不依赖额外配置。"""
    executable_on_path = shutil.which("obs64.exe")
    if executable_on_path:
        return Path(executable_on_path)
    for candidate in OBS_EXECUTABLE_CANDIDATES:
        if candidate.is_file():
            return candidate
    return None


def get_obs_process_ids():
    """返回当前 Windows 会话中的 obs64.exe 进程 ID。"""
    if sys.platform != "win32":
        return set()
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq obs64.exe", "/FO", "CSV", "/NH"],
            text=True,
            encoding="mbcs",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return set()

    process_ids = set()
    for row in csv.reader(output.splitlines()):
        if len(row) >= 2 and row[0].lower() == "obs64.exe":
            try:
                process_ids.add(int(row[1]))
            except ValueError:
                pass
    return process_ids


def find_largest_window_for_processes(process_ids):
    """查找指定进程集合中面积最大的可见顶层窗口。"""
    if sys.platform != "win32" or not process_ids:
        return None

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", wintypes.LONG),
            ("top", wintypes.LONG),
            ("right", wintypes.LONG),
            ("bottom", wintypes.LONG),
        ]

    windows = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL

    @callback_type
    def enum_window(window_handle, _):
        if not user32.IsWindowVisible(window_handle):
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
        if process_id.value not in process_ids:
            return True
        rectangle = RECT()
        if user32.GetWindowRect(window_handle, ctypes.byref(rectangle)):
            width = max(0, rectangle.right - rectangle.left)
            height = max(0, rectangle.bottom - rectangle.top)
            if width >= 300 and height >= 200:
                title_length = user32.GetWindowTextLengthW(window_handle)
                title_buffer = ctypes.create_unicode_buffer(title_length + 1)
                user32.GetWindowTextW(window_handle, title_buffer, title_length + 1)
                title = title_buffer.value.upper()
                main_window_priority = int("OBS" in title and "PROJECTOR" not in title)
                windows.append((main_window_priority, width * height, window_handle))
        return True

    user32.EnumWindows(enum_window, 0)
    return max(windows, default=(0, 0, None), key=lambda item: (item[0], item[1]))[2]


def move_windows_window(window_handle, x, y, width, height):
    """恢复并移动一个 Windows 顶层窗口。"""
    if sys.platform != "win32" or not window_handle:
        return False
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.MoveWindow.argtypes = [
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.BOOL,
    ]
    user32.MoveWindow.restype = wintypes.BOOL
    user32.ShowWindow(window_handle, 9)  # SW_RESTORE
    return bool(user32.MoveWindow(window_handle, x, y, width, height, True))


db = VideoDB(JSON_DIR)
failed_jobs = FailedJobStore(FAILED_JOBS_PATH)
INSTANCE_MUTEX_HANDLE = None


def acquire_single_instance():
    """Windows 下只允许启动一个录制控制程序实例。"""
    global INSTANCE_MUTEX_HANDLE
    if sys.platform != "win32":
        return True

    import ctypes

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(
        None,
        False,
        "Local\\ecom-love-obs-single-instance",
    )
    if not handle:
        return True
    if kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        kernel32.CloseHandle(handle)
        return False
    INSTANCE_MUTEX_HANDLE = handle
    return True


class OBSDisplayWindow(QWidget):
    """可供 OBS 窗口采集的绿幕信息窗口。"""

    hide_requested = Signal()

    def __init__(self):
        super().__init__()
        self.setWindowTitle("录制信息 - OBS 采集窗口")
        self.resize(800, 400)
        self.setStyleSheet("background-color: #00FF00;")

        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        self.tracking_label = QLabel("单号：-")
        self.tracking_label.setAlignment(Qt.AlignCenter)
        self.tracking_label.setFont(QFont("Microsoft YaHei", 40, QFont.Bold))
        self.tracking_label.setStyleSheet(
            "color: #FFFFFF; background-color: #000000; padding: 20px; border-radius: 10px;"
        )
        layout.addWidget(self.tracking_label)

        time_layout = QHBoxLayout()
        time_font = QFont("Microsoft YaHei", 24, QFont.Bold)

        self.current_time_label = QLabel("当前时间：--:--:--")
        self.current_time_label.setAlignment(Qt.AlignCenter)
        self.current_time_label.setFont(time_font)
        self.current_time_label.setStyleSheet(
            "color: #FFFFFF; background-color: #0066CC; padding: 15px; border-radius: 8px;"
        )
        time_layout.addWidget(self.current_time_label)

        self.duration_label = QLabel("已录制：00:00")
        self.duration_label.setAlignment(Qt.AlignCenter)
        self.duration_label.setFont(time_font)
        self.duration_label.setStyleSheet(
            "color: #FFFFFF; background-color: #CC0000; padding: 15px; border-radius: 8px;"
        )
        time_layout.addWidget(self.duration_label)
        layout.addLayout(time_layout)

        self.setLayout(layout)
        self.disk_free_gb = None
        self.recording_started_at = None
        self.clock_timer = QTimer(self)
        self.clock_timer.timeout.connect(self.update_clock)
        self.clock_timer.start(1000)
        self.update_clock()

    def update_clock(self):
        self.current_time_label.setText(f"当前时间：{datetime.now():%H:%M:%S}")
        if self.recording_started_at:
            elapsed = max(0, int((datetime.now() - self.recording_started_at).total_seconds()))
            self.duration_label.setText(f"已录制：{elapsed // 60:02d}:{elapsed % 60:02d}")

    def update_recording(self, tracking_number, start_time):
        self.tracking_label.setText(f"单号：{tracking_number or '-'}")
        if start_time:
            try:
                self.recording_started_at = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                self.recording_started_at = None
        else:
            self.recording_started_at = None
            self.duration_label.setText("已录制：00:00")

    def closeEvent(self, event):
        event.ignore()
        self.hide()
        self.hide_requested.emit()


class RecorderApp(QWidget):
    obs_connection_ready = Signal(object, object, str)
    obs_connection_failed = Signal(str)
    obs_scene_changed = Signal(str)
    worker_log = Signal(str)
    recording_finalized = Signal(object, bool, str)
    failed_jobs_retry_finished = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("发货视频录制")
        self.resize(600, 550)

        layout = QVBoxLayout()

        # ===== 输入区 =====
        self.label = QLabel("条码枪扫描快递单号后回车即可开始录制：")
        layout.addWidget(self.label)

        self.input = QLineEdit()
        self.input.returnPressed.connect(self.handle_input)
        layout.addWidget(self.input)

        self.stop_button = QPushButton("停止录制")
        self.stop_button.clicked.connect(self.stop_recording)
        self.stop_button.clicked.connect(
            lambda _checked=False: QTimer.singleShot(0, self.focus_scan_input)
        )
        layout.addWidget(self.stop_button)

        self.display_checkbox = QCheckBox("显示 OBS 信息窗口")
        self.display_checkbox.setChecked(False)
        layout.addWidget(self.display_checkbox)

        # ===== 信息区 =====
        self.obs_connection_label = QLabel("OBS 连接：等待连接")
        self.obs_connection_label.setStyleSheet("font-size: 14px; color: #b7791f;")
        layout.addWidget(self.obs_connection_label)

        self.tracking_label = QLabel("当前单号：-")
        self.tracking_label.setStyleSheet("font-size: 28px; font-weight: bold; color: blue;")
        layout.addWidget(self.tracking_label)

        self.state_label = QLabel("录制状态：-")
        self.state_label.setStyleSheet("font-size: 20px; color: gray; font-weight: bold;")
        layout.addWidget(self.state_label)

        self.info_label = QLabel("当前场景：-")
        self.info_label.setStyleSheet("font-size: 14px;")
        layout.addWidget(self.info_label)

        self.disk_label = QLabel("磁盘剩余：-")
        self.disk_label.setStyleSheet("font-size: 14px; color: green;")
        layout.addWidget(self.disk_label)

        self.pending_label = QLabel("最新待处理单号：-")
        self.pending_label.setStyleSheet("font-size: 14px; color: #805ad5;")
        layout.addWidget(self.pending_label)

        self.failed_jobs_label = QLabel("待恢复文件：0")
        self.failed_jobs_label.setStyleSheet("font-size: 14px; color: gray;")
        layout.addWidget(self.failed_jobs_label)

        recovery_buttons = QHBoxLayout()
        self.retry_failed_button = QPushButton("重试保存失败任务")
        self.retry_failed_button.clicked.connect(self.retry_failed_jobs)
        recovery_buttons.addWidget(self.retry_failed_button)
        self.resolve_orphan_button = QPushButton("处理 OBS 当前录像")
        self.resolve_orphan_button.clicked.connect(self.resolve_orphan_recording)
        self.resolve_orphan_button.hide()
        recovery_buttons.addWidget(self.resolve_orphan_button)
        layout.addLayout(recovery_buttons)

        # ===== 日志区 =====
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        self.setLayout(layout)

        # 定时器：最长 20 分钟自动停止
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.force_stop_recording)

        # 定时器：每5秒轮询一次OBS状态（兜底）
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_obs_status)
        self.poll_timer.start(5000)

        # 定时器：OBS 未启动或断线后每3秒自动尝试重连
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.timeout.connect(self.ensure_obs_connection)
        self.reconnect_timer.start(3000)

        # 定时器：每10秒刷新磁盘空间
        self.disk_timer = QTimer(self)
        self.disk_timer.timeout.connect(self.update_disk_space)
        self.disk_timer.start(10000)
        self.update_disk_space()

        # 状态变量
        self.current_tracking_number = None
        self.start_time = None
        self.current_scene = "-"
        self.recording_phase = "idle"
        self.record_start_check_attempts = 0
        self.pending_tracking_number = None
        self.stop_requested = False
        self.last_scan_number = None
        self.last_scan_monotonic = 0.0
        self.scanner_buffer = ""
        self.exit_after_finalize = False
        self.force_closing = False
        self.orphan_recording_blocked = False
        self.req_client = None
        self.event_client = None
        self.connection_in_progress = False
        self.connection_error_logged = False
        self.obs_process = None
        self.windows_arranged = False
        self.window_arrange_attempts = 0

        self.window_arrange_timer = QTimer(self)
        self.window_arrange_timer.setInterval(500)
        self.window_arrange_timer.timeout.connect(self._try_arrange_windows)

        self.scanner_buffer_timer = QTimer(self)
        self.scanner_buffer_timer.setSingleShot(True)
        self.scanner_buffer_timer.setInterval(300)
        self.scanner_buffer_timer.timeout.connect(self._clear_scanner_buffer)

        self.display_window = OBSDisplayWindow()
        self.display_checkbox.toggled.connect(self._set_display_window_visible)
        self.display_window.hide_requested.connect(
            lambda: self.display_checkbox.setChecked(False)
        )

        self.obs_connection_ready.connect(self._on_obs_connection_ready)
        self.obs_connection_failed.connect(self._on_obs_connection_failed)
        self.obs_scene_changed.connect(self._on_obs_scene_changed)
        self.worker_log.connect(self.log)
        self.recording_finalized.connect(self._on_recording_finalized)
        self.failed_jobs_retry_finished.connect(self._on_failed_jobs_retry_finished)
        QApplication.instance().installEventFilter(self)
        self.update_failed_jobs_status()
        QTimer.singleShot(0, self.ensure_obs_connection)
        QTimer.singleShot(0, self.launch_obs_and_prepare_windows)
        QTimer.singleShot(100, self.focus_scan_input)

    # ---------------- UI辅助 ----------------
    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.log_box.append(line)
        print(line)
        logger.info(msg)
        self.update_info()

    def _clear_scanner_buffer(self):
        self.scanner_buffer = ""

    def eventFilter(self, watched, event):
        if (
            event.type() != QEvent.KeyPress
            or watched is self.input
            or QApplication.activeModalWidget() is not None
        ):
            return super().eventFilter(watched, event)

        key = event.key()
        if key in (Qt.Key_Return, Qt.Key_Enter):
            if self.scanner_buffer:
                tracking_number = self.scanner_buffer
                self._clear_scanner_buffer()
                self.input.setText(tracking_number)
                self.handle_input()
                return True
            return super().eventFilter(watched, event)

        blocked_modifiers = Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier
        text = event.text()
        if text and text.isprintable() and not (event.modifiers() & blocked_modifiers):
            self.scanner_buffer += text
            self.scanner_buffer_timer.start()
            return True
        return super().eventFilter(watched, event)

    def set_state(self, state: str):
        if state == "录制中":
            self.state_label.setText("录制状态：录制中")
            self.state_label.setStyleSheet("font-size: 20px; color: green; font-weight: bold;")
        elif state == "保存中":
            self.state_label.setText("录制状态：保存中")
            self.state_label.setStyleSheet("font-size: 20px; color: blue; font-weight: bold;")
        elif state == "已停止":
            self.state_label.setText("录制状态：已停止")
            self.state_label.setStyleSheet("font-size: 20px; color: gray; font-weight: bold;")
        else:
            self.state_label.setText(f"录制状态：{state}")
            self.state_label.setStyleSheet("font-size: 20px; color: black; font-weight: bold;")

    def update_info(self):
        tracking = self.current_tracking_number if self.current_tracking_number else "-"
        self.tracking_label.setText(f"当前单号：{tracking}")
        self.info_label.setText(f"当前场景：{self.current_scene}")
        self.display_window.update_recording(
            self.current_tracking_number,
            self.start_time,
        )

    def set_pending_tracking(self, tracking_number):
        """只保留最新一次扫描，不建立扫码队列。"""
        self.pending_tracking_number = tracking_number or None
        self.pending_label.setText(
            f"最新待处理单号：{self.pending_tracking_number or '-'}"
        )

    def update_failed_jobs_status(self):
        count = failed_jobs.count()
        self.failed_jobs_label.setText(f"待恢复文件：{count}")
        color = "red" if count else "gray"
        self.failed_jobs_label.setStyleSheet(
            f"font-size: 14px; color: {color};"
        )
        self.retry_failed_button.setEnabled(count > 0)

    @staticmethod
    def _failed_job_payload(
        tracking_number,
        source_path,
        start_time,
        end_time,
        duration,
        resolution,
        fps,
        error,
        archived_path=None,
    ):
        return {
            "tracking_number": tracking_number,
            "source_path": source_path,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "resolution": resolution,
            "fps": fps,
            "last_error": str(error),
            "archived_path": archived_path,
        }

    def retry_failed_jobs(self):
        self.retry_failed_button.setEnabled(False)
        self.log("正在重试保存失败任务…")
        threading.Thread(target=self._retry_failed_jobs_worker, daemon=True).start()

    def _retry_failed_jobs_worker(self):
        remaining = []
        succeeded = 0
        jobs_to_retry = failed_jobs.list()
        processed_ids = {job.get("id") for job in jobs_to_retry}
        for job in jobs_to_retry:
            source_path = job.get("source_path")
            try:
                archived_path = job.get("archived_path")
                if archived_path and os.path.isfile(archived_path):
                    destination = archived_path
                else:
                    if not source_path or not os.path.isfile(source_path):
                        raise FileNotFoundError("OBS 输出文件不存在，需人工检查 OBS 输出目录")
                    date_text = (job.get("end_time") or time.strftime("%Y-%m-%d"))[:10]
                    date_dir = os.path.join(RECORD_PATH, date_text)
                    os.makedirs(date_dir, exist_ok=True)
                    extension = os.path.splitext(source_path)[1] or ".mkv"
                    filename = f"{sanitize_tracking_filename(job.get('tracking_number'))}{extension}"
                    destination = unique_filename(date_dir, filename)
                    ensure_copy_space(source_path, date_dir, DISK_COPY_RESERVE_GB)
                    move_recording_file(source_path, destination)
                file_size = os.path.getsize(destination)
                db.insert_record(
                    job.get("tracking_number"),
                    job.get("start_time"),
                    job.get("end_time"),
                    job.get("duration"),
                    destination,
                    file_size,
                    job.get("resolution", "-"),
                    job.get("fps", 0),
                )
                succeeded += 1
                self.worker_log.emit(
                    f"失败任务已恢复：{job.get('tracking_number')} → {destination}"
                )
            except Exception as exc:
                job["last_error"] = str(exc)
                job["last_retry_at"] = datetime.now().isoformat(timespec="seconds")
                remaining.append(job)
                self.worker_log.emit(
                    f"失败任务仍无法保存：{job.get('tracking_number')} ({exc})"
                )
        failed_jobs.finish_retry(processed_ids, remaining)
        self.failed_jobs_retry_finished.emit(succeeded, failed_jobs.count())

    def _on_failed_jobs_retry_finished(self, succeeded, remaining):
        self.update_failed_jobs_status()
        self.log(f"失败任务重试完成：成功 {succeeded}，剩余 {remaining}")
        if remaining:
            QMessageBox.warning(
                self,
                "仍有文件未恢复",
                f"成功恢复 {succeeded} 个，仍有 {remaining} 个。请检查磁盘空间和 OBS 输出目录。",
            )

    def resolve_orphan_recording(self):
        """处理程序启动前已经由 OBS 开始的录像。"""
        client = self.req_client
        if client is None:
            return
        try:
            status = client.get_record_status()
            if not bool(getattr(status, "output_active", False)):
                self.orphan_recording_blocked = False
                self.resolve_orphan_button.hide()
                self.set_state("已停止")
                return
        except Exception as exc:
            self.mark_obs_disconnected(exc)
            return

        dialog = QMessageBox(self)
        dialog.setWindowTitle("检测到 OBS 正在录制")
        dialog.setText("OBS 已在录制，但程序不知道对应单号。请选择处理方式。")
        takeover_button = dialog.addButton("接管当前录像", QMessageBox.AcceptRole)
        stop_button = dialog.addButton("停止并保存孤立录像", QMessageBox.DestructiveRole)
        keep_button = dialog.addButton("保持录制并暂不处理", QMessageBox.RejectRole)
        dialog.exec()
        clicked = dialog.clickedButton()

        if clicked is takeover_button:
            tracking_number, accepted = QInputDialog.getText(
                self,
                "接管 OBS 录像",
                "请输入当前录像对应的快递单号：",
            )
            tracking_number = tracking_number.strip()
            if not accepted or not tracking_number:
                self.orphan_recording_blocked = True
                self.resolve_orphan_button.show()
                self.set_state("OBS 已在录制，扫码禁用")
                return
            self.current_tracking_number = tracking_number
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.recording_phase = "recording"
            self.orphan_recording_blocked = False
            self.resolve_orphan_button.hide()
            self.timer.start(MAX_RECORD_DURATION_MS)
            self.set_state("录制中")
            self.log(f"已接管 OBS 当前录像：{tracking_number}（开始时间按接管时刻计算）")
            return

        if clicked is stop_button:
            self.current_tracking_number = f"ORPHAN_{datetime.now():%Y%m%d_%H%M%S}"
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.recording_phase = "recording"
            self.orphan_recording_blocked = False
            self.resolve_orphan_button.hide()
            self.log("正在停止并保存启动前已存在的 OBS 录像")
            self.stop_recording()
            return

        if clicked is keep_button:
            self.orphan_recording_blocked = True
            self.resolve_orphan_button.show()
            self.set_state("OBS 已在录制，扫码禁用")
            self.log("OBS 当前录像保持运行；处理前扫码功能已禁用")

    # ---------------- Windows 窗口管理 ----------------
    def _set_display_window_visible(self, visible):
        self.display_window.setVisible(visible)
        QTimer.singleShot(100, self.focus_scan_input)

    def focus_scan_input(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()
        self.input.setFocus(Qt.OtherFocusReason)
        if sys.platform == "win32":
            try:
                import ctypes
                from ctypes import wintypes
                user32 = ctypes.windll.user32
                user32.SetForegroundWindow.argtypes = [wintypes.HWND]
                user32.SetForegroundWindow(int(self.winId()))
            except Exception:
                pass

    def launch_obs_and_prepare_windows(self):
        if sys.platform != "win32":
            self.focus_scan_input()
            return

        if not get_obs_process_ids():
            executable = find_obs_executable()
            if executable is None:
                self.log("未找到 OBS 安装目录，请手动启动 OBS；连接恢复后会继续运行")
            else:
                try:
                    self.obs_process = subprocess.Popen(
                        [str(executable)],
                        cwd=str(executable.parent),
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.log(f"已启动 OBS：{executable}")
                except OSError as exc:
                    self.log(f"无法启动 OBS，请手动打开：{exc}")
        self.start_window_arrangement()

    def start_window_arrangement(self):
        if sys.platform != "win32" or self.windows_arranged:
            return
        self.window_arrange_attempts = 0
        if not self.window_arrange_timer.isActive():
            self.window_arrange_timer.start()
        self._try_arrange_windows()

    def _try_arrange_windows(self):
        if sys.platform != "win32":
            self.window_arrange_timer.stop()
            return

        self.window_arrange_attempts += 1
        obs_window = find_largest_window_for_processes(get_obs_process_ids())
        if obs_window:
            screen = self.screen() or QApplication.primaryScreen()
            geometry = screen.availableGeometry()
            left_width = geometry.width() // 2
            right_width = geometry.width() - left_width
            self.setGeometry(
                geometry.x(),
                geometry.y(),
                left_width,
                geometry.height(),
            )
            moved = move_windows_window(
                obs_window,
                geometry.x() + left_width,
                geometry.y(),
                right_width,
                geometry.height(),
            )
            if moved:
                self.windows_arranged = True
                self.window_arrange_timer.stop()
                self.log("窗口已排列：录制控制在左，OBS 在右")
                QTimer.singleShot(300, self.focus_scan_input)
                return

        if self.window_arrange_attempts >= 60:
            self.window_arrange_timer.stop()
            self.log("暂未找到 OBS 主窗口，连接成功后会再次尝试排列")
            self.focus_scan_input()

    # ---------------- OBS 连接管理 ----------------
    def ensure_obs_connection(self):
        if self.req_client is not None or self.connection_in_progress:
            return
        self.connection_in_progress = True
        self.obs_connection_label.setText("OBS 连接：正在连接…")
        self.obs_connection_label.setStyleSheet("font-size: 14px; color: #b7791f;")
        threading.Thread(target=self._connect_obs_worker, daemon=True).start()

    def _connect_obs_worker(self):
        try:
            request_client = obs.ReqClient(
                host=OBS_WS_HOST,
                port=OBS_WS_PORT,
                password=OBS_WS_PASSWORD,
                timeout=2,
            )
        except Exception as exc:
            self.obs_connection_failed.emit(str(exc))
            return

        event_client_instance = None
        event_warning = ""
        try:
            event_client_instance = obs.EventClient(
                host=OBS_WS_HOST,
                port=OBS_WS_PORT,
                password=OBS_WS_PASSWORD,
                timeout=2,
            )
            event_client_instance.callback.register(
                self.on_current_program_scene_changed
            )
        except Exception as exc:
            event_warning = str(exc)

        self.obs_connection_ready.emit(
            request_client,
            event_client_instance,
            event_warning,
        )

    def on_current_program_scene_changed(self, data):
        scene = None
        if isinstance(data, dict):
            scene = data.get("sceneName") or data.get("scene_name")
        else:
            scene = getattr(data, "scene_name", None) or getattr(data, "sceneName", None)
        if scene:
            self.obs_scene_changed.emit(str(scene))

    def _on_obs_connection_ready(self, request_client, event_client_instance, event_warning):
        self.connection_in_progress = False
        self.req_client = request_client
        self.event_client = event_client_instance
        self.connection_error_logged = False
        self.obs_connection_label.setText("OBS 连接：已连接")
        self.obs_connection_label.setStyleSheet("font-size: 14px; color: green; font-weight: bold;")
        if event_warning:
            self.log("OBS WebSocket 已连接，事件监听不可用，将使用状态轮询")
        else:
            self.log("OBS WebSocket 已连接")
        self.start_window_arrangement()
        self.poll_obs_status()
        if self.current_tracking_number is not None:
            QTimer.singleShot(0, self._resume_recording_after_reconnect)
            return
        try:
            status = request_client.get_record_status()
            obs_is_recording = bool(getattr(status, "output_active", False))
        except Exception as exc:
            self.mark_obs_disconnected(exc)
            return
        if obs_is_recording:
            self.resolve_orphan_recording()
            return
        if self.pending_tracking_number and self.current_tracking_number is None:
            tracking_number = self.pending_tracking_number
            self.set_pending_tracking(None)
            self.start_recording(tracking_number)

    def _on_obs_connection_failed(self, error_message):
        self.connection_in_progress = False
        self.req_client = None
        self.event_client = None
        self.obs_connection_label.setText("OBS 连接：等待 OBS 启动")
        self.obs_connection_label.setStyleSheet("font-size: 14px; color: #b7791f;")
        if not self.connection_error_logged:
            self.log(f"OBS 尚未就绪，程序将自动重连：{error_message}")
            self.connection_error_logged = True

    def _on_obs_scene_changed(self, scene):
        self.current_scene = scene
        self.log(f"[事件] 场景切换到：{scene}")

    def _resume_recording_after_reconnect(self):
        client = self.req_client
        if client is None or self.current_tracking_number is None:
            return
        try:
            status = client.get_record_status()
            is_recording = bool(getattr(status, "output_active", False))
        except Exception as exc:
            self.mark_obs_disconnected(exc)
            return

        if is_recording:
            self.recording_phase = "recording"
            if self.start_time is None:
                self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            if not self.timer.isActive():
                self.timer.start(MAX_RECORD_DURATION_MS)
            self.set_state("录制中")
            if self.stop_requested or self.pending_tracking_number:
                self.stop_requested = False
                QTimer.singleShot(0, self.stop_recording)
            return

        interrupted_tracking_number = self.current_tracking_number
        self.log(f"OBS 重连后确认未在录制，放弃未完成单号：{interrupted_tracking_number}")
        self._reset_recording_state_and_continue()

    @staticmethod
    def _disconnect_client(client):
        if client is None:
            return
        for method_name in ("disconnect", "unsubscribe", "close"):
            method = getattr(client, method_name, None)
            if callable(method):
                try:
                    method()
                except Exception:
                    pass
                return
        websocket = getattr(getattr(client, "base_client", None), "ws", None)
        close_websocket = getattr(websocket, "close", None)
        if callable(close_websocket):
            try:
                close_websocket()
            except Exception:
                pass

    def mark_obs_disconnected(self, reason):
        self._disconnect_client(self.event_client)
        self._disconnect_client(self.req_client)
        self.event_client = None
        self.req_client = None
        self.connection_in_progress = False
        self.current_scene = "-"
        if self.current_tracking_number is not None:
            self.recording_phase = "waiting_reconnect"
        self.windows_arranged = False
        self.start_window_arrangement()
        self.obs_connection_label.setText("OBS 连接：连接已断开，等待重连")
        self.obs_connection_label.setStyleSheet("font-size: 14px; color: #b7791f;")
        self.log(f"OBS 连接已断开，程序将自动重连：{reason}")

    def update_disk_space(self):
        try:
            usage = shutil.disk_usage(RECORD_PATH)
            free_gb = usage.free / (1024**3)
            self.disk_free_gb = free_gb
            if free_gb < DISK_CRITICAL_GB:
                self.disk_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")
                suffix = "（空间不足，禁止新录制）"
            elif free_gb < DISK_WARN_GB:
                self.disk_label.setStyleSheet("font-size: 14px; color: #b7791f; font-weight: bold;")
                suffix = "（请尽快清理）"
            else:
                self.disk_label.setStyleSheet("font-size: 14px; color: green;")
                suffix = ""
            self.disk_label.setText(f"磁盘剩余：{free_gb:.1f} GB {suffix}")
        except Exception as e:
            self.disk_free_gb = None
            self.disk_label.setText(f"磁盘剩余：未知 ({e})")
            self.disk_label.setStyleSheet("font-size: 14px; color: gray;")

    # ---------------- 主逻辑 ----------------
    def handle_input(self):
        tracking_number = self.input.text().strip()
        self.input.setText("")
        if not tracking_number:
            return

        # 特殊指令 STOPSTOP
        if tracking_number.upper() == "STOPSTOP":
            self.log("检测到 STOPSTOP 指令 → 停止录制")
            self.stop_recording()
            return

        if self.orphan_recording_blocked:
            self.log(f"OBS 存在未处理录像，忽略扫码：{tracking_number}")
            return

        scan_time = time.monotonic()
        if (
            tracking_number == self.last_scan_number
            and scan_time - self.last_scan_monotonic <= DUPLICATE_SCAN_WINDOW_SECONDS
        ):
            self.log(f"忽略 1 秒内的重复扫码：{tracking_number}")
            return
        self.last_scan_number = tracking_number
        self.last_scan_monotonic = scan_time
        if tracking_number == self.current_tracking_number:
            self.log(f"忽略当前单号的重复扫码：{tracking_number}")
            return

        if self.req_client is None:
            self.set_pending_tracking(tracking_number)
            if self.current_tracking_number is not None:
                self.stop_requested = True
                self.log(f"OBS 暂时离线，已暂存下一单号：{tracking_number}")
            else:
                self.log(f"OBS 暂时离线，已暂存单号：{tracking_number}")
            self.ensure_obs_connection()
            return
        if self.recording_phase == "idle" and self.current_tracking_number is None:
            self.start_recording(tracking_number)
        elif self.recording_phase == "starting":
            self.set_pending_tracking(tracking_number)
            self.log(f"OBS 正在启动录制，已暂存下一单号：{tracking_number}")
        elif self.recording_phase in ("stopping", "saving"):
            self.set_pending_tracking(tracking_number)
            self.log(f"上一单正在保存，已暂存下一单号：{tracking_number}")
        else:
            self.stop_recording(start_new_tracking_number=tracking_number)

    def start_recording(self, tracking_number):
        if self.recording_phase != "idle" or self.current_tracking_number is not None:
            if tracking_number != self.current_tracking_number:
                self.set_pending_tracking(tracking_number)
                self.log(f"当前状态暂不能启动，已暂存单号：{tracking_number}")
            return
        client = self.req_client
        if client is None:
            self.set_pending_tracking(tracking_number)
            self.ensure_obs_connection()
            return
        self.update_disk_space()
        if self.disk_free_gb is not None and self.disk_free_gb < DISK_CRITICAL_GB:
            self.log(
                f"磁盘仅剩 {self.disk_free_gb:.1f} GB，低于 {DISK_CRITICAL_GB} GB，"
                "已禁止开始录制"
            )
            return
        try:
            status = client.get_record_status()
            if getattr(status, "output_active", False):
                self.set_pending_tracking(tracking_number)
                self.orphan_recording_blocked = True
                self.resolve_orphan_button.show()
                self.log("⚠️ OBS 已在录制中，已保留最新单号并等待处理当前录像")
                QTimer.singleShot(0, self.resolve_orphan_recording)
                return
            client.start_record()
            self.current_tracking_number = tracking_number
            self.start_time = None
            self.recording_phase = "starting"
            self.record_start_check_attempts = 0
            self.set_state("正在启动")
            self.log(f"已请求开始录制，等待 OBS 确认：{tracking_number}")
            QTimer.singleShot(
                RECORD_START_CONFIRM_INTERVAL_MS,
                self._confirm_recording_started,
            )
        except Exception as e:
            self.log(f"无法开始录制: {e}")
            self.current_tracking_number = None
            self.start_time = None
            self.recording_phase = "idle"
            self.set_state("已停止")
            try:
                client.get_record_status()
            except Exception:
                self.mark_obs_disconnected(e)

    def _confirm_recording_started(self):
        if self.recording_phase != "starting" or self.current_tracking_number is None:
            return
        client = self.req_client
        if client is None:
            self.recording_phase = "waiting_reconnect"
            return
        try:
            status = client.get_record_status()
            is_recording = bool(getattr(status, "output_active", False))
        except Exception as exc:
            self.recording_phase = "waiting_reconnect"
            self.mark_obs_disconnected(exc)
            return

        if is_recording:
            self.recording_phase = "recording"
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.set_state("录制中")
            self.timer.start(MAX_RECORD_DURATION_MS)
            self.log(f"OBS 已确认开始录制：{self.current_tracking_number}")
            if self.stop_requested or self.pending_tracking_number:
                self.stop_requested = False
                QTimer.singleShot(0, self.stop_recording)
            return

        self.record_start_check_attempts += 1
        if self.record_start_check_attempts < RECORD_START_CONFIRM_ATTEMPTS:
            QTimer.singleShot(
                RECORD_START_CONFIRM_INTERVAL_MS,
                self._confirm_recording_started,
            )
            return

        failed_tracking_number = self.current_tracking_number
        confirmation_seconds = (
            RECORD_START_CONFIRM_INTERVAL_MS * RECORD_START_CONFIRM_ATTEMPTS / 1000
        )
        self.log(
            f"OBS 在 {confirmation_seconds:g} 秒内未进入录制状态，"
            f"取消单号：{failed_tracking_number}"
        )
        self._reset_recording_state_and_continue()

    def _reset_recording_state_and_continue(
        self,
        requested_next_tracking_number=None,
        allow_continue=True,
    ):
        self.timer.stop()
        self.current_tracking_number = None
        self.start_time = None
        self.recording_phase = "idle"
        self.record_start_check_attempts = 0
        self.stop_requested = False
        self.set_state("已停止")
        self.update_info()
        next_tracking_number = self.pending_tracking_number or requested_next_tracking_number
        self.set_pending_tracking(None)
        if self.exit_after_finalize:
            self.exit_after_finalize = False
            QTimer.singleShot(0, self._shutdown_and_quit)
            return
        if allow_continue and next_tracking_number:
            # 直接消费最新值，避免零延迟回调与下一次扫码交错后恢复旧单号。
            self.start_recording(next_tracking_number)

    def force_stop_recording(self):
        if self.current_tracking_number:
            self.log(f"⚠️ 超过 {MAX_RECORD_DURATION_MS/60000:.0f} 分钟，自动停止录制")
        self.stop_recording()

    def stop_recording(self, start_new_tracking_number=None):
        if self.current_tracking_number is None:
            return
        if start_new_tracking_number:
            self.set_pending_tracking(start_new_tracking_number)
        if self.recording_phase == "starting":
            self.stop_requested = True
            self.log("OBS 尚在确认启动，确认录制后再切换单号")
            return
        if self.recording_phase in ("stopping", "saving"):
            self.log("当前录制正在停止或保存，下一单号已暂存")
            return
        client = self.req_client
        if client is None:
            self.stop_requested = True
            self.recording_phase = "waiting_reconnect"
            self.set_state("等待 OBS 重连")
            self.ensure_obs_connection()
            return
        if self.recording_phase not in ("recording", "waiting_reconnect"):
            self.log(f"当前状态无法停止录制：{self.recording_phase}")
            return

        self.timer.stop()
        self.recording_phase = "stopping"
        try:
            resp = client.stop_record()
            output_path = getattr(resp, "output_path", None)
            self.log("录制结束，正在保存文件...")
            self.recording_phase = "saving"
            self.set_state("保存中")
        except Exception as e:
            self.log(f"停止录制失败: {e}")
            try:
                status = client.get_record_status()
                is_recording = bool(getattr(status, "output_active", False))
            except Exception:
                self.stop_requested = True
                self.recording_phase = "waiting_reconnect"
                self.set_state("等待 OBS 重连")
                self.mark_obs_disconnected(e)
                return
            if not is_recording:
                self.log("OBS 实际未在录制，已重置脚本状态")
                self._reset_recording_state_and_continue(start_new_tracking_number)
                return
            self.recording_phase = "recording"
            self.timer.start(MAX_RECORD_DURATION_MS)
            self.set_state("录制中")
            return

        current_tracking_number = self.current_tracking_number
        current_start_time = self.start_time

        def finalize_recording():
            success = False
            error_message = ""
            destination = None
            end_time = time.strftime("%Y-%m-%d %H:%M:%S")
            duration = None
            if current_start_time:
                try:
                    t1 = time.mktime(time.strptime(current_start_time, "%Y-%m-%d %H:%M:%S"))
                    t2 = time.mktime(time.strptime(end_time, "%Y-%m-%d %H:%M:%S"))
                    duration = int(t2 - t1)
                except Exception:
                    pass

            try:
                video_settings = client.get_video_settings()
                resolution = f"{video_settings.base_width}x{video_settings.base_height}"
                fps = (
                    video_settings.fpsNumerator // video_settings.fpsDenominator
                    if video_settings.fpsDenominator else 0
                )
            except Exception:
                resolution, fps = "-", 0

            try:
                if not output_path:
                    raise RuntimeError("OBS 未返回输出文件路径")

                extension = os.path.splitext(output_path)[1] or ".mkv"
                date_dir = os.path.join(RECORD_PATH, end_time[:10])
                os.makedirs(date_dir, exist_ok=True)
                filename = f"{sanitize_tracking_filename(current_tracking_number)}{extension}"
                destination = unique_filename(date_dir, filename)
                ensure_copy_space(output_path, date_dir, DISK_COPY_RESERVE_GB)
                move_recording_file(output_path, destination)
                self.worker_log.emit(f"录制完成：{destination}")

                file_size = os.path.getsize(destination)
                db.insert_record(
                    current_tracking_number,
                    current_start_time,
                    end_time,
                    duration,
                    destination,
                    file_size,
                    resolution,
                    fps,
                )
                success = True
                self.worker_log.emit(f"JSONL 已记录单号：{current_tracking_number}")
            except Exception as exc:
                error_message = str(exc)
                failed_jobs.add(
                    self._failed_job_payload(
                        current_tracking_number,
                        output_path,
                        current_start_time,
                        end_time,
                        duration,
                        resolution,
                        fps,
                        exc,
                        destination if destination and os.path.isfile(destination) else None,
                    )
                )
                self.worker_log.emit(
                    f"录制文件未能归档，已加入恢复任务：{current_tracking_number} ({exc})"
                )

            finally:
                self.recording_finalized.emit(
                    start_new_tracking_number,
                    success,
                    error_message,
                )

        threading.Thread(target=finalize_recording, daemon=True).start()

    def _on_recording_finalized(self, requested_next_tracking_number, success, error_message):
        self.update_failed_jobs_status()
        if self.exit_after_finalize:
            self.exit_after_finalize = False
            self._reset_recording_state_and_continue(allow_continue=False)
            if success:
                self._shutdown_and_quit()
            else:
                QMessageBox.critical(
                    self,
                    "保存失败，未退出",
                    f"录像已停止，但文件未成功归档：{error_message}\n\n"
                    "程序将保持打开，请点击“重试保存失败任务”。",
                )
            return
        self._reset_recording_state_and_continue(requested_next_tracking_number)

    def poll_obs_status(self):
        """轮询兜底，不覆盖事件状态"""
        client = self.req_client
        if client is None:
            self.ensure_obs_connection()
            return
        try:
            scene_info = client.get_current_program_scene()
            self.current_scene = getattr(scene_info, "current_program_scene_name", "-")
            self.update_info()
        except Exception as e:
            self.mark_obs_disconnected(e)

    def _shutdown_and_quit(self):
        self.force_closing = True
        self.reconnect_timer.stop()
        self.window_arrange_timer.stop()
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.timer.stop()
        self.display_window.hide()
        self._disconnect_client(self.event_client)
        self._disconnect_client(self.req_client)
        self.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if self.force_closing:
            event.accept()
            return

        if self.orphan_recording_blocked:
            event.ignore()
            QMessageBox.warning(
                self,
                "OBS 仍在录制",
                "请先点击“处理 OBS 当前录像”，接管或停止保存后再退出。",
            )
            self.resolve_orphan_recording()
            return

        if self.current_tracking_number is not None or self.recording_phase != "idle":
            answer = QMessageBox.question(
                self,
                "录制尚未结束",
                "是否停止当前录像、保存完成后再退出？",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Yes,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            event.ignore()
            self.exit_after_finalize = True
            self.set_pending_tracking(None)
            self.stop_recording()
            return

        if failed_jobs.count():
            answer = QMessageBox.question(
                self,
                "仍有保存失败任务",
                "仍有录像文件尚未成功归档，确认退出吗？",
                QMessageBox.Yes | QMessageBox.Cancel,
                QMessageBox.Cancel,
            )
            if answer != QMessageBox.Yes:
                event.ignore()
                return
        event.accept()
        self._shutdown_and_quit()


# ---------------- 主程序 ----------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    if not acquire_single_instance():
        QMessageBox.warning(None, "程序已运行", "发货视频录制程序已经在运行。")
        sys.exit(1)
    app_window = RecorderApp()
    app_window.show()
    sys.exit(app.exec())
