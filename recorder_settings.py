"""Settings for the standalone recorder; OBS settings are never used."""
import json
import math
import os
import shutil
import sys
import uuid
import re
from urllib.parse import urlsplit
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
        self.base_dir.mkdir(parents=True, exist_ok=True)
        identity = self.base_dir / '.station-id'
        try:
            fd = os.open(identity, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w') as stream:
                stream.write(uuid.uuid4().hex)
        self.station_id = identity.read_text().strip()

    def defaults(self):
        return dict(schema_version=2, station_id=self.station_id, station_name="打包工位",
                    role="integrated", setup_complete=False, web_bind="127.0.0.1",
                    record_paths=[], cleanup_policy="disabled", retention_days=30,
                    upload_kind="none", upload_path="", upload_url="", upload_token="",
                    speech_enabled=True, speech_quantity=True, speech_remarks=True,
                    speech_refund=True, order_valid_hours=24, camera="", microphone="", audio_enabled=True,
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
        for key in ("camera", "microphone", "record_path", "ffmpeg_path", "ffprobe_path",
                    "station_id", "station_name", "web_bind", "upload_path", "upload_url", "upload_token"):
            if not isinstance(result[key], str):
                raise ValueError(f"{key} 必须是文本")
            result[key] = result[key].strip()
        if not result["record_path"]:
            raise ValueError("录像目录不能为空")
        path = Path(result["record_path"])
        result["record_path"] = str((path if path.is_absolute() else self.base_dir / path).resolve())
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', result['station_id']):
            raise ValueError('工位 ID 格式不正确')
        if not result['station_name'] or len(result['station_name']) > 80:
            raise ValueError('工位名称必须为 1～80 个字符')
        if result['web_bind'] not in ('127.0.0.1', '0.0.0.0'):
            raise ValueError('Web 监听地址必须为 127.0.0.1 或 0.0.0.0')
        if not isinstance(result['record_paths'], list) or len(result['record_paths']) > 16:
            raise ValueError('附加录像目录必须为列表，最多 16 个')
        paths = []
        for item in result['record_paths']:
            if not isinstance(item, str) or not item.strip():
                raise ValueError('附加录像目录不能为空')
            path = Path(item.strip())
            path = (path if path.is_absolute() else self.base_dir / path).resolve()
            if str(path) not in paths and str(path) != result['record_path']:
                paths.append(str(path))
        result['record_paths'] = paths
        if result['upload_kind'] == 'directory' and not result['upload_path']:
            raise ValueError('请指定上传目标目录（可以是已挂载的 NAS）')
        if result['upload_path']:
            result['upload_path'] = str(Path(result['upload_path']).expanduser().resolve())
        if result['upload_kind'] == 'center':
            url = urlsplit(result['upload_url'])
            if url.scheme not in ('http', 'https') or not url.hostname or url.username or url.password or url.query or url.fragment:
                raise ValueError('中心地址必须是 HTTP/HTTPS 地址，不能包含凭据或查询参数')
            if not result['upload_token']:
                raise ValueError('请填写中心分配的工位上传密钥')
            result['upload_url'] = result['upload_url'].rstrip('/')
        for key in ("audio_enabled", "preview_enabled", "setup_complete", "speech_enabled",
                    "speech_quantity", "speech_remarks", "speech_refund"):
            if not isinstance(result[key], bool):
                raise ValueError(f"{key} 必须是布尔值")
        choices = dict(role=('integrated', 'station', 'center', 'viewer'),
                       cleanup_policy=('disabled', 'verified', 'local'),
                       upload_kind=('none', 'directory', 'center'), encoder=("h264_qsv", "hevc_qsv", "libx264", "libx265"),
                       audio_backend=("pulse", "alsa"),
                       input_format=("auto", "mjpeg", "yuyv422", "nv12"),
                       preset=("veryfast", "faster", "fast", "medium", "slow"))
        for key, allowed in choices.items():
            if result[key] not in allowed:
                raise ValueError(f"不支持的 {key}：{result[key]}")
        bounds = dict(retention_days=(1, 3650), order_valid_hours=(1, 168), width=(160, 7680), height=(120, 4320), fps=(1, 120), quality=(1, 51),
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
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        if os.name != 'nt':
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(result, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, self.path)
        return result
