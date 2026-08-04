# -*- coding: utf-8 -*-
import sys
import os
import csv
import logging
import time
import threading
import shutil
import subprocess
import uuid
from logging.handlers import RotatingFileHandler
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLineEdit,
    QLabel, QTextEdit, QPushButton, QCheckBox, QHBoxLayout,
    QMessageBox, QInputDialog, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QTabWidget, QSpinBox, QDoubleSpinBox
)
from PySide6.QtCore import QAbstractNativeEventFilter, QTimer, Qt, Signal, QEvent
from PySide6.QtGui import QFont, QKeySequence, QShortcut
import obsws_python as obs
from app_core import (
    AppSettingsStore,
    FailedJobStore,
    VideoDB,
    ensure_copy_space,
    move_recording_file,
    release_filename_reservation,
    reserve_unique_filename,
    sanitize_tracking_filename,
    video_settings_metadata,
    wait_for_recording_file_ready,
)

# ================= 配置 =================
BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"
settings_store = AppSettingsStore(SETTINGS_PATH, BASE_DIR)
APP_SETTINGS = settings_store.load()


def apply_module_settings(settings):
    """更新工作线程使用的只读配置快照。"""
    global OBS_WS_HOST, OBS_WS_PORT, OBS_WS_PASSWORD, OBS_EXECUTABLE_PATH
    global RECORD_PATH, MAX_RECORD_DURATION_MS, DISK_WARN_GB, DISK_CRITICAL_GB
    global DISK_COPY_RESERVE_GB, RECORD_START_CONFIRM_ATTEMPTS
    global RECORD_STOP_RETRY_ATTEMPTS, DUPLICATE_SCAN_WINDOW_SECONDS
    global AUTO_LAUNCH_OBS, AUTO_ARRANGE_WINDOWS, FORCE_ENGLISH_INPUT
    OBS_WS_HOST = settings["obs_ws_host"]
    OBS_WS_PORT = settings["obs_ws_port"]
    OBS_WS_PASSWORD = settings["obs_ws_password"]
    OBS_EXECUTABLE_PATH = settings["obs_executable"]
    RECORD_PATH = settings["record_path"]
    MAX_RECORD_DURATION_MS = settings["max_record_minutes"] * 60 * 1000
    DISK_WARN_GB = settings["disk_warn_gb"]
    DISK_CRITICAL_GB = settings["disk_critical_gb"]
    DISK_COPY_RESERVE_GB = settings["disk_copy_reserve_gb"]
    RECORD_START_CONFIRM_ATTEMPTS = max(
        1,
        round(settings["start_confirm_timeout_seconds"] * 1000 / 100),
    )
    RECORD_STOP_RETRY_ATTEMPTS = settings["stop_retry_count"]
    DUPLICATE_SCAN_WINDOW_SECONDS = settings["duplicate_scan_seconds"]
    AUTO_LAUNCH_OBS = settings["auto_launch_obs"]
    AUTO_ARRANGE_WINDOWS = settings["auto_arrange_windows"]
    FORCE_ENGLISH_INPUT = settings["force_english_input"]


apply_module_settings(APP_SETTINGS)
RECORD_START_CONFIRM_INTERVAL_MS = 100
RECORD_STOP_RETRY_INTERVAL_MS = 500
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
    if OBS_EXECUTABLE_PATH:
        configured = Path(OBS_EXECUTABLE_PATH)
        if configured.is_file():
            return configured
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


def accept_obs_crash_dialog(process_ids):
    """确认 OBS 异常退出提示，使无人值守重启能继续初始化 WebSocket。"""
    if sys.platform != "win32" or not process_ids:
        return None

    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    matches = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND,
        ctypes.POINTER(wintypes.DWORD),
    ]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.PostMessageW.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]

    @callback_type
    def enum_window(window_handle, _):
        if not user32.IsWindowVisible(window_handle):
            return True
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(window_handle, ctypes.byref(process_id))
        if process_id.value not in process_ids:
            return True
        title_length = user32.GetWindowTextLengthW(window_handle)
        title_buffer = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(window_handle, title_buffer, title_length + 1)
        title = title_buffer.value.casefold()
        if "obs" in title and ("崩溃" in title or "crash" in title):
            matches.append(window_handle)
            return False
        return True

    user32.EnumWindows(enum_window, 0)
    if not matches:
        return None
    window_handle = matches[0]
    user32.SetForegroundWindow(window_handle)
    # “以正常模式运行 / Run Normally”是该对话框的默认按钮。
    user32.PostMessageW(window_handle, 0x0100, 0x0D, 0)  # WM_KEYDOWN / VK_RETURN
    user32.PostMessageW(window_handle, 0x0101, 0x0D, 0)  # WM_KEYUP / VK_RETURN
    return int(window_handle)


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


class WindowsNativeEnterFilter(QAbstractNativeEventFilter):
    """在 IME 消费按键前捕获扫码枪回车。"""

    def __init__(self, callback):
        super().__init__()
        self.callback = callback

    def nativeEventFilter(self, event_type, message):
        if sys.platform != "win32":
            return False, 0
        try:
            import ctypes
            from ctypes import wintypes

            windows_message = wintypes.MSG.from_address(int(message))
            if (
                windows_message.message in (0x0100, 0x0104)
                and windows_message.wParam == 0x0D
            ):
                return bool(self.callback()), 0
        except Exception as exc:
            logger.warning("无法处理 Windows 原生回车事件：%s", exc)
        return False, 0


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


