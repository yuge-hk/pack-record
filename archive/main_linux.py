#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发货视频录制系统 - Linux版本（可直接运行）
依赖：
    pip install PySide6 obsws-python
"""

import sys
import os
import time
import json
import threading
import shutil
from pathlib import Path
from datetime import datetime
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QLineEdit,
    QLabel, QTextEdit, QPushButton, QHBoxLayout
)
from PySide6.QtCore import QTimer, Qt, Signal, QObject
from PySide6.QtGui import QFont
import obsws_python as obs

# ================= 配置 =================
OBS_WS_HOST = "localhost"
OBS_WS_PORT = 4455
OBS_WS_PASSWORD = "83vMnnkOgq0nHu6l"
RECORD_PATH = os.path.abspath("./recordings")
MAX_RECORD_DURATION_MS = 20 * 60 * 1000
DISK_WARN_GB = 40
JSON_DIR = Path("./records")

os.makedirs(RECORD_PATH, exist_ok=True)
JSON_DIR.mkdir(parents=True, exist_ok=True)

# ---------------- JSON 数据存储 ----------------
class VideoDB:
    """使用 JSON 文件存储每日录制记录"""
    def __init__(self):
        self.lock = threading.Lock()

    def _get_json_path(self):
        today_str = time.strftime("%Y-%m-%d")
        return JSON_DIR / f"{today_str}.json"

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
            "created_at": datetime.now().isoformat(timespec="seconds")
        }
        with self.lock:
            path = self._get_json_path()
            if path.exists():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, list):
                        data = []
                except Exception:
                    data = []
            else:
                data = []
            data.append(record)
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def close(self):
        pass


# ---------------- 工具函数 ----------------
def unique_filename(base_dir, filename):
    name, ext = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(base_dir, new_name)):
        new_name = f"{name}({counter}){ext}"
        counter += 1
    return os.path.join(base_dir, new_name)


# ---------------- 信号对象 ----------------
class RecordSignals(QObject):
    update_display = Signal(str, str, str)


# 全局变量
req_client = None
event_client = None
db = VideoDB()
record_signals = RecordSignals()


# ---------------- OBS 信息显示窗口 ----------------
class OBSDisplayWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("录制信息 - OBS采集窗口")
        self.setGeometry(100, 100, 800, 400)
        self.setStyleSheet("background-color: #00FF00;")

        layout = QVBoxLayout()
        layout.setContentsMargins(40, 40, 40, 40)
        layout.setSpacing(20)

        self.tracking_label = QLabel("单号：-")
        self.tracking_label.setAlignment(Qt.AlignCenter)
        font1 = QFont("DejaVu Sans", 48, QFont.Bold)
        self.tracking_label.setFont(font1)
        self.tracking_label.setStyleSheet(
            "color: #FFFFFF; background-color: #000000; padding: 20px; border-radius: 10px;"
        )
        layout.addWidget(self.tracking_label)

        time_layout = QHBoxLayout()

        self.current_time_label = QLabel("当前时间：--:--:--")
        font2 = QFont("DejaVu Sans", 28, QFont.Bold)
        self.current_time_label.setFont(font2)
        self.current_time_label.setStyleSheet(
            "color: #FFFFFF; background-color: #0066CC; padding: 15px; border-radius: 8px;"
        )
        self.current_time_label.setAlignment(Qt.AlignCenter)
        time_layout.addWidget(self.current_time_label)

        self.duration_label = QLabel("已录制：00:00")
        self.duration_label.setFont(font2)
        self.duration_label.setStyleSheet(
            "color: #FFFFFF; background-color: #CC0000; padding: 15px; border-radius: 8px;"
        )
        self.duration_label.setAlignment(Qt.AlignCenter)
        time_layout.addWidget(self.duration_label)
        layout.addLayout(time_layout)

        tip_label = QLabel("提示：在OBS中使用色度键滤镜去除绿色背景")
        tip_label.setAlignment(Qt.AlignCenter)
        tip_label.setStyleSheet("color: #000000; font-size: 14px;")
        layout.addWidget(tip_label)

        self.setLayout(layout)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_current_time)
        self.timer.start(1000)

        self.start_time = None
        self.update_current_time()

    def update_current_time(self):
        current = datetime.now().strftime("%H:%M:%S")
        self.current_time_label.setText(f"当前时间：{current}")
        if self.start_time:
            elapsed = datetime.now() - self.start_time
            minutes = int(elapsed.total_seconds() // 60)
            seconds = int(elapsed.total_seconds() % 60)
            self.duration_label.setText(f"已录制：{minutes:02d}:{seconds:02d}")

    def update_info(self, tracking_number, start_time, duration):
        if tracking_number:
            self.tracking_label.setText(f"单号：{tracking_number}")
            if start_time:
                try:
                    self.start_time = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
                except Exception:
                    self.start_time = None
        else:
            self.tracking_label.setText("单号：-")
            self.duration_label.setText("已录制：00:00")
            self.start_time = None


# ---------------- 主控制窗口 ----------------
class RecorderApp(QWidget):
    finalize_done = Signal(object)
    log_signal = Signal(str)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("发货视频录制 - Linux版")
        self.resize(700, 600)

        layout = QVBoxLayout()
        self.label = QLabel("条码枪扫描快递单号后回车即可开始录制：")
        layout.addWidget(self.label)

        self.input = QLineEdit()
        self.input.returnPressed.connect(self.handle_input)
        layout.addWidget(self.input)

        btn_layout = QHBoxLayout()
        self.stop_button = QPushButton("停止录制")
        self.stop_button.clicked.connect(self.stop_recording)
        btn_layout.addWidget(self.stop_button)

        self.show_display_button = QPushButton("显示/隐藏 OBS信息窗口")
        self.show_display_button.clicked.connect(self.toggle_display_window)
        btn_layout.addWidget(self.show_display_button)
        layout.addLayout(btn_layout)

        self.tracking_label = QLabel("当前单号：-")
        self.tracking_label.setStyleSheet("font-size: 28px; font-weight: bold; color: blue;")
        layout.addWidget(self.tracking_label)

        self.state_label = QLabel("录制状态：-")
        self.state_label.setStyleSheet("font-size: 20px; color: gray; font-weight: bold;")
        layout.addWidget(self.state_label)

        self.info_label = QLabel("当前场景：-")
        layout.addWidget(self.info_label)

        self.disk_label = QLabel("磁盘剩余：-")
        layout.addWidget(self.disk_label)

        self.log_box = QTextEdit()
        self.log_box.setReadOnly(True)
        layout.addWidget(self.log_box, stretch=1)
        self.setLayout(layout)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.force_stop_recording)

        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll_obs_status)
        self.poll_timer.start(5000)

        self.disk_timer = QTimer(self)
        self.disk_timer.timeout.connect(self.update_disk_space)
        self.disk_timer.start(10000)
        self.update_disk_space()

        self.current_tracking_number = None
        self.start_time = None
        self.current_scene = "-"
        self.is_processing_stop = False  # 添加标志防止重复处理停止操作
        self.waiting_for_new_recording = None  # 存储待开始的新单号

        self.display_window = OBSDisplayWindow()
        self.display_window.show()

        record_signals.update_display.connect(self.display_window.update_info)
        self.finalize_done.connect(self._on_finalize_done)
        self.log_signal.connect(self._on_log_signal)

    # ===== UI更新逻辑 =====
    def toggle_display_window(self):
        if self.display_window.isVisible():
            self.display_window.hide()
        else:
            self.display_window.show()

    def _on_log_signal(self, text: str):
        self.log_box.append(text)
        self.update_info()

    def log(self, msg: str):
        timestamp = time.strftime("%H:%M:%S")
        line = f"[{timestamp}] {msg}"
        self.log_signal.emit(line)
        print(line)

    def set_state(self, state: str):
        color = {"录制中": "green", "保存中": "blue", "已停止": "gray"}.get(state, "black")
        self.state_label.setText(f"录制状态：{state}")
        self.state_label.setStyleSheet(f"font-size:20px; color:{color}; font-weight:bold;")

    def update_info(self):
        tracking = self.current_tracking_number or "-"
        self.tracking_label.setText(f"当前单号：{tracking}")
        self.info_label.setText(f"当前场景：{self.current_scene}")
        record_signals.update_display.emit(self.current_tracking_number or "", self.start_time or "", "")

    def update_disk_space(self):
        try:
            usage = shutil.disk_usage(RECORD_PATH)
            free_gb = usage.free / (1024**3)
            color = "red" if free_gb < DISK_WARN_GB else "green"
            self.disk_label.setStyleSheet(f"font-size:14px; color:{color};")
            self.disk_label.setText(f"磁盘剩余：{free_gb:.1f} GB")
        except Exception as e:
            self.disk_label.setText(f"磁盘剩余：未知 ({e})")

    # ===== 录制逻辑 =====
    def handle_input(self):
        tracking_number = self.input.text().strip()
        self.input.clear()
        if not tracking_number:
            return
        if tracking_number.upper() == "STOPSTOP":
            self.stop_recording()
            return
        if req_client is None:
            self.log("错误：未连接到 OBS WebSocket")
            return
            
        # 检查是否正在录制或正在处理停止操作
        try:
            status = req_client.get_record_status()
            is_recording = getattr(status, "output_active", False)
        except Exception:
            self.log("无法获取OBS录制状态")
            return
        
        if is_recording and self.current_tracking_number is not None:
            # 正在录制中，需要先停止当前录制，然后开始新的录制
            self.log(f"当前正在录制单号：{self.current_tracking_number}，准备切换到新单号：{tracking_number}")
            self.stop_recording(start_new_tracking_number=tracking_number)
        elif not is_recording and self.current_tracking_number is None:
            # 没有在录制，开始新的录制
            self.start_recording(tracking_number)
        else:
            # 其他情况，比如正在保存中
            self.log(f"当前状态不允许开始录制，状态：{self.state_label.text()}")

    def start_recording(self, tracking_number):
        # 尝试多次检查OBS状态，确保可以开始录制
        max_attempts = 10
        attempt = 0
        while attempt < max_attempts:
            try:
                status = req_client.get_record_status()
                is_recording = getattr(status, "output_active", False)
                if not is_recording:
                    # 可以开始录制
                    req_client.start_record()
                    self.current_tracking_number = tracking_number
                    self.start_time = time.strftime("%Y-%m-%d %H:%M:%S")
                    self.set_state("录制中")
                    self.timer.start(MAX_RECORD_DURATION_MS)
                    self.log(f"开始录制：{tracking_number}")
                    self.update_info()
                    return
                else:
                    self.log(f"尝试开始录制时发现OBS仍在录制中，等待... (尝试 {attempt+1}/{max_attempts})")
                    time.sleep(0.5)
                    attempt += 1
            except Exception as e:
                self.log(f"检查OBS状态时出错: {e}")
                time.sleep(0.5)
                attempt += 1
        
        # 如果多次尝试后仍然失败
        self.log(f"无法开始录制单号 {tracking_number}，OBS状态仍为录制中")

    def force_stop_recording(self):
        if self.current_tracking_number:
            self.log("⚠️ 超时自动停止录制")
        self.stop_recording()

    def stop_recording(self, start_new_tracking_number=None):
        if self.is_processing_stop:
            self.log("已在处理停止录制，忽略重复操作")
            if start_new_tracking_number:
                # 更新待开始的新录制
                self.waiting_for_new_recording = start_new_tracking_number
            return
            
        self.timer.stop()
        if req_client is None or (self.current_tracking_number is None and start_new_tracking_number is None):
            return
            
        # 设置标志位防止重复处理
        self.is_processing_stop = True
        
        try:
            # 检查当前是否在录制
            status = req_client.get_record_status()
            is_recording = getattr(status, "output_active", False)
            
            if is_recording:
                resp = req_client.stop_record()
                output_path = getattr(resp, "output_path", None)
                self.log("录制结束，正在保存文件...")
                self.set_state("保存中")
                
                def finalize(current_tracking, start_time_val, output_path_val, next_tracking):
                    try:
                        if output_path_val:
                            _, ext = os.path.splitext(output_path_val)
                            if not ext:
                                ext = ".mkv"
                            today_str = time.strftime("%Y-%m-%d")
                            date_dir = os.path.join(RECORD_PATH, today_str)
                            os.makedirs(date_dir, exist_ok=True)
                            dst = unique_filename(date_dir, f"{current_tracking}{ext}")
                            for _ in range(10):
                                try:
                                    os.replace(output_path_val, dst)
                                    self.log_signal.emit(f"录制完成：{dst}")
                                    break
                                except Exception:
                                    time.sleep(0.5)
                            end_time = time.strftime("%Y-%m-%d %H:%M:%S")
                            duration = None
                            if start_time_val:
                                try:
                                    t1 = time.mktime(time.strptime(start_time_val, "%Y-%m-%d %H:%M:%S"))
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
                            db.insert_record(current_tracking, start_time_val, end_time, duration, dst, file_size, resolution, fps)
                            self.log_signal.emit(f"数据库已记录单号：{current_tracking}")
                    finally:
                        self.finalize_done.emit(next_tracking)
                        # 重置标志位
                        self.is_processing_stop = False

                cur_track = self.current_tracking_number
                cur_start_time = self.start_time
                next_track = start_new_tracking_number
                threading.Thread(target=finalize, args=(cur_track, cur_start_time, output_path, next_track), daemon=True).start()
            else:
                # 如果当前不在录制，但有待启动的新录制，则直接开始
                self.is_processing_stop = False
                if start_new_tracking_number:
                    self.log("当前无录制任务，直接开始新录制")
                    self.start_recording(start_new_tracking_number)
                    
        except Exception as e:
            self.log(f"停止录制失败: {e}")
            self.is_processing_stop = False
            if start_new_tracking_number:
                # 如果停止失败但仍需开始新录制，则等待片刻后开始
                def delayed_start():
                    self.start_recording(start_new_tracking_number)
                QTimer.singleShot(1000, delayed_start)

    def _on_finalize_done(self, start_new_tracking_number):
        self.current_tracking_number = None
        self.start_time = None
        self.set_state("已停止")
        self.update_info()
        
        # 如果在处理过程中有新的录制请求，则开始新的录制
        if self.waiting_for_new_recording:
            new_tracking = self.waiting_for_new_recording
            self.waiting_for_new_recording = None
            self.log(f"处理等待的新录制请求：{new_tracking}")
            # 延迟一点时间再开始新录制，确保OBS状态完全就绪
            QTimer.singleShot(500, lambda: self.start_recording(new_tracking))
        elif start_new_tracking_number:
            # 延迟一点时间再开始新录制，确保上一个文件完全保存
            QTimer.singleShot(500, lambda: self.start_recording(start_new_tracking_number))

    def poll_obs_status(self):
        if req_client is None:
            return
        try:
            scene_info = req_client.get_current_program_scene()
            scene_name = getattr(scene_info, "current_program_scene_name", None) or \
                         scene_info.get("sceneName", "-") if isinstance(scene_info, dict) else "-"
            self.current_scene = scene_name
            self.update_info()
        except Exception as e:
            self.log(f"轮询OBS状态失败: {e}")


# ---------------- 主程序 ----------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = RecorderApp()
    win.show()

    try:
        req_client = obs.ReqClient(host=OBS_WS_HOST, port=OBS_WS_PORT, password=OBS_WS_PASSWORD)
        event_client = obs.EventClient(host=OBS_WS_HOST, port=OBS_WS_PORT, password=OBS_WS_PASSWORD)
        def on_scene_changed(data):
            scene = data.get("sceneName", "-") if isinstance(data, dict) else str(data)
            win.current_scene = scene
            win.log(f"[事件] 场景切换到: {scene}")
        try:
            event_client.callback.register(on_scene_changed)
            win.log("成功初始化 OBS WebSocket 事件监听")
        except Exception:
            win.log("已连接 OBS WebSocket（事件注册可能不支持当前库版本）")
    except Exception as e:
        win.log(f"无法连接到 OBS WebSocket: {e}")

    sys.exit(app.exec())

