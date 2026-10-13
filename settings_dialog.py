"""Camera, audio, encoding and storage setup for Windows and Linux."""
import os
import threading

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QSpinBox,
                               QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from ffmpeg_backend import available_encoders, camera_modes, discover_devices


class SettingsDialog(QDialog):
    diagnostic_done = Signal(str, object)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = dict(settings)
        self.setWindowTitle("摄像头、麦克风与录像设置")
        self.resize(720, 660)
        self.fields, self.busy = {}, False
        self.diagnostic_done.connect(self._diagnostic_done, Qt.QueuedConnection)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)

        devices = self._tab(tabs, "摄像头与声音")
        self._combo(devices, "camera", "USB 摄像头", [], editable=True)
        self._combo(devices, "microphone", "麦克风", [], editable=True)
        self._check(devices, "audio_enabled", "录制麦克风声音")
        self._combo(devices, "audio_backend", "Linux 音频接口", [("PulseAudio / PipeWire", "pulse"), ("ALSA", "alsa")])
        self.fields["audio_backend"].setEnabled(os.name != "nt")
        self._combo(devices, "input_format", "摄像头输入格式", [("MJPEG（常见 USB 2K 摄像头）", "mjpeg"),
                    ("自动选择", "auto"), ("YUY2 / YUYV", "yuyv422"), ("NV12", "nv12")])
        self._spin(devices, "width", "录像宽度", 160, 7680)
        self._spin(devices, "height", "录像高度", 120, 4320)
        self._spin(devices, "fps", "录像帧率", 1, 120)
        self.refresh_button = QPushButton("刷新摄像头与麦克风")
        self.refresh_button.clicked.connect(lambda: self._diagnose("devices"))
        self.modes_button = QPushButton("查看摄像头支持的格式")
        self.modes_button.clicked.connect(lambda: self._diagnose("modes"))
        devices.addRow(self.refresh_button, self.modes_button)
        hint = QLabel("选择设备后先试拍预览。若 2K 无法启动，请查看摄像头支持的输入格式。\n"
                      "Linux 默认通过 PulseAudio 接口使用声音，兼容提供该接口的 PipeWire。")
        hint.setWordWrap(True)
        devices.addRow(hint)

        encoding = self._tab(tabs, "编码与预览")
        self._combo(encoding, "encoder", "视频编码", [("H.265 / HEVC · Intel QSV 硬件", "hevc_qsv"),
                    ("H.264 · Intel QSV 硬件", "h264_qsv"), ("H.264 · CPU 软件", "libx264"),
                    ("H.265 · CPU 软件（负担较高）", "libx265")])
        self._spin(encoding, "quality", "质量值（越低画质越高）", 1, 51)
        self._combo(encoding, "preset", "编码预设", [(v, v) for v in ("veryfast", "faster", "fast", "medium", "slow")])
        self._check(encoding, "preview_enabled", "录像时显示低分辨率预览")
        self._spin(encoding, "preview_fps", "预览帧率", 1, 15)
        self.encoders_button = QPushButton("检查 FFmpeg 包含的编码器")
        self.encoders_button.clicked.connect(lambda: self._diagnose("encoders"))
        encoding.addRow(self.encoders_button)
        encoding.addRow(QLabel("QSV 不可用时会报错，不会自动改用 CPU。\n质量值不保证与 OBS 的同数值等价，请用标签文字核对画质。"))

        storage = self._tab(tabs, "文件与保护")
        self._path(storage, "record_path", "录像目录", directory=True)
        self._spin(storage, "max_record_minutes", "单段最长分钟数", 1, 1440)
        self._spin(storage, "disk_warn_gb", "磁盘警告阈值 GB", 1, 100000)
        self._spin(storage, "disk_critical_gb", "禁止新录像阈值 GB", 1, 100000)
        self._spin(storage, "disk_copy_reserve_gb", "跨盘复制额外预留 GB", 0, 10000)
        storage.addRow(QLabel("录像格式为 MKV，文件名使用单号，重名自动添加序号。\n不添加画面水印。扫码切单期间只保留最新单号。"))

        advanced = self._tab(tabs, "程序与超时")
        self._path(advanced, "ffmpeg_path", "FFmpeg 路径")
        self._path(advanced, "ffprobe_path", "FFprobe 路径")
        for key, label in (("start_timeout_seconds", "启动确认超时秒"),
                           ("stall_timeout_seconds", "输出停滞超时秒"), ("stop_timeout_seconds", "停止保存超时秒")):
            self._spin(advanced, key, label, 3, 120)
        advanced.addRow(QLabel("路径留空时，依次查找程序的 tools 文件夹、程序目录和系统 PATH。"))

        self.diagnostics = QTextEdit()
        self.diagnostics.setReadOnly(True)
        self.diagnostics.setMaximumHeight(150)
        self.diagnostics.setPlaceholderText("设备和格式检查结果")
        layout.addWidget(self.diagnostics)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.buttons = buttons

    def _tab(self, tabs, name):
        widget = QWidget()
        form = QFormLayout(widget)
        tabs.addTab(widget, name)
        return form

    def _combo(self, form, key, label, choices, editable=False):
        widget = QComboBox()
        widget.setEditable(editable)
        for text, value in choices:
            widget.addItem(text, value)
        index = widget.findData(self.settings[key])
        if index < 0:
            widget.addItem(self.settings[key], self.settings[key])
            index = widget.count() - 1
        widget.setCurrentIndex(index)
        form.addRow(label, widget)
        self.fields[key] = widget

    def _spin(self, form, key, label, minimum, maximum):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(self.settings[key])
        form.addRow(label, widget)
        self.fields[key] = widget

    def _check(self, form, key, label):
        widget = QCheckBox(label)
        widget.setChecked(self.settings[key])
        form.addRow(widget)
        self.fields[key] = widget

    def _path(self, form, key, label, directory=False):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        edit = QLineEdit(self.settings[key])
        button = QPushButton("浏览")

        def browse():
            selected = (QFileDialog.getExistingDirectory(self, label, edit.text()) if directory else
                        QFileDialog.getOpenFileName(self, label, edit.text())[0])
            if selected:
                edit.setText(selected)

        button.clicked.connect(browse)
        layout.addWidget(edit)
        layout.addWidget(button)
        form.addRow(label, row)
        self.fields[key] = edit

    def values(self):
        values = dict(self.settings)
        for key, widget in self.fields.items():
            if isinstance(widget, QComboBox):
                # An edited device identifier must not retain the old item's data.
                index = widget.currentIndex()
                values[key] = (widget.itemData(index) if index >= 0 and widget.currentText() == widget.itemText(index)
                               else widget.currentText().strip())
            elif isinstance(widget, QCheckBox):
                values[key] = widget.isChecked()
            elif isinstance(widget, QSpinBox):
                values[key] = widget.value()
            else:
                values[key] = widget.text().strip()
        return values

    def _diagnose(self, kind):
        if self.busy:
            return
        settings = self.values()
        self.busy = True
        for button in (self.refresh_button, self.modes_button, self.encoders_button):
            button.setEnabled(False)
        self.diagnostics.setText("正在检查…")

        def work():
            try:
                action = {"devices": discover_devices, "modes": camera_modes, "encoders": available_encoders}[kind]
                result = action(settings)
                self.diagnostic_done.emit(kind, result)
            except Exception as error:
                self.diagnostic_done.emit("error", str(error))

        threading.Thread(target=work, daemon=True).start()

    def _diagnostic_done(self, kind, result):
        self.busy = False
        for button in (self.refresh_button, self.modes_button, self.encoders_button):
            button.setEnabled(True)
        if kind == "devices":
            for key, group in (("camera", "video"), ("microphone", "audio")):
                widget = self.fields[key]
                selected = self.values()[key]
                widget.clear()
                for name, identifier in result[group]:
                    widget.addItem(name, identifier)
                index = widget.findData(selected)
                if selected and index < 0:
                    widget.addItem(f"此前设备（当前未发现）：{selected}", selected)
                    index = widget.count() - 1
                if index >= 0:
                    widget.setCurrentIndex(index)
            self.diagnostics.setText(f"摄像头 {len(result['video'])} 个，音频输入 {len(result['audio'])} 个。")
        elif kind == "encoders":
            relevant = sorted(set(result) & {"h264_qsv", "hevc_qsv", "libx264", "libx265", "aac"})
            self.diagnostics.setText("可用编码器：" + ", ".join(relevant) +
                                     "\n此检查只确认 FFmpeg 编译支持。QSV 驱动和硬件是否正常，需要实际录制验证。")
        else:
            self.diagnostics.setText(str(result))

    def accept(self):
        if self.busy:
            QMessageBox.information(self, "正在检查", "请等待设备检查完成后保存。")
            return
        super().accept()