class SettingsDialog(QDialog):
    """面向操作员的配置面板，不暴露录像一致性与恢复安全规则。"""

    def __init__(self, values, defaults, parent=None):
        super().__init__(parent)
        self.defaults = defaults
        self.setWindowTitle("设置")
        self.resize(560, 440)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        general_tab = QWidget()
        general_form = QFormLayout(general_tab)
        record_path_row = QHBoxLayout()
        self.record_path = QLineEdit()
        record_path_row.addWidget(self.record_path)
        browse_record = QPushButton("浏览…")
        browse_record.clicked.connect(self._browse_record_path)
        record_path_row.addWidget(browse_record)
        general_form.addRow("录像归档目录", record_path_row)
        self.max_record_minutes = QSpinBox()
        self.max_record_minutes.setRange(1, 1440)
        self.max_record_minutes.setSuffix(" 分钟")
        general_form.addRow("单段最长录像", self.max_record_minutes)
        self.show_obs_info = QCheckBox("启动时显示 OBS 信息窗口")
        general_form.addRow("信息窗口", self.show_obs_info)
        self.auto_launch_obs = QCheckBox("程序启动时自动打开 OBS")
        general_form.addRow("自动启动", self.auto_launch_obs)
        self.auto_arrange_windows = QCheckBox("控制程序与 OBS 自动左右排列")
        general_form.addRow("窗口排列", self.auto_arrange_windows)
        self.force_english_input = QCheckBox("扫码框聚焦时锁定英文键盘布局")
        general_form.addRow("扫码输入法", self.force_english_input)
        self.tabs.addTab(general_tab, "常规")

        obs_tab = QWidget()
        obs_form = QFormLayout(obs_tab)
        executable_row = QHBoxLayout()
        self.obs_executable = QLineEdit()
        self.obs_executable.setPlaceholderText("留空则自动查找 OBS")
        executable_row.addWidget(self.obs_executable)
        browse_obs = QPushButton("浏览…")
        browse_obs.clicked.connect(self._browse_obs_executable)
        executable_row.addWidget(browse_obs)
        obs_form.addRow("OBS 主程序", executable_row)
        self.obs_ws_host = QLineEdit()
        obs_form.addRow("WebSocket 地址", self.obs_ws_host)
        self.obs_ws_port = QSpinBox()
        self.obs_ws_port.setRange(1, 65535)
        obs_form.addRow("WebSocket 端口", self.obs_ws_port)
        self.obs_ws_password = QLineEdit()
        self.obs_ws_password.setEchoMode(QLineEdit.Password)
        obs_form.addRow("WebSocket 密码", self.obs_ws_password)
        note = QLabel("OBS 连接参数以明文保存在项目本机的 settings.json 中。")
        note.setWordWrap(True)
        obs_form.addRow("", note)
        self.tabs.addTab(obs_tab, "OBS")

        storage_tab = QWidget()
        storage_form = QFormLayout(storage_tab)
        self.disk_warn_gb = QSpinBox()
        self.disk_warn_gb.setRange(1, 100000)
        self.disk_warn_gb.setSuffix(" GB")
        storage_form.addRow("剩余空间警告", self.disk_warn_gb)
        self.disk_critical_gb = QSpinBox()
        self.disk_critical_gb.setRange(1, 100000)
        self.disk_critical_gb.setSuffix(" GB")
        storage_form.addRow("禁止新录像阈值", self.disk_critical_gb)
        self.disk_copy_reserve_gb = QSpinBox()
        self.disk_copy_reserve_gb.setRange(0, 10000)
        self.disk_copy_reserve_gb.setSuffix(" GB")
        storage_form.addRow("跨盘复制额外预留", self.disk_copy_reserve_gb)
        self.duplicate_scan_seconds = QDoubleSpinBox()
        self.duplicate_scan_seconds.setRange(0.0, 10.0)
        self.duplicate_scan_seconds.setDecimals(1)
        self.duplicate_scan_seconds.setSingleStep(0.1)
        self.duplicate_scan_seconds.setSuffix(" 秒")
        storage_form.addRow("同单号防重复窗口", self.duplicate_scan_seconds)
        self.tabs.addTab(storage_tab, "扫码与存储")

        advanced_tab = QWidget()
        advanced_form = QFormLayout(advanced_tab)
        self.start_confirm_timeout_seconds = QDoubleSpinBox()
        self.start_confirm_timeout_seconds.setRange(1.0, 60.0)
        self.start_confirm_timeout_seconds.setDecimals(1)
        self.start_confirm_timeout_seconds.setSuffix(" 秒")
        advanced_form.addRow("开始录像确认超时", self.start_confirm_timeout_seconds)
        self.stop_retry_count = QSpinBox()
        self.stop_retry_count.setRange(1, 20)
        advanced_form.addRow("停止录像重试次数", self.stop_retry_count)
        warning = QLabel(
            "高级参数通常无需修改。恢复事务、文件完整性检查、最新扫码覆盖策略"
            "等安全规则为固定行为。"
        )
        warning.setWordWrap(True)
        advanced_form.addRow("", warning)
        self.tabs.addTab(advanced_tab, "高级")

        buttons = QDialogButtonBox(
            QDialogButtonBox.Save
            | QDialogButtonBox.Cancel
            | QDialogButtonBox.RestoreDefaults
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.RestoreDefaults).clicked.connect(
            lambda: self.set_values(self.defaults)
        )
        root.addWidget(buttons)
        self.set_values(values)

    def _browse_record_path(self):
        path = QFileDialog.getExistingDirectory(
            self,
            "选择录像归档目录",
            self.record_path.text(),
        )
        if path:
            self.record_path.setText(path)

    def _browse_obs_executable(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "选择 OBS 主程序",
            self.obs_executable.text(),
            "OBS (obs64.exe);;可执行文件 (*.exe);;所有文件 (*)",
        )
        if path:
            self.obs_executable.setText(path)

    def set_values(self, values):
        for key in (
            "record_path", "obs_executable", "obs_ws_host", "obs_ws_password"
        ):
            getattr(self, key).setText(str(values[key]))
        for key in (
            "obs_ws_port", "max_record_minutes", "disk_warn_gb",
            "disk_critical_gb", "disk_copy_reserve_gb", "stop_retry_count",
        ):
            getattr(self, key).setValue(int(values[key]))
        for key in ("duplicate_scan_seconds", "start_confirm_timeout_seconds"):
            getattr(self, key).setValue(float(values[key]))
        for key in (
            "show_obs_info", "auto_launch_obs", "auto_arrange_windows",
            "force_english_input",
        ):
            getattr(self, key).setChecked(bool(values[key]))

    def values(self):
        return {
            "schema_version": AppSettingsStore.SCHEMA_VERSION,
            "record_path": self.record_path.text(),
            "obs_executable": self.obs_executable.text(),
            "obs_ws_host": self.obs_ws_host.text(),
            "obs_ws_port": self.obs_ws_port.value(),
            "obs_ws_password": self.obs_ws_password.text(),
            "max_record_minutes": self.max_record_minutes.value(),
            "disk_warn_gb": self.disk_warn_gb.value(),
            "disk_critical_gb": self.disk_critical_gb.value(),
            "disk_copy_reserve_gb": self.disk_copy_reserve_gb.value(),
            "duplicate_scan_seconds": self.duplicate_scan_seconds.value(),
            "start_confirm_timeout_seconds": self.start_confirm_timeout_seconds.value(),
            "stop_retry_count": self.stop_retry_count.value(),
            "show_obs_info": self.show_obs_info.isChecked(),
            "auto_launch_obs": self.auto_launch_obs.isChecked(),
            "auto_arrange_windows": self.auto_arrange_windows.isChecked(),
            "force_english_input": self.force_english_input.isChecked(),
        }


