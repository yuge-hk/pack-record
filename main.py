# -*- coding: utf-8 -*-
import sys
import os
import errno
import csv
import time
import json
import threading
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLineEdit,
    QLabel, QTextEdit, QPushButton, QCheckBox, QHBoxLayout
)
from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QFont
import obsws_python as obs

# ================= 配置 =================
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = "83vMnnkOgq0nHu6l"
BASE_DIR = Path(__file__).resolve().parent
RECORD_PATH = str(BASE_DIR / "recordings")   # 基础目录
MAX_RECORD_DURATION_MS = 20 * 60 * 1000       # 最长 20 分钟
DISK_WARN_GB = 40                             # 磁盘小于40GB时变红色
RECORD_START_CONFIRM_INTERVAL_MS = 100
RECORD_START_CONFIRM_ATTEMPTS = 50
DUPLICATE_SCAN_WINDOW_SECONDS = 1.0
JSON_DIR = BASE_DIR / "records"
OBS_EXECUTABLE_CANDIDATES = (
    Path(r"C:\Program Files\obs-studio\bin\64bit\obs64.exe"),
    Path(r"C:\Program Files (x86)\obs-studio\bin\64bit\obs64.exe"),
)

os.makedirs(RECORD_PATH, exist_ok=True)
JSON_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- JSON 数据管理 ----------------
class VideoDB:
    """按日期保存录制记录，并允许录制收尾线程安全写入。"""

    def __init__(self):
        self.lock = threading.Lock()

    @staticmethod
    def _get_json_path():
        return JSON_DIR / f"{time.strftime('%Y-%m-%d')}.json"

    def insert_record(self, barcode, start_time, end_time, duration,
                      video_path, file_size, resolution, fps):
        record = {
            "barcode": barcode,
            "start_time": start_time,
            "end_time": end_time,
            "duration": duration,
            "video_path": video_path,
            "file_size": file_size,
            "resolution": resolution,
            "fps": fps,
            "created_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.lock:
            path = self._get_json_path()
            if path.exists():
                try:
                    records = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(records, list):
                        records = []
                except (OSError, json.JSONDecodeError):
                    records = []
            else:
                records = []
            records.append(record)
            temporary_path = path.with_suffix(".json.tmp")
            temporary_path.write_text(
                json.dumps(records, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(temporary_path, path)

    def close(self):
        pass


# ---------------- 工具函数 ----------------
def unique_filename(base_dir, filename):
    """在 base_dir 下生成不重复的文件名"""
    name, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(base_dir, new_name)):
        new_name = f"{name}({counter}){ext}"
        counter += 1
    return os.path.join(base_dir, new_name)


def move_recording_file(source, destination, retries=10, retry_delay=0.5):
    """移动 OBS 输出；同盘原子重命名，跨磁盘安全复制后删除源文件。"""
    last_error = None
    partial_destination = f"{destination}.part"
    for _ in range(retries):
        # 跨盘复制已经完成但源文件暂时无法删除时，只重试删除源文件。
        if os.path.isfile(source) and os.path.isfile(destination):
            try:
                if os.path.getsize(source) == os.path.getsize(destination):
                    os.unlink(source)
                    return destination
            except OSError as exc:
                last_error = exc
                time.sleep(retry_delay)
                continue
        try:
            os.replace(source, destination)
            return destination
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                last_error = exc
                time.sleep(retry_delay)
                continue
        try:
            shutil.copy2(source, partial_destination)
            os.replace(partial_destination, destination)
            os.unlink(source)
            return destination
        except OSError as exc:
            last_error = exc
            time.sleep(retry_delay)
    try:
        if os.path.exists(partial_destination):
            os.unlink(partial_destination)
    except OSError:
        pass
    raise RuntimeError(f"无法移动录制文件: {last_error}") from last_error


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


db = VideoDB()


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


class RecorderApp(QWidget):
    obs_connection_ready = Signal(object, object, str)
    obs_connection_failed = Signal(str)
    obs_scene_changed = Signal(str)
    worker_log = Signal(str)
    recording_finalized = Signal(object)

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
        QTimer.singleShot(0, self.ensure_obs_connection)
        QTimer.singleShot(0, self.launch_obs_and_prepare_windows)
        QTimer.singleShot(100, self.focus_scan_input)

    # ---------------- UI辅助 ----------------
    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.log_box.append(line)
        print(line)
        self.update_info()

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
        if self.pending_tracking_number and self.current_tracking_number is None:
            tracking_number = self.pending_tracking_number
            self.pending_tracking_number = None
            QTimer.singleShot(0, lambda: self.start_recording(tracking_number))

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
            if free_gb < DISK_WARN_GB:
                self.disk_label.setStyleSheet("font-size: 14px; color: red; font-weight: bold;")
            else:
                self.disk_label.setStyleSheet("font-size: 14px; color: green;")
            self.disk_label.setText(f"磁盘剩余：{free_gb:.1f} GB")
        except Exception as e:
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
            self.pending_tracking_number = tracking_number
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
            self.pending_tracking_number = tracking_number
            self.log(f"OBS 正在启动录制，已暂存下一单号：{tracking_number}")
        elif self.recording_phase in ("stopping", "saving"):
            self.pending_tracking_number = tracking_number
            self.log(f"上一单正在保存，已暂存下一单号：{tracking_number}")
        else:
            self.stop_recording(start_new_tracking_number=tracking_number)

    def start_recording(self, tracking_number):
        if self.recording_phase != "idle" or self.current_tracking_number is not None:
            if tracking_number != self.current_tracking_number:
                self.pending_tracking_number = tracking_number
                self.log(f"当前状态暂不能启动，已暂存单号：{tracking_number}")
            return
        client = self.req_client
        if client is None:
            self.pending_tracking_number = tracking_number
            self.ensure_obs_connection()
            return
        try:
            status = client.get_record_status()
            if getattr(status, "output_active", False):
                self.log("⚠️ OBS 已在录制中，无法再次开始")
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

    def _reset_recording_state_and_continue(self, requested_next_tracking_number=None):
        self.timer.stop()
        self.current_tracking_number = None
        self.start_time = None
        self.recording_phase = "idle"
        self.record_start_check_attempts = 0
        self.stop_requested = False
        self.set_state("已停止")
        self.update_info()
        next_tracking_number = self.pending_tracking_number or requested_next_tracking_number
        self.pending_tracking_number = None
        if next_tracking_number:
            QTimer.singleShot(0, lambda: self.start_recording(next_tracking_number))

    def force_stop_recording(self):
        if self.current_tracking_number:
            self.log(f"⚠️ 超过 {MAX_RECORD_DURATION_MS/60000:.0f} 分钟，自动停止录制")
        self.stop_recording()

    def stop_recording(self, start_new_tracking_number=None):
        if self.current_tracking_number is None:
            return
        if start_new_tracking_number:
            self.pending_tracking_number = start_new_tracking_number
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
            try:
                if output_path:
                    _, ext = os.path.splitext(output_path)
                    if not ext:
                        ext = ".mkv"

                    # 按日期目录保存
                    today_str = time.strftime("%Y-%m-%d")
                    date_dir = os.path.join(RECORD_PATH, today_str)
                    os.makedirs(date_dir, exist_ok=True)

                    filename = f"{current_tracking_number}{ext}"
                    dst = unique_filename(date_dir, filename)
                    src = output_path

                    try:
                        move_recording_file(src, dst)
                        self.worker_log.emit(f"录制完成：{dst}")
                    except Exception as exc:
                        self.worker_log.emit(f"录制完成，但无法移动文件：{output_path} ({exc})")
                        return

                    # 数据库写入
                    end_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    duration = None
                    if current_start_time:
                        try:
                            t1 = time.mktime(time.strptime(current_start_time, "%Y-%m-%d %H:%M:%S"))
                            t2 = time.mktime(time.strptime(end_time, "%Y-%m-%d %H:%M:%S"))
                            duration = int(t2 - t1)
                        except Exception:
                            pass

                    file_size = os.path.getsize(dst) if os.path.exists(dst) else 0
                    try:
                        vs = client.get_video_settings()
                        resolution = f"{vs.base_width}x{vs.base_height}"
                        fps = vs.fpsNumerator // vs.fpsDenominator if vs.fpsDenominator else 0
                    except Exception:
                        resolution, fps = "-", 0

                    db.insert_record(
                        current_tracking_number, current_start_time, end_time, duration,
                        dst, file_size, resolution, fps
                    )
                    self.worker_log.emit(f"JSON 已记录单号：{current_tracking_number}")

            finally:
                self.recording_finalized.emit(start_new_tracking_number)

        threading.Thread(target=finalize_recording, daemon=True).start()

    def _on_recording_finalized(self, requested_next_tracking_number):
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

    def closeEvent(self, event):
        self.reconnect_timer.stop()
        self.window_arrange_timer.stop()
        self.poll_timer.stop()
        self.disk_timer.stop()
        self.timer.stop()
        self.display_window.hide()
        self._disconnect_client(self.event_client)
        self._disconnect_client(self.req_client)
        event.accept()
        QApplication.quit()


# ---------------- 主程序 ----------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app_window = RecorderApp()
    app_window.show()
    sys.exit(app.exec())
