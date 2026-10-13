"""Settings for the standalone recorder; OBS settings are never used."""
import json
import math
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path


def application_dir():
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def runtime_dir():
    """Installed/portable binary packages keep mutable files in the user profile."""
    if not (getattr(sys, "frozen", False) or getattr(sys, "_pack_record_bundle", False)):
        return application_dir()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))) / "PackRecord"
    return Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local" / "share"))) / "pack-record"


def find_tool(name, configured=""):
    if configured:
        resolved = shutil.which(configured)
        if resolved:
            return resolved
        raise FileNotFoundError(f"找不到 {name}：{configured}")
    suffix = ".exe" if os.name == "nt" else ""
    for directory in (application_dir() / "tools", application_dir()):
        candidate = directory / (name + suffix)
        if candidate.is_file():
            return str(candidate)
    resolved = shutil.which(name)
    if not resolved:
        raise FileNotFoundError(f"找不到 {name}，请安装或放入程序目录下的 tools 文件夹")
    return resolved


class RecorderSettingsStore:
    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        # Do not overwrite an existing OBS installation's settings.
        self.path = self.base_dir / "recorder-settings.json"
        self.warnings = []

    def defaults(self):
        return dict(schema_version=1, camera="", microphone="", audio_enabled=True,
                    audio_backend="pulse", input_format="mjpeg", width=2560, height=1440,
                    fps=30, encoder="hevc_qsv", quality=24, preset="fast",
                    ffmpeg_path="", ffprobe_path="", preview_enabled=True, preview_fps=8,
                    record_path=str(self.base_dir / "recordings"), max_record_minutes=20,
                    disk_warn_gb=40, disk_critical_gb=10, disk_copy_reserve_gb=1,
                    duplicate_scan_seconds=1.0, start_timeout_seconds=15,
                    stall_timeout_seconds=10, stop_timeout_seconds=15)

    def validate(self, values):
        if not isinstance(values, dict):
            raise ValueError("设置必须是 JSON 对象")
        result = self.defaults()
        result.update({key: value for key, value in values.items() if key in result})
        for key in ("camera", "microphone", "record_path", "ffmpeg_path", "ffprobe_path"):
            if not isinstance(result[key], str):
                raise ValueError(f"{key} 必须是文本")
            result[key] = result[key].strip()
        if not result["record_path"]:
            raise ValueError("录像目录不能为空")
        path = Path(result["record_path"])
        result["record_path"] = str((path if path.is_absolute() else self.base_dir / path).resolve())
        for key in ("audio_enabled", "preview_enabled"):
            if not isinstance(result[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        choices = dict(encoder=("h264_qsv", "hevc_qsv", "libx264", "libx265"),
                       audio_backend=("pulse", "alsa"),
                       input_format=("auto", "mjpeg", "yuyv422", "nv12"),
                       preset=("veryfast", "faster", "fast", "medium", "slow"))
        for key, allowed in choices.items():
            if result[key] not in allowed:
                raise ValueError(f"不支持的 {key}：{result[key]}")
        bounds = dict(width=(160, 7680), height=(120, 4320), fps=(1, 120), quality=(1, 51),
                      preview_fps=(1, 15), max_record_minutes=(1, 1440), disk_warn_gb=(1, 100000),
                      disk_critical_gb=(1, 100000), disk_copy_reserve_gb=(0, 10000),
                      start_timeout_seconds=(3, 120), stall_timeout_seconds=(3, 120),
                      stop_timeout_seconds=(3, 120))
        for key, (lower, upper) in bounds.items():
            value = result[key]
            if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
                raise ValueError(f"{key} 必须是 {lower}～{upper} 之间的整数")
        if result["width"] % 2 or result["height"] % 2:
            raise ValueError("分辨率宽高必须为偶数")
        window = result["duplicate_scan_seconds"]
        if isinstance(window, bool) or not isinstance(window, (int, float)) or not math.isfinite(window) or not 0 <= window <= 10:
            raise ValueError("重复扫码窗口必须在 0～10 秒之间")
        if result["disk_critical_gb"] >= result["disk_warn_gb"]:
            raise ValueError("禁止录像阈值必须小于磁盘警告阈值")
        return result

    def load(self):
        if not self.path.exists():
            return self.defaults()
        try:
            return self.validate(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError, TypeError) as error:
            backup = self.path.with_name(self.path.name + f".corrupt-{datetime.now():%Y%m%d_%H%M%S_%f}")
            try:
                os.replace(self.path, backup)
                self.warnings.append(f"设置损坏，已备份：{backup}（{error}）")
            except OSError as backup_error:
                self.warnings.append(f"设置损坏，备份失败：{backup_error}")
            return self.defaults()

    def save(self, values):
        result = self.validate(values)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        return result