class RecorderApp(QWidget):
    obs_connection_ready = Signal(object, object, str)
    obs_connection_failed = Signal(str)
    obs_scene_changed = Signal(str)
    obs_record_state_changed = Signal(bool, str, str)
    worker_log = Signal(str)
    recording_finalized = Signal(object, bool, str)
    failed_jobs_retry_finished = Signal(int, int)

    def __init__(self):
        super().__init__()
        self.settings = dict(APP_SETTINGS)
        self.setWindowTitle("发货视频录制")
        self.resize(600, 550)

        layout = QVBoxLayout()

        # ===== 输入区 =====
        self.label = QLabel("条码枪扫描快递单号后回车即可开始录制：")
        layout.addWidget(self.label)

        self.input = QLineEdit()
        self.input_ime_context = None
        self.input.setAttribute(Qt.WA_InputMethodEnabled, False)
        self.input.setInputMethodHints(Qt.ImhNoPredictiveText)
        self.input.returnPressed.connect(self.handle_input)
        self.input.textEdited.connect(self._on_scan_text_edited)
        layout.addWidget(self.input)

        self.stop_button = QPushButton("停止录制")
        self.stop_button.clicked.connect(self.stop_recording)
        self.stop_button.clicked.connect(
            lambda _checked=False: QTimer.singleShot(0, self.focus_scan_input)
        )
        layout.addWidget(self.stop_button)

        self.display_checkbox = QCheckBox("显示 OBS 信息窗口")
        self.display_checkbox.setChecked(self.settings["show_obs_info"])
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
        recovery_buttons.addStretch(1)
        self.settings_button = QPushButton("设置")
        self.settings_button.clicked.connect(self.open_settings)
        recovery_buttons.addWidget(self.settings_button)
        self.settings_shortcut = QShortcut(QKeySequence("Ctrl+,"), self)
        self.settings_shortcut.activated.connect(self.open_settings)
        layout.addLayout(recovery_buttons)

        # ===== 日志区 =====
        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)

        self.setLayout(layout)
        self.disk_free_gb = None
        self.disk_block_reason = None
        self.obs_record_directory = None

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

        self.keyboard_layout_timer = QTimer(self)
        self.keyboard_layout_timer.setInterval(250)
        self.keyboard_layout_timer.timeout.connect(
            self._force_windows_english_keyboard_layout
        )
        if FORCE_ENGLISH_INPUT:
            self.keyboard_layout_timer.start()

        # 状态变量
        self.current_tracking_number = None
        self.current_recording_id = None
        self.start_time = None
        self.recording_started_monotonic = None
        self.recording_deadline_monotonic = None
        self.recording_finalization_started = False
        self.current_scene = "-"
        self.recording_phase = "idle"
        self.record_start_check_attempts = 0
        self.record_stop_retry_attempts = 0
        self.pending_tracking_number = None
        self.stop_requested = False
        self.last_scan_number = None
        self.last_scan_monotonic = 0.0
        self.scanner_buffer = ""
        self.exit_after_finalize = False
        self.force_closing = False
        self.orphan_recording_blocked = False
        self.retry_in_progress = False
        self.last_record_output_path = None
        self.obs_request_lock = threading.RLock()
        self.record_state_lock = threading.Lock()
        self.req_client = None
        self.event_client = None
        self.connection_in_progress = False
        self.connection_error_logged = False
        self.obs_process = None
        self.windows_arranged = False
        self.window_arrange_attempts = 0
        self.last_obs_crash_dialog_handle = None

        self.window_arrange_timer = QTimer(self)
        self.window_arrange_timer.setInterval(500)
        self.window_arrange_timer.timeout.connect(self._try_arrange_windows)

        self.scanner_buffer_timer = QTimer(self)
        self.scanner_buffer_timer.setSingleShot(True)
        self.scanner_buffer_timer.setInterval(300)
        self.scanner_buffer_timer.timeout.connect(self._clear_scanner_buffer)

        self.scan_submit_fallback_timer = QTimer(self)
        self.scan_submit_fallback_timer.setSingleShot(True)
        self.scan_submit_fallback_timer.setInterval(600)
        self.scan_submit_fallback_timer.timeout.connect(
            self._submit_fast_scan_fallback
        )
        self.scan_edit_started_monotonic = None
        self.scan_edit_last_monotonic = None

        self.display_window = OBSDisplayWindow()
        self.display_checkbox.toggled.connect(self._set_display_window_visible)
        self.display_window.hide_requested.connect(
            lambda: self.display_checkbox.setChecked(False)
        )
        self.display_window.setVisible(self.display_checkbox.isChecked())

        self.obs_connection_ready.connect(self._on_obs_connection_ready)
        self.obs_connection_failed.connect(self._on_obs_connection_failed)
        self.obs_scene_changed.connect(self._on_obs_scene_changed)
        self.obs_record_state_changed.connect(self._on_obs_record_state_changed)
        self.worker_log.connect(self.log)
        self.recording_finalized.connect(self._on_recording_finalized)
        self.failed_jobs_retry_finished.connect(self._on_failed_jobs_retry_finished)
        QApplication.instance().installEventFilter(self)
        self.native_enter_filter = WindowsNativeEnterFilter(
            self._handle_windows_native_enter
        )
        QApplication.instance().installNativeEventFilter(self.native_enter_filter)
        self.update_failed_jobs_status()
        for warning in settings_store.consume_warnings():
            self.log(f"⚠️ {warning}")
        QTimer.singleShot(0, self.ensure_obs_connection)
        QTimer.singleShot(0, self.launch_obs_and_prepare_windows)
        QTimer.singleShot(100, self.focus_scan_input)
        QTimer.singleShot(150, self._disable_windows_ime)

    # ---------------- UI辅助 ----------------
    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.log_box.append(line)
        print(line)
        logger.info(msg)
        self.update_info()

    def open_settings(self):
        dialog = SettingsDialog(
            self.settings,
            settings_store.defaults(),
            self,
        )
        while dialog.exec() == QDialog.Accepted:
            proposed = dialog.values()
            connection_keys = {
                "obs_ws_host", "obs_ws_port", "obs_ws_password"
            }
            idle_required_keys = connection_keys | {"record_path"}
            changed_idle_keys = {
                key for key in idle_required_keys
                if proposed.get(key) != self.settings.get(key)
            }
            if changed_idle_keys and (
                self.current_tracking_number is not None
                or self.recording_phase != "idle"
                or self.retry_in_progress
            ):
                QMessageBox.warning(
                    self,
                    "当前无法应用",
                    "修改录像目录或 OBS 连接参数前，请先停止录像并等待保存完成。",
                )
                continue
            if changed_idle_keys & connection_keys and self.connection_in_progress:
                QMessageBox.warning(
                    self,
                    "OBS 正在连接",
                    "请等待本次 OBS 连接完成后再修改连接参数。",
                )
                continue
            try:
                validated = settings_store.validate(proposed)
                Path(validated["record_path"]).mkdir(parents=True, exist_ok=True)
                saved = settings_store.save(validated)
            except (OSError, ValueError) as exc:
                QMessageBox.critical(self, "设置无法保存", str(exc))
                continue
            previous = self.settings
            self.settings = saved
            APP_SETTINGS.clear()
            APP_SETTINGS.update(saved)
            apply_module_settings(saved)
            self._apply_runtime_settings(previous)
            self.log("设置已保存并应用")
            break

    def _apply_runtime_settings(self, previous):
        self.display_checkbox.setChecked(self.settings["show_obs_info"])
        if FORCE_ENGLISH_INPUT:
            if not self.keyboard_layout_timer.isActive():
                self.keyboard_layout_timer.start()
            self._disable_windows_ime()
        else:
            self.keyboard_layout_timer.stop()

        connection_changed = any(
            previous.get(key) != self.settings.get(key)
            for key in ("obs_ws_host", "obs_ws_port", "obs_ws_password")
        )
        if connection_changed:
            with self.obs_request_lock:
                self._disconnect_client(self.event_client)
                self._disconnect_client(self.req_client)
                self.event_client = None
                self.req_client = None
            self.connection_in_progress = False
            QTimer.singleShot(0, self.ensure_obs_connection)

        if AUTO_ARRANGE_WINDOWS and not self.windows_arranged:
            self.start_window_arrangement()
        elif not AUTO_ARRANGE_WINDOWS:
            self.window_arrange_timer.stop()
        if AUTO_LAUNCH_OBS and sys.platform == "win32" and not get_obs_process_ids():
            QTimer.singleShot(0, self.launch_obs_and_prepare_windows)
        self.update_disk_space()
        self.focus_scan_input()

    def _clear_scanner_buffer(self):
        self.scanner_buffer = ""

    def _on_scan_text_edited(self, text):
        now = time.monotonic()
        if not text:
            self.scan_submit_fallback_timer.stop()
            self.scan_edit_started_monotonic = None
            self.scan_edit_last_monotonic = None
            return
        if (
            self.scan_edit_last_monotonic is None
            or now - self.scan_edit_last_monotonic > 0.4
        ):
            self.scan_edit_started_monotonic = now
        self.scan_edit_last_monotonic = now
        self.scan_submit_fallback_timer.start()

    def _submit_fast_scan_fallback(self):
        text = self.input.text().strip()
        if (
            len(text) < 4
            or self.scan_edit_started_monotonic is None
            or self.scan_edit_last_monotonic is None
            or self.scan_edit_last_monotonic - self.scan_edit_started_monotonic > 1.5
        ):
            return
        self.log("检测到扫码枪快速输入但回车未提交，已自动处理")
        self.handle_input()

    def _handle_windows_native_enter(self):
        if (
            QApplication.activeModalWidget() is None
            and self.input.hasFocus()
            and bool(self.input.text().strip())
        ):
            QTimer.singleShot(0, self.handle_input)
            return True
        return False

    def _disable_windows_ime(self):
        """扫码输入框始终使用直接键盘输入，避免中文输入法接管回车。"""
        if not FORCE_ENGLISH_INPUT:
            self.input.setAttribute(Qt.WA_InputMethodEnabled, True)
            if sys.platform == "win32" and self.input_ime_context:
                try:
                    import ctypes
                    from ctypes import wintypes

                    imm32 = ctypes.windll.imm32
                    imm32.ImmAssociateContext.argtypes = [
                        wintypes.HWND,
                        wintypes.HANDLE,
                    ]
                    imm32.ImmAssociateContext(
                        int(self.input.winId()),
                        self.input_ime_context,
                    )
                    self.input_ime_context = None
                except Exception as exc:
                    logger.warning("无法恢复扫码输入框 IME：%s", exc)
            return
        self.input.setAttribute(Qt.WA_InputMethodEnabled, False)
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes

            imm32 = ctypes.windll.imm32
            imm32.ImmAssociateContext.argtypes = [
                wintypes.HWND,
                wintypes.HANDLE,
            ]
            imm32.ImmAssociateContext.restype = wintypes.HANDLE
            previous_context = imm32.ImmAssociateContext(
                int(self.input.winId()),
                None,
            )
            if self.input_ime_context is None and previous_context:
                self.input_ime_context = previous_context
            self._force_windows_english_keyboard_layout()
        except Exception as exc:
            logger.warning("无法禁用扫码输入框 IME：%s", exc)

    def _force_windows_english_keyboard_layout(self):
        """扫码框聚焦时锁定英文键盘布局，防止数字成为中文候选键。"""
        if (
            not FORCE_ENGLISH_INPUT
            or sys.platform != "win32"
            or not self.input.hasFocus()
        ):
            return
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            user32.GetKeyboardLayout.argtypes = [wintypes.DWORD]
            user32.GetKeyboardLayout.restype = wintypes.HANDLE
            current_layout = user32.GetKeyboardLayout(0)
            current_layout_value = getattr(
                current_layout,
                "value",
                current_layout,
            ) or 0
            if current_layout_value & 0xFFFF == 0x0409:
                return
            user32.LoadKeyboardLayoutW.argtypes = [wintypes.LPCWSTR, wintypes.UINT]
            user32.LoadKeyboardLayoutW.restype = wintypes.HANDLE
            user32.ActivateKeyboardLayout.argtypes = [wintypes.HANDLE, wintypes.UINT]
            user32.ActivateKeyboardLayout.restype = wintypes.HANDLE
            layout = user32.LoadKeyboardLayoutW("00000409", 0x00000001)
            if layout:
                user32.ActivateKeyboardLayout(layout, 0x00000100)
        except Exception as exc:
            logger.warning("无法锁定扫码输入框英文键盘布局：%s", exc)

    def eventFilter(self, watched, event):
        if (
            event.type() != QEvent.KeyPress
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
            if watched is self.input and self.input.text().strip():
                self.handle_input()
                return True
            return super().eventFilter(watched, event)

        if watched is self.input and key == Qt.Key_A and (
            event.modifiers() & Qt.ControlModifier
        ):
            self._clear_scanner_buffer()
            self.input.clear()
            return True
        if watched is self.input and key == Qt.Key_Backspace:
            self.scanner_buffer = self.scanner_buffer[:-1]
            self.input.setText(self.scanner_buffer)
            return True

        blocked_modifiers = Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier
        text = event.text()
        if (not text or not text.isprintable()) and 0x20 <= key <= 0x7E:
            # 中文 IME 会把数字键当成候选选择，event.text() 可能为空；
            # event.key() 仍保留扫码枪发送的原始 ASCII 键值。
            text = chr(key)
        if text and text.isprintable() and not (event.modifiers() & blocked_modifiers):
            self.scanner_buffer += text
            self.scanner_buffer_timer.start()
            if watched is self.input:
                self.input.setText(self.scanner_buffer)
                now = time.monotonic()
                if (
                    self.scan_edit_last_monotonic is None
                    or now - self.scan_edit_last_monotonic > 0.4
                ):
                    self.scan_edit_started_monotonic = now
                self.scan_edit_last_monotonic = now
                self.scan_submit_fallback_timer.start()
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
        try:
            count = failed_jobs.count()
        except Exception as exc:
            self.failed_jobs_label.setText("待恢复文件：无法读取")
            self.failed_jobs_label.setStyleSheet(
                "font-size: 14px; color: red; font-weight: bold;"
            )
            self.retry_failed_button.setEnabled(False)
            logger.error("无法读取恢复任务：%s", exc)
            return
        self.failed_jobs_label.setText(f"待恢复文件：{count}")
        color = "red" if count else "gray"
        self.failed_jobs_label.setStyleSheet(
            f"font-size: 14px; color: {color};"
        )
        self.retry_failed_button.setEnabled(count > 0)
        for warning in failed_jobs.consume_warnings():
            self.log(f"⚠️ {warning}")

    def _call_obs_client(self, client, method_name, *args, **kwargs):
        if client is None:
            raise ConnectionError("OBS WebSocket 尚未连接")
        with self.obs_request_lock:
            return getattr(client, method_name)(*args, **kwargs)

    def _call_obs(self, method_name, *args, **kwargs):
        return self._call_obs_client(self.req_client, method_name, *args, **kwargs)

    def _latest_stopped_output_path(self):
        with self.record_state_lock:
            output_path = self.last_record_output_path
        if output_path and os.path.isfile(output_path):
            return output_path
        directory = self.obs_record_directory
        if not directory or not os.path.isdir(directory):
            return None
        started_at = None
        if self.start_time:
            try:
                started_at = time.mktime(
                    time.strptime(self.start_time, "%Y-%m-%d %H:%M:%S")
                ) - 10
            except (ValueError, OverflowError):
                pass
        candidates = []
        try:
            for entry in os.scandir(directory):
                if entry.is_file() and not entry.name.endswith((".part", ".tmp")):
                    modified = entry.stat().st_mtime
                    if started_at is None or modified >= started_at:
                        candidates.append((modified, entry.path))
        except OSError:
            return None
        return max(candidates, default=(0, None))[1]

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
        recording_id=None,
        planned_destination=None,
    ):
        return {
            "id": recording_id or uuid.uuid4().hex,
            "tracking_number": tracking_number,
            "source_path": source_path,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "resolution": resolution,
            "fps": fps,
            "last_error": str(error),
            "archived_path": archived_path,
            "planned_destination": planned_destination,
            "state": "pending",
        }

    def retry_failed_jobs(self):
        if self.retry_in_progress:
            return
        self.retry_in_progress = True
        self.retry_failed_button.setEnabled(False)
        self.log("正在重试保存失败任务…")
        threading.Thread(target=self._retry_failed_jobs_worker, daemon=True).start()

    def _retry_failed_jobs_worker(self):
        succeeded = 0
        remaining = 0
        try:
            jobs_to_retry = failed_jobs.list()
            for job in jobs_to_retry:
                source_path = job.get("source_path")
                reservation = None
                try:
                    archived_path = job.get("archived_path")
                    if archived_path and os.path.isfile(archived_path):
                        destination = archived_path
                    elif (
                        job.get("planned_destination")
                        and os.path.isfile(job["planned_destination"])
                        and (
                            job.get("state") == "recording_metadata"
                            or not source_path
                            or not os.path.isfile(source_path)
                        )
                    ):
                        # state=recording_metadata 表示本程序已完成移动；若源文件也
                        # 已消失，则兼容进程在移动成功、更新任务状态前崩溃的窗口。
                        destination = job["planned_destination"]
                    else:
                        if not source_path or not os.path.isfile(source_path):
                            raise FileNotFoundError("OBS 输出文件不存在，需人工检查 OBS 输出目录")
                        wait_for_recording_file_ready(source_path)
                        date_text = (job.get("end_time") or time.strftime("%Y-%m-%d"))[:10]
                        date_dir = os.path.join(RECORD_PATH, date_text)
                        os.makedirs(date_dir, exist_ok=True)
                        extension = os.path.splitext(source_path)[1] or ".mkv"
                        filename = f"{sanitize_tracking_filename(job.get('tracking_number'))}{extension}"
                        planned_destination = job.get("planned_destination")
                        planned_reservation = (
                            f"{planned_destination}.reserve"
                            if planned_destination
                            else None
                        )
                        if (
                            planned_destination
                            and not os.path.exists(planned_destination)
                            and planned_reservation
                            and os.path.isfile(planned_reservation)
                        ):
                            destination = planned_destination
                            reservation = planned_reservation
                        else:
                            destination, reservation = reserve_unique_filename(
                                date_dir,
                                filename,
                            )
                        job["planned_destination"] = destination
                        job["state"] = "moving"
                        failed_jobs.upsert(job)
                        ensure_copy_space(source_path, date_dir, DISK_COPY_RESERVE_GB)
                        move_recording_file(source_path, destination)
                        job["archived_path"] = destination
                        job["source_path"] = destination
                        job["state"] = "recording_metadata"
                        failed_jobs.upsert(job)
                    file_size = wait_for_recording_file_ready(
                        destination,
                        stable_checks=0,
                    )
                    db.insert_record(
                        job.get("tracking_number"),
                        job.get("start_time"),
                        job.get("end_time"),
                        job.get("duration"),
                        destination,
                        file_size,
                        job.get("resolution", "-"),
                        job.get("fps", 0),
                        recording_id=job.get("id"),
                    )
                    failed_jobs.remove(job.get("id"))
                    succeeded += 1
                    self.worker_log.emit(
                        f"失败任务已恢复：{job.get('tracking_number')} → {destination}"
                    )
                except Exception as exc:
                    job["last_error"] = str(exc)
                    job["last_retry_at"] = datetime.now().isoformat(timespec="seconds")
                    try:
                        failed_jobs.upsert(job)
                    except Exception as store_error:
                        self.worker_log.emit(
                            f"恢复任务状态无法持久化：{job.get('tracking_number')} ({store_error})"
                        )
                    self.worker_log.emit(
                        f"失败任务仍无法保存：{job.get('tracking_number')} ({exc})"
                    )
                finally:
                    try:
                        release_filename_reservation(reservation)
                    except OSError as exc:
                        self.worker_log.emit(f"无法清理文件名保留标记：{exc}")
            remaining = failed_jobs.count()
        except Exception as exc:
            self.worker_log.emit(f"无法读取或更新恢复任务：{exc}")
            try:
                remaining = failed_jobs.count()
            except Exception:
                remaining = -1
        finally:
            self.failed_jobs_retry_finished.emit(succeeded, remaining)

    def _on_failed_jobs_retry_finished(self, succeeded, remaining):
        self.retry_in_progress = False
        self.update_failed_jobs_status()
        self.log(f"失败任务重试完成：成功 {succeeded}，剩余 {remaining}")
        if remaining < 0:
            QMessageBox.critical(
                self,
                "恢复任务不可用",
                "无法读取恢复任务文件，请检查 records 目录权限和磁盘状态。",
            )
        elif remaining:
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
            status = self._call_obs_client(client, "get_record_status")
            if not bool(getattr(status, "output_active", False)):
                self._clear_orphan_recording_block("OBS 孤立录像已经结束，扫码功能已恢复")
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
            self.current_recording_id = uuid.uuid4().hex
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.recording_started_monotonic = time.monotonic()
            self.recording_deadline_monotonic = (
                self.recording_started_monotonic + MAX_RECORD_DURATION_MS / 1000
            )
            self.recording_finalization_started = False
            self.recording_phase = "recording"
            self.orphan_recording_blocked = False
            self.resolve_orphan_button.hide()
            self.timer.start(MAX_RECORD_DURATION_MS)
            self.set_state("录制中")
            self.log(f"已接管 OBS 当前录像：{tracking_number}（开始时间按接管时刻计算）")
            return

        if clicked is stop_button:
            self.current_tracking_number = f"ORPHAN_{datetime.now():%Y%m%d_%H%M%S}"
            self.current_recording_id = uuid.uuid4().hex
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.recording_started_monotonic = time.monotonic()
            self.recording_deadline_monotonic = (
                self.recording_started_monotonic + MAX_RECORD_DURATION_MS / 1000
            )
            self.recording_finalization_started = False
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
        self._disable_windows_ime()
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

        if AUTO_LAUNCH_OBS and not get_obs_process_ids():
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
        if AUTO_ARRANGE_WINDOWS:
            self.start_window_arrangement()
        else:
            self.focus_scan_input()

    def start_window_arrangement(self):
        if not AUTO_ARRANGE_WINDOWS or sys.platform != "win32" or self.windows_arranged:
            return
        self.window_arrange_attempts = 0
        if not self.window_arrange_timer.isActive():
            self.window_arrange_timer.start()
        self._try_arrange_windows()

    def _try_arrange_windows(self):
        if not AUTO_ARRANGE_WINDOWS or sys.platform != "win32":
            self.window_arrange_timer.stop()
            return

        self.window_arrange_attempts += 1
        process_ids = get_obs_process_ids()
        crash_dialog_handle = accept_obs_crash_dialog(process_ids)
        if crash_dialog_handle:
            if crash_dialog_handle != self.last_obs_crash_dialog_handle:
                self.last_obs_crash_dialog_handle = crash_dialog_handle
                self.log("已确认 OBS 异常退出提示，将以正常模式继续启动")
            QTimer.singleShot(1000, self.focus_scan_input)
            return
        obs_window = find_largest_window_for_processes(process_ids)
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
            # OBS 崩溃重启时 WebSocket 端口可能早于场景/输出模块就绪。
            # 只有关键请求可用后才将连接交给界面，避免“刚连上又断开”。
            request_client.get_record_directory()
            request_client.get_current_program_scene()
        except Exception as exc:
            if "request_client" in locals():
                self._disconnect_client(request_client)
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
            event_client_instance.callback.register(self.on_record_state_changed)
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

    def on_record_state_changed(self, data):
        if isinstance(data, dict):
            state = str(data.get("outputState") or data.get("output_state") or "")
            output_path = str(data.get("outputPath") or data.get("output_path") or "")
        else:
            state = str(
                getattr(data, "output_state", None)
                or getattr(data, "outputState", "")
            )
            output_path = str(
                getattr(data, "output_path", None)
                or getattr(data, "outputPath", "")
            )
        state_upper = state.upper()
        if "STARTED" in state_upper:
            active = True
        elif any(word in state_upper for word in ("STOPPED", "ERROR")):
            active = False
        else:
            return
        if not active and output_path:
            with self.record_state_lock:
                self.last_record_output_path = output_path
        self.obs_record_state_changed.emit(active, output_path, state)

    def _on_obs_record_state_changed(self, active, output_path, state):
        if active:
            return
        if (
            self.current_tracking_number is not None
            and self.recording_phase in ("recording", "stopping", "waiting_reconnect")
        ):
            self.log(f"OBS 报告录像已结束：{state or 'STOPPED'}")
            self._begin_recording_finalization(
                output_path or self._latest_stopped_output_path(),
                reason="OBS 状态事件",
            )
        elif self.current_tracking_number is None and self.orphan_recording_blocked:
            self._clear_orphan_recording_block("OBS 状态事件确认孤立录像已结束")

    def _clear_orphan_recording_block(self, reason):
        self.orphan_recording_blocked = False
        self.resolve_orphan_button.hide()
        self.set_state("已停止")
        self.log(reason)
        if self.pending_tracking_number and self.recording_phase == "idle":
            tracking_number = self.pending_tracking_number
            self.set_pending_tracking(None)
            QTimer.singleShot(0, lambda: self.start_recording(tracking_number))

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
        try:
            directory_response = self._call_obs_client(
                request_client,
                "get_record_directory",
            )
            self.obs_record_directory = getattr(
                directory_response,
                "record_directory",
                None,
            )
        except Exception as exc:
            self.log(f"无法读取 OBS 输出目录：{exc}")
        self.update_disk_space()
        self.poll_obs_status()
        if self.current_tracking_number is not None:
            QTimer.singleShot(0, self._resume_recording_after_reconnect)
            return
        try:
            status = self._call_obs_client(request_client, "get_record_status")
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
        if sys.platform == "win32" and not get_obs_process_ids():
            QTimer.singleShot(0, self.launch_obs_and_prepare_windows)

    def _on_obs_scene_changed(self, scene):
        self.current_scene = scene
        self.log(f"[事件] 场景切换到：{scene}")

    def _resume_recording_after_reconnect(self):
        client = self.req_client
        if client is None or self.current_tracking_number is None:
            return
        try:
            status = self._call_obs_client(client, "get_record_status")
            is_recording = bool(getattr(status, "output_active", False))
        except Exception as exc:
            self.mark_obs_disconnected(exc)
            return

        if is_recording:
            self.recording_phase = "recording"
            if self.start_time is None:
                self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            if self.recording_started_monotonic is None:
                self.recording_started_monotonic = time.monotonic()
            if self.recording_deadline_monotonic is None:
                self.recording_deadline_monotonic = (
                    self.recording_started_monotonic + MAX_RECORD_DURATION_MS / 1000
                )
            remaining_ms = max(
                1,
                int((self.recording_deadline_monotonic - time.monotonic()) * 1000),
            )
            self.timer.start(remaining_ms)
            self.set_state("录制中")
            if self.stop_requested or self.pending_tracking_number:
                self.stop_requested = False
                QTimer.singleShot(0, self.stop_recording)
            return

        interrupted_tracking_number = self.current_tracking_number
        self.log(f"OBS 重连后确认录像已结束，尝试恢复单号：{interrupted_tracking_number}")
        self._begin_recording_finalization(
            self._latest_stopped_output_path(),
            reason="OBS 重连状态对账",
        )

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
        with self.obs_request_lock:
            self._disconnect_client(self.event_client)
            self._disconnect_client(self.req_client)
            self.event_client = None
            self.req_client = None
        self.connection_in_progress = False
        self.current_scene = "-"
        if (
            self.current_tracking_number is not None
            and self.recording_phase != "saving"
        ):
            self.recording_phase = "waiting_reconnect"
        self.windows_arranged = False
        self.start_window_arrangement()
        self.obs_connection_label.setText("OBS 连接：连接已断开，等待重连")
        self.obs_connection_label.setStyleSheet("font-size: 14px; color: #b7791f;")
        self.log(f"OBS 连接已断开，程序将自动重连：{reason}")

    def update_disk_space(self):
        try:
            paths = [("归档盘", RECORD_PATH)]
            if self.obs_record_directory:
                if os.name == "nt":
                    archive_drive = os.path.splitdrive(
                        os.path.abspath(RECORD_PATH)
                    )[0].casefold()
                    obs_drive = os.path.splitdrive(
                        os.path.abspath(self.obs_record_directory)
                    )[0].casefold()
                    separate_filesystem = obs_drive != archive_drive
                else:
                    separate_filesystem = (
                        os.stat(RECORD_PATH).st_dev
                        != os.stat(self.obs_record_directory).st_dev
                    )
                if separate_filesystem:
                    paths.append(("OBS 输出盘", self.obs_record_directory))
            free_values = []
            details = []
            for label, path in paths:
                usage = shutil.disk_usage(path)
                free_value = usage.free / (1024**3)
                free_values.append(free_value)
                details.append(f"{label} {free_value:.1f} GB")
            free_gb = min(free_values)
            self.disk_free_gb = free_gb
            self.disk_block_reason = None
            if free_gb < DISK_CRITICAL_GB:
                self.disk_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")
                suffix = "（空间不足，禁止新录制）"
                self.disk_block_reason = "；".join(details)
            elif free_gb < DISK_WARN_GB:
                self.disk_label.setStyleSheet("font-size: 14px; color: #b7791f; font-weight: bold;")
                suffix = "（请尽快清理）"
            else:
                self.disk_label.setStyleSheet("font-size: 14px; color: green;")
                suffix = ""
            self.disk_label.setText(f"磁盘剩余：{'；'.join(details)} {suffix}")
        except Exception as e:
            self.disk_free_gb = None
            self.disk_block_reason = f"无法读取磁盘空间：{e}"
            self.disk_label.setText(f"磁盘剩余：未知，禁止新录制 ({e})")
            self.disk_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")

    # ---------------- 主逻辑 ----------------
    def handle_input(self):
        tracking_number = self.input.text().strip()
        self.input.setText("")
        self.scan_submit_fallback_timer.stop()
        self.scan_edit_started_monotonic = None
        self.scan_edit_last_monotonic = None
        if not tracking_number:
            return

        # 特殊指令 STOPSTOP
        if tracking_number.upper() == "STOPSTOP":
            self.log("检测到 STOPSTOP 指令 → 停止录制")
            self.set_pending_tracking(None)
            self.stop_requested = False
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
        if self.disk_block_reason:
            self.set_pending_tracking(tracking_number)
            self.log(
                f"磁盘状态不满足录制要求（{self.disk_block_reason}），"
                "已禁止开始录制并保留最新单号"
            )
            return
        try:
            status = self._call_obs_client(client, "get_record_status")
            if getattr(status, "output_active", False):
                self.set_pending_tracking(tracking_number)
                self.orphan_recording_blocked = True
                self.resolve_orphan_button.show()
                self.log("⚠️ OBS 已在录制中，已保留最新单号并等待处理当前录像")
                QTimer.singleShot(0, self.resolve_orphan_recording)
                return
            self.current_tracking_number = tracking_number
            self.current_recording_id = uuid.uuid4().hex
            self.start_time = None
            self.recording_phase = "starting"
            self.recording_finalization_started = False
            self.recording_started_monotonic = None
            self.recording_deadline_monotonic = None
            with self.record_state_lock:
                self.last_record_output_path = None
            self.record_start_check_attempts = 0
            self.record_stop_retry_attempts = 0
            self.set_state("正在启动")
            self._call_obs_client(client, "start_record")
            self.log(f"已请求开始录制，等待 OBS 确认：{tracking_number}")
            QTimer.singleShot(
                RECORD_START_CONFIRM_INTERVAL_MS,
                self._confirm_recording_started,
            )
        except Exception as e:
            self.log(f"开始录制请求异常，正在核对 OBS 实际状态：{e}")
            try:
                status = self._call_obs_client(client, "get_record_status")
                if bool(getattr(status, "output_active", False)):
                    self.log("OBS 实际已开始录制，已恢复本地状态")
                    self._mark_recording_confirmed()
                    return
                self.log("OBS 暂未进入录制状态，将继续等待状态确认")
                self.record_start_check_attempts = 0
                QTimer.singleShot(
                    RECORD_START_CONFIRM_INTERVAL_MS,
                    self._confirm_recording_started,
                )
            except Exception as status_error:
                self.recording_phase = "waiting_reconnect"
                self.mark_obs_disconnected(status_error)

    def _mark_recording_confirmed(self):
        self.recording_phase = "recording"
        if self.start_time is None:
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.recording_started_monotonic is None:
            self.recording_started_monotonic = time.monotonic()
        if self.recording_deadline_monotonic is None:
            self.recording_deadline_monotonic = (
                self.recording_started_monotonic + MAX_RECORD_DURATION_MS / 1000
            )
        remaining_ms = max(
            1,
            int((self.recording_deadline_monotonic - time.monotonic()) * 1000),
        )
        self.set_state("录制中")
        self.timer.start(remaining_ms)
        self.log(f"OBS 已确认开始录制：{self.current_tracking_number}")
        if self.stop_requested or self.pending_tracking_number:
            self.stop_requested = False
            QTimer.singleShot(0, self.stop_recording)

    def _confirm_recording_started(self):
        if self.recording_phase != "starting" or self.current_tracking_number is None:
            return
        client = self.req_client
        if client is None:
            self.recording_phase = "waiting_reconnect"
            return
        try:
            status = self._call_obs_client(client, "get_record_status")
            is_recording = bool(getattr(status, "output_active", False))
        except Exception as exc:
            self.recording_phase = "waiting_reconnect"
            self.mark_obs_disconnected(exc)
            return

        if is_recording:
            self._mark_recording_confirmed()
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
        self.current_recording_id = None
        self.start_time = None
        self.recording_started_monotonic = None
        self.recording_deadline_monotonic = None
        self.recording_finalization_started = False
        with self.record_state_lock:
            self.last_record_output_path = None
        self.recording_phase = "idle"
        self.record_start_check_attempts = 0
        self.record_stop_retry_attempts = 0
        self.stop_requested = False
        self.set_state("已停止")
        self.update_info()
        # pending_tracking_number 是唯一事实来源；不得恢复早先捕获的旧单号。
        next_tracking_number = self.pending_tracking_number
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

    def stop_recording(self, start_new_tracking_number=None, _retry=False):
        if self.current_tracking_number is None:
            return
        if start_new_tracking_number:
            self.set_pending_tracking(start_new_tracking_number)
        if not _retry:
            self.record_stop_retry_attempts = 0
        if self.recording_phase == "starting":
            self.stop_requested = True
            self.log("OBS 尚在确认启动，确认录制后再切换单号")
            return
        if self.recording_phase in ("stopping", "saving"):
            if self.pending_tracking_number:
                self.log("当前录制正在停止或保存，最新单号已暂存")
            else:
                self.log("当前录制正在停止或保存")
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
        self.set_state("正在停止")
        try:
            response = self._call_obs_client(client, "stop_record")
            self.record_stop_retry_attempts = 0
            output_path = getattr(response, "output_path", None)
            self.log("OBS 已响应停止请求，正在保存文件…")
            self._begin_recording_finalization(
                output_path or self._latest_stopped_output_path(),
                requested_next_tracking_number=start_new_tracking_number,
                reason="停止请求",
            )
        except Exception as exc:
            self.log(f"停止录制请求异常，正在核对 OBS 实际状态：{exc}")
            try:
                status = self._call_obs_client(client, "get_record_status")
                is_recording = bool(getattr(status, "output_active", False))
            except Exception as status_error:
                self.stop_requested = True
                self.recording_phase = "waiting_reconnect"
                self.set_state("等待 OBS 重连")
                self.mark_obs_disconnected(status_error)
                return
            if not is_recording:
                self.log("OBS 确认录像已经结束，继续恢复保存")
                self._begin_recording_finalization(
                    self._latest_stopped_output_path(),
                    requested_next_tracking_number=start_new_tracking_number,
                    reason="停止请求异常后的状态对账",
                )
                return
            self.recording_phase = "recording"
            remaining_ms = MAX_RECORD_DURATION_MS
            if self.recording_deadline_monotonic is not None:
                remaining_ms = max(
                    1,
                    int((self.recording_deadline_monotonic - time.monotonic()) * 1000),
                )
            self.timer.start(remaining_ms)
            self.set_state("录制中")
            self.record_stop_retry_attempts += 1
            if self.record_stop_retry_attempts < RECORD_STOP_RETRY_ATTEMPTS:
                self.log(
                    "OBS 仍在录制，将再次发送停止请求 "
                    f"({self.record_stop_retry_attempts}/{RECORD_STOP_RETRY_ATTEMPTS})"
                )
                QTimer.singleShot(
                    RECORD_STOP_RETRY_INTERVAL_MS,
                    lambda: self.stop_recording(_retry=True),
                )
            else:
                self.log("OBS 连续拒绝停止请求，请检查 OBS 输出状态后重新扫码或使用 STOPSTOP")

    def _begin_recording_finalization(
        self,
        output_path,
        requested_next_tracking_number=None,
        reason="",
    ):
        """以单一幂等入口完成录像归档，避免事件/轮询/请求重复保存。"""
        if requested_next_tracking_number:
            self.set_pending_tracking(requested_next_tracking_number)
        if self.current_tracking_number is None:
            return
        if self.recording_finalization_started or self.recording_phase == "saving":
            return

        self.recording_finalization_started = True
        self.timer.stop()
        self.recording_phase = "saving"
        self.set_state("保存中")
        if reason:
            self.log(f"开始归档录像（{reason}）")

        tracking_number = self.current_tracking_number
        recording_id = self.current_recording_id or uuid.uuid4().hex
        self.current_recording_id = recording_id
        start_time = self.start_time
        started_monotonic = self.recording_started_monotonic
        client = self.req_client
        resolved_output_path = output_path or self._latest_stopped_output_path()

        def finalize_recording():
            success = False
            error_message = ""
            reservation = None
            move_completed = False
            end_time = time.strftime("%Y-%m-%d %H:%M:%S")
            duration = None
            if started_monotonic is not None:
                duration = max(0, int(time.monotonic() - started_monotonic))
            elif start_time:
                try:
                    duration = max(
                        0,
                        int(
                            time.mktime(time.strptime(end_time, "%Y-%m-%d %H:%M:%S"))
                            - time.mktime(time.strptime(start_time, "%Y-%m-%d %H:%M:%S"))
                        ),
                    )
                except (ValueError, OverflowError):
                    pass

            try:
                video_settings = self._call_obs_client(client, "get_video_settings")
                resolution, fps = video_settings_metadata(video_settings)
            except Exception:
                resolution, fps = "-", 0

            job = self._failed_job_payload(
                tracking_number,
                resolved_output_path,
                start_time,
                end_time,
                duration,
                resolution,
                fps,
                "等待归档",
                recording_id=recording_id,
            )
            try:
                if not resolved_output_path or not os.path.isfile(resolved_output_path):
                    raise FileNotFoundError("无法定位 OBS 输出文件，需人工检查 OBS 输出目录")
                wait_for_recording_file_ready(resolved_output_path)

                extension = os.path.splitext(resolved_output_path)[1] or ".mkv"
                date_dir = os.path.join(RECORD_PATH, end_time[:10])
                os.makedirs(date_dir, exist_ok=True)
                filename = f"{sanitize_tracking_filename(tracking_number)}{extension}"
                destination, reservation = reserve_unique_filename(date_dir, filename)

                job["planned_destination"] = destination
                job["state"] = "moving"
                failed_jobs.upsert(job)
                ensure_copy_space(resolved_output_path, date_dir, DISK_COPY_RESERVE_GB)
                move_recording_file(resolved_output_path, destination)
                move_completed = True

                job["source_path"] = destination
                job["archived_path"] = destination
                job["state"] = "recording_metadata"
                failed_jobs.upsert(job)
                self.worker_log.emit(f"录制完成：{destination}")

                file_size = wait_for_recording_file_ready(
                    destination,
                    stable_checks=0,
                )
                db.insert_record(
                    tracking_number,
                    start_time,
                    end_time,
                    duration,
                    destination,
                    file_size,
                    resolution,
                    fps,
                    recording_id=recording_id,
                )
                failed_jobs.remove(recording_id)
                success = True
                self.worker_log.emit(f"JSONL 已记录单号：{tracking_number}")
            except Exception as exc:
                error_message = str(exc)
                job["last_error"] = error_message
                job["last_retry_at"] = datetime.now().isoformat(timespec="seconds")
                if move_completed and job.get("planned_destination") and os.path.isfile(
                    job["planned_destination"]
                ):
                    job["source_path"] = job["planned_destination"]
                    job["archived_path"] = job["planned_destination"]
                    job["state"] = "recording_metadata"
                try:
                    failed_jobs.upsert(job)
                    self.worker_log.emit(
                        f"录制文件未能完整归档，已加入恢复任务：{tracking_number} ({exc})"
                    )
                except Exception as store_error:
                    error_message = f"{error_message}；恢复任务也无法写入：{store_error}"
                    self.worker_log.emit(f"严重：{error_message}")
            finally:
                try:
                    release_filename_reservation(reservation)
                except OSError as cleanup_error:
                    self.worker_log.emit(f"无法清理文件名保留标记：{cleanup_error}")
                self.recording_finalized.emit(
                    requested_next_tracking_number,
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
        self._reset_recording_state_and_continue()

    def poll_obs_status(self):
        """轮询 OBS 的场景和录像状态，作为事件丢失时的状态对账。"""
        client = self.req_client
        if client is None:
            self.ensure_obs_connection()
            return
        try:
            scene_info = self._call_obs_client(client, "get_current_program_scene")
            self.current_scene = getattr(scene_info, "current_program_scene_name", "-")
            status = self._call_obs_client(client, "get_record_status")
            is_recording = bool(getattr(status, "output_active", False))
            output_path = getattr(status, "output_path", None)
            self.update_info()
        except Exception as exc:
            self.mark_obs_disconnected(exc)
            return

        local_recording = (
            self.current_tracking_number is not None
            and self.recording_phase
            in ("recording", "stopping", "waiting_reconnect")
        )
        if local_recording and not is_recording:
            self.log("轮询发现 OBS 录像已结束，开始状态对账")
            self._begin_recording_finalization(
                output_path or self._latest_stopped_output_path(),
                reason="OBS 状态轮询",
            )
        elif (
            is_recording
            and self.current_tracking_number is None
            and not self.orphan_recording_blocked
        ):
            self.orphan_recording_blocked = True
            self.resolve_orphan_button.show()
            self.set_state("OBS 已在录制，扫码禁用")
            self.log("检测到不受程序管理的 OBS 录像，扫码功能已禁用")
        elif (
            not is_recording
            and self.current_tracking_number is None
            and self.orphan_recording_blocked
        ):
            self._clear_orphan_recording_block("轮询确认孤立录像已结束，扫码功能已恢复")

    def _shutdown_and_quit(self):
        self.force_closing = True
        self.reconnect_timer.stop()
        self.window_arrange_timer.stop()
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.keyboard_layout_timer.stop()
        self.timer.stop()
        self.display_window.hide()
        with self.obs_request_lock:
            self._disconnect_client(self.event_client)
            self._disconnect_client(self.req_client)
            self.event_client = None
            self.req_client = None
        self.hide()
        QApplication.quit()

    def closeEvent(self, event):
        if self.force_closing:
            event.accept()
            return

        if self.retry_in_progress:
            event.ignore()
            QMessageBox.warning(
                self,
                "正在恢复文件",
                "保存失败任务仍在恢复中，请等待完成后再退出。",
            )
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

        try:
            failed_job_count = failed_jobs.count()
        except Exception as exc:
            event.ignore()
            QMessageBox.critical(
                self,
                "无法确认恢复任务",
                f"无法读取恢复任务文件，为避免遗漏录像，程序不会退出：{exc}",
            )
            return
        if failed_job_count:
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
        duplicate_dialog = QMessageBox(
            QMessageBox.Warning,
            "程序已运行",
            "发货视频录制程序已经在运行。",
        )
        duplicate_dialog.setStandardButtons(QMessageBox.Ok)
        QTimer.singleShot(2000, duplicate_dialog.accept)
        duplicate_dialog.exec()
        sys.exit(1)
    app_window = RecorderApp()
    app_window.show()
    sys.exit(app.exec())
