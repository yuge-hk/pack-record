# -*- coding: utf-8 -*-
import sys
import os
import time
import sqlite3
import threading
import shutil
from pathlib import Path
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLineEdit,
    QLabel, QTextEdit, QPushButton
)
from PySide6.QtCore import QTimer
import obsws_python as obs

# ================= 配置 =================
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = "83vMnnkOgq0nHu6l"
RECORD_PATH = os.path.abspath("recordings")   # 基础目录
MAX_RECORD_DURATION_MS = 20 * 60 * 1000       # 最长 20 分钟
DISK_WARN_GB = 40                             # 磁盘小于40GB时变红色

if not os.path.exists(RECORD_PATH):
    os.makedirs(RECORD_PATH, exist_ok=True)

# ---------------- SQLite 管理 ----------------
class VideoDB:
    def __init__(self):
        today_str = time.strftime("%Y-%m-%d")
        self.db_path = Path("sql") / today_str
        self.db_path.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path / "record.db")
        self._create_table()

    def _create_table(self):
        self.conn.execute("""
        CREATE TABLE IF NOT EXISTS records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            barcode TEXT,
            start_time TEXT,
            end_time TEXT,
            duration INTEGER,
            video_path TEXT,
            file_size INTEGER,
            resolution TEXT,
            fps INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        self.conn.commit()

    def insert_record(self, barcode, start_time, end_time, duration,
                      video_path, file_size, resolution, fps):
        self.conn.execute("""
        INSERT INTO records(barcode, start_time, end_time, duration,
                            video_path, file_size, resolution, fps)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (barcode, start_time, end_time, duration, video_path, file_size, resolution, fps))
        self.conn.commit()

    def close(self):
        self.conn.close()


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


# 全局 OBS 客户端
req_client = None
event_client = None
db = VideoDB()


class RecorderApp(QWidget):
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
        layout.addWidget(self.stop_button)

        # ===== 信息区 =====
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
        self.timer = QTimer()
        self.timer.timeout.connect(self.force_stop_recording)

        # 定时器：每5秒轮询一次OBS状态（兜底）
        self.poll_timer = QTimer()
        self.poll_timer.timeout.connect(self.poll_obs_status)
        self.poll_timer.start(5000)

        # 定时器：每10秒刷新磁盘空间
        self.disk_timer = QTimer()
        self.disk_timer.timeout.connect(self.update_disk_space)
        self.disk_timer.start(10000)
        self.update_disk_space()

        # 状态变量
        self.current_tracking_number = None
        self.start_time = None
        self.current_scene = "-"

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

        if req_client is None:
            self.log("错误：未连接到 OBS WebSocket")
            return
        if self.current_tracking_number is None:
            self.start_recording(tracking_number)
        else:
            self.stop_recording(start_new_tracking_number=tracking_number)

    def start_recording(self, tracking_number):
        try:
            status = req_client.get_record_status()
            if getattr(status, "output_active", False):
                self.log("⚠️ OBS 已在录制中，无法再次开始")
                return
            req_client.start_record()
            self.current_tracking_number = tracking_number
            self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
            self.set_state("录制中")
            self.timer.start(MAX_RECORD_DURATION_MS)
            self.log(f"开始录制：{tracking_number}")
        except Exception as e:
            self.log(f"无法开始录制: {e}")
            self.current_tracking_number = None

    def force_stop_recording(self):
        if self.current_tracking_number:
            self.log(f"⚠️ 超过 {MAX_RECORD_DURATION_MS/60000:.0f} 分钟，自动停止录制")
        self.stop_recording()

    def stop_recording(self, start_new_tracking_number=None):
        self.timer.stop()
        if req_client is None or self.current_tracking_number is None:
            return

        try:
            resp = req_client.stop_record()
            output_path = getattr(resp, "output_path", None)
            self.log("录制结束，正在保存文件...")
            self.set_state("保存中")
        except Exception as e:
            self.log(f"停止录制失败: {e}")
            self.current_tracking_number = None
            self.set_state("已停止")
            return

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

                    filename = f"{self.current_tracking_number}{ext}"
                    dst = unique_filename(date_dir, filename)
                    src = output_path

                    for _ in range(10):
                        try:
                            os.replace(src, dst)
                            self.log(f"录制完成：{dst}")
                            break
                        except PermissionError:
                            time.sleep(0.5)
                    else:
                        self.log(f"录制完成，但无法重命名文件：{output_path}")

                    # 数据库写入
                    end_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    duration = None
                    if self.start_time:
                        try:
                            t1 = time.mktime(time.strptime(self.start_time, "%Y-%m-%d %H:%M:%S"))
                            t2 = time.mktime(time.strptime(end_time, "%Y-%m-%d %H:%M:%S"))
                            duration = int(t2 - t1)
                        except Exception:
                            pass

                    file_size = os.path.getsize(dst) if os.path.exists(dst) else 0
                    try:
                        vs = req_client.get_video_settings()
                        resolution = f"{vs.base_width}x{vs.base_height}"
                        fps = vs.fpsNumerator // vs.fpsDenominator if vs.fpsDenominator else 0
                    except Exception:
                        resolution, fps = "-", 0

                    db.insert_record(
                        self.current_tracking_number, self.start_time, end_time, duration,
                        dst, file_size, resolution, fps
                    )
                    self.log(f"数据库已记录单号：{self.current_tracking_number}")

            finally:
                self.current_tracking_number = None
                self.start_time = None
                self.set_state("已停止")
                self.update_info()
                if start_new_tracking_number:
                    self.start_recording(start_new_tracking_number)

        threading.Thread(target=finalize_recording, daemon=True).start()

    def poll_obs_status(self):
        """轮询兜底，不覆盖事件状态"""
        if req_client is None:
            return
        try:
            scene_info = req_client.get_current_program_scene()
            self.current_scene = getattr(scene_info, "current_program_scene_name", "-")
            self.update_info()
        except Exception as e:
            self.log(f"轮询OBS状态失败: {e}")


# ---------------- 主程序 ----------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    app_window = RecorderApp()
    app_window.show()

    try:
        req_client = obs.ReqClient(host=OBS_WS_HOST, port=OBS_WS_PORT, password=OBS_WS_PASSWORD)
        event_client = obs.EventClient(host=OBS_WS_HOST, port=OBS_WS_PORT, password=OBS_WS_PASSWORD)

        # 事件回调
        def current_program_scene_changed(data):
            scene = data.get("sceneName", "-")
            app_window.current_scene = scene
            app_window.log(f"[事件] 场景切换到: {scene}")

        event_client.callback.register(current_program_scene_changed)
        app_window.log("成功初始化 OBS WebSocket 事件监听")

    except Exception as e:
        app_window.log(f"无法连接到 OBS WebSocket: {e}")

    app.exec()
