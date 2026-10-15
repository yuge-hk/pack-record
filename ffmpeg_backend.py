"""Direct camera capture with bounded preview memory. No Qt or OBS dependency."""
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from recorder_settings import find_tool

PREVIEW_WIDTH, PREVIEW_HEIGHT = 640, 360
FRAME_BYTES = PREVIEW_WIDTH * PREVIEW_HEIGHT * 3


def process_options():
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    if getattr(sys, "frozen", False):
        # PyInstaller prepends bundled Qt/Python libraries to LD_LIBRARY_PATH.
        # System FFmpeg must use the original system library search path.
        environment = os.environ.copy()
        original = environment.get("LD_LIBRARY_PATH_ORIG")
        if original is None:
            environment.pop("LD_LIBRARY_PATH", None)
        else:
            environment["LD_LIBRARY_PATH"] = original
        return {"env": environment}
    return {}


def run_tool(arguments, timeout=15):
    result = subprocess.run(arguments, capture_output=True, timeout=timeout, **process_options())
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace")


def parse_dshow_devices(text):
    devices = {"video": [], "audio": []}
    kind = None
    for line in text.splitlines():
        match = re.search(r'"(.*)"\s+\((video|audio)\)', line)
        if match:
            name, kind = match.groups()
            devices[kind].append((name, name))
        else:
            alternate = re.search(r'Alternative name "(.*)"', line)
            if alternate and kind and devices[kind]:
                name, _ = devices[kind][-1]
                devices[kind][-1] = (name, alternate.group(1))
    return devices


def discover_devices(settings, platform=None):
    platform = platform or ("windows" if os.name == "nt" else "linux")
    ffmpeg = find_tool("ffmpeg", settings["ffmpeg_path"])
    if platform == "windows":
        _, text = run_tool([ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"])
        devices = parse_dshow_devices(text)
        if not devices["video"]:
            raise RuntimeError("未发现 DirectShow 摄像头，请检查设备和 dshow 支持\n" + text[-1500:])
        return devices
    cameras, seen = [], set()
    paths = sorted(Path("/dev/v4l/by-id").glob("*")) + sorted(Path("/dev").glob("video*"))
    for path in paths:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        name_path = Path("/sys/class/video4linux") / resolved.name / "name"
        name = name_path.read_text().strip() if name_path.exists() else resolved.name
        cameras.append((f"{name} ({resolved.name})", str(path)))
    microphones = []
    if settings["audio_backend"] == "pulse":
        try:
            result = subprocess.run(["pactl", "-f", "json", "list", "sources"], capture_output=True, timeout=5,
                                    **process_options())
            if result.returncode == 0:
                microphones = [(source.get("description", source["name"]), source["name"])
                               for source in json.loads(result.stdout)
                               if not source["name"].endswith(".monitor")]
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
        microphones.insert(0, ("系统默认麦克风", "default"))
    else:
        pcm = Path("/proc/asound/pcm")
        if pcm.exists():
            for line in pcm.read_text().splitlines():
                match = re.match(r"(\d+)-(\d+):\s*(.*?)\s*:", line)
                if match and "capture" in line:
                    card, device, label = match.groups()
                    card_id = Path(f"/proc/asound/card{int(card)}/id")
                    identifier = card_id.read_text().strip() if card_id.exists() else str(int(card))
                    microphones.append((label, f"plughw:CARD={identifier},DEV={int(device)}"))
        microphones.insert(0, ("ALSA 默认输入", "default"))
    return {"video": cameras, "audio": microphones}


def camera_modes(settings, platform=None):
    ffmpeg = find_tool("ffmpeg", settings["ffmpeg_path"])
    platform = platform or ("windows" if os.name == "nt" else "linux")
    if not settings["camera"]:
        raise ValueError("请先选择摄像头")
    if platform == "windows":
        args = ["-list_options", "true", "-f", "dshow", "-i", "video=" + settings["camera"]]
    else:
        args = ["-f", "v4l2", "-list_formats", "all", "-i", settings["camera"]]
    _, text = run_tool([ffmpeg, "-hide_banner"] + args)
    return text


def available_encoders(settings):
    code, text = run_tool([find_tool("ffmpeg", settings["ffmpeg_path"]), "-hide_banner", "-encoders"])
    if code:
        raise RuntimeError(text)
    return {match.group(1) for match in re.finditer(r"^\s*[VAS][A-Z.]{5}\s+(\S+)", text, re.M)}


def input_arguments(settings, platform, synthetic=False):
    size, fps = f'{settings["width"]}x{settings["height"]}', str(settings["fps"])
    if synthetic:
        args = ["-re", "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={fps}"]
        if settings["audio_enabled"]:
            args += ["-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
        return args
    if not settings["camera"]:
        raise ValueError("请先在设置中选择 USB 摄像头")
    if settings["audio_enabled"] and not settings["microphone"]:
        raise ValueError("请先选择麦克风，或明确关闭录音")
    args = ["-thread_queue_size", "32"]
    if platform == "windows":
        args += ["-f", "dshow", "-rtbufsize", "128M", "-video_size", size, "-framerate", fps]
        fmt = settings["input_format"]
        if fmt == "mjpeg":
            args += ["-vcodec", "mjpeg"]
        elif fmt != "auto":
            args += ["-pixel_format", fmt]
        source = "video=" + settings["camera"]
        if settings["audio_enabled"]:
            source += ":audio=" + settings["microphone"]
        args += ["-i", source]
    else:
        args += ["-f", "v4l2", "-video_size", size, "-framerate", fps]
        if settings["input_format"] != "auto":
            args += ["-input_format", settings["input_format"]]
        args += ["-use_wallclock_as_timestamps", "1", "-i", settings["camera"]]
        if settings["audio_enabled"]:
            args += ["-thread_queue_size", "128", "-f", settings["audio_backend"],
                     "-use_wallclock_as_timestamps", "1", "-i", settings["microphone"]]
    return args


def build_command(settings, output=None, platform=None, synthetic=False):
    """Return argv, never shell text. The recording output is always first."""
    platform = platform or ("windows" if os.name == "nt" else "linux")
    args = [find_tool("ffmpeg", settings["ffmpeg_path"]), "-hide_banner", "-loglevel", "warning",
            "-nostats", "-stats_period", "0.25", "-progress", "pipe:2", "-n", "-filter_complex_threads", "1"]
    args += input_arguments(settings, platform, synthetic)
    audio_map = "0:a:0" if platform == "windows" and not synthetic else "1:a:0"
    preview = settings["preview_enabled"] or output is None
    if preview:
        video_filter = (f"fps={settings['preview_fps']},scale={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:"
                        "force_original_aspect_ratio=decrease,"
                        f"pad={PREVIEW_WIDTH}:{PREVIEW_HEIGHT}:(ow-iw)/2:(oh-ih)/2,format=rgb24")
        graph = (f"[0:v:0]split=2[record][preview_in];[preview_in]{video_filter}[preview]"
                 if output else f"[0:v:0]{video_filter}[preview]")
        args += ["-filter_complex", graph]
    if output:
        args += ["-map", "[record]" if preview else "0:v:0"]
        if settings["audio_enabled"]:
            args += ["-map", audio_map, "-af", "aresample=async=1000:first_pts=0",
                     "-c:a", "aac", "-b:a", "128k", "-ar", "48000",
                     "-stats_mux_pre:a:0", "pipe:2", "-stats_mux_pre_fmt:a:0", "audio_time={t}",
                     "-shortest", "-shortest_buf_duration", "2"]
        else:
            args += ["-an"]
        encoder = settings["encoder"]
        # Shipping scans stop abruptly. Avoid a long delayed video tail while the
        # independent microphone input has already stopped delivering packets.
        args += ["-c:v", encoder, "-preset", settings["preset"], "-bf", "0"]
        if encoder.startswith("libx"):
            args += ["-tune", "zerolatency"]
        args += [
                 "-pix_fmt", "nv12" if encoder.endswith("_qsv") else "yuv420p",
                 "-global_quality" if encoder.endswith("_qsv") else "-crf", str(settings["quality"]),
                 "-g", str(settings["fps"] * 2), "-r", str(settings["fps"]), "-fps_mode", "cfr",
                 "-stats_mux_pre:v:0", "pipe:2", "-stats_mux_pre_fmt:v:0", "video_time={t}",
                 "-f", "matroska", str(output)]
    if preview:
        args += ["-map", "[preview]", "-an", "-c:v", "rawvideo", "-threads", "1",
                 "-pix_fmt", "rgb24"]
        if output is None:
            args += ["-stats_mux_pre:v:0", "pipe:2", "-stats_mux_pre_fmt:v:0", "video_time={t}"]
        args += ["-f", "rawvideo", "pipe:1"]
    if output is None and settings["audio_enabled"]:
        args += ["-map", audio_map, "-vn", "-stats_mux_pre:a:0", "pipe:2",
                 "-stats_mux_pre_fmt:a:0", "audio_time={t}", "-f", "null", os.devnull]
    return args


def probe_recording(path, settings, *, match_dimensions=True):
    """Require real video/audio packets, correct dimensions/codec and finalized duration."""
    ffprobe = find_tool("ffprobe", settings["ffprobe_path"])
    result = subprocess.run([ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
                            capture_output=True, timeout=30, **process_options())
    if result.returncode:
        raise RuntimeError("录像无法读取：" + result.stderr.decode(errors="replace")[-1000:])
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video or (match_dimensions and (int(video.get("width", 0)) != settings["width"] or int(video.get("height", 0)) != settings["height"])):
        raise RuntimeError("录像没有视频或实际分辨率与设置不一致")
    if settings["audio_enabled"] and not audio:
        raise RuntimeError("录像缺少要求的麦克风音轨")
    expected = "hevc" if settings["encoder"] in ("hevc_qsv", "libx265") else "h264"
    if video.get("codec_name") != expected:
        raise RuntimeError("录像编码格式与设置不一致")
    duration = float(data.get("format", {}).get("duration", 0))
    if not math.isfinite(duration) or duration <= 0:
        raise RuntimeError("录像时长为零或缺少完整结束信息；请人工检查原始文件")
    packets = subprocess.run([ffprobe, "-v", "error", "-read_intervals", "%+2", "-show_packets",
                              "-show_entries", "packet=stream_index", "-of", "json", str(path)],
                             capture_output=True, timeout=30, **process_options())
    if packets.returncode:
        raise RuntimeError("无法验证录像数据包")
    indices = {p["stream_index"] for p in json.loads(packets.stdout).get("packets", [])}
    if video["index"] not in indices or (settings["audio_enabled"] and audio["index"] not in indices):
        raise RuntimeError("录像有音视频轨道但缺少实际数据包")
    return duration


@dataclass
class SessionResult:
    confirmed: bool
    error: str
    returncode: int
    forced: bool = False


class CaptureSession:
    def __init__(self, settings, output=None, synthetic=False):
        self.settings = dict(settings)
        self.output, self.synthetic = output, synthetic
        self._command = None
        self.stop_event = threading.Event()
        self._frame_lock = threading.Lock()
        self._latest_frame, self._frame_serial = None, 0

    def stop(self):
        self.stop_event.set()

    def prepare(self):
        # Validate selections before creating a durable recording task.
        self._command = build_command(self.settings, self.output, synthetic=self.synthetic)

    def latest_frame(self, after_serial=0):
        with self._frame_lock:
            return (after_serial, None) if self._frame_serial == after_serial else (self._frame_serial, self._latest_frame)

    def run(self, on_started=lambda: None, on_progress=lambda _: None):
        command = self._command or build_command(self.settings, self.output, synthetic=self.synthetic)
        process = subprocess.Popen(command,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=0, **process_options())
        errors, reader_errors = deque(maxlen=35), []
        progress_lock = threading.Lock()
        progress = {"frame": 0, "out_time_us": 0}

        def stderr_reader():
            try:
                for line in iter(process.stderr.readline, b""):
                    text = line.decode("utf-8", errors="replace").strip()
                    key, separator, value = text.partition("=")
                    if separator and key in ("frame", "out_time_us", "fps", "speed", "drop_frames", "dup_frames", "progress", "video_time", "audio_time"):
                        with progress_lock:
                            progress[key] = value
                    elif text:
                        errors.append(text)
            except Exception as error:
                reader_errors.append(str(error))

        def stdout_reader():
            try:
                buffer = bytearray()
                while True:
                    chunk = process.stdout.read(65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                    while len(buffer) >= FRAME_BYTES:
                        frame = bytes(buffer[:FRAME_BYTES])
                        del buffer[:FRAME_BYTES]
                        with self._frame_lock:
                            self._latest_frame, self._frame_serial = frame, self._frame_serial + 1
            except Exception as error:
                reader_errors.append(str(error))

        threads = [threading.Thread(target=stderr_reader), threading.Thread(target=stdout_reader)]
        for thread in threads:
            thread.start()
        started_at = time.monotonic()
        last_report = 0
        last_times = {"video_time": -1.0, "audio_time": -1.0}
        last_advances = {key: started_at for key in last_times}
        confirmed, forced, stopping_at, fault = False, False, None, ""
        try:
            while process.poll() is None:
                now = time.monotonic()
                with progress_lock:
                    snapshot = dict(progress)
                required = ["video_time"] + (["audio_time"] if self.settings["audio_enabled"] else [])
                for key in required:
                    try:
                        value = float(snapshot.get(key, -1))
                    except (ValueError, TypeError):
                        value = -1
                    if math.isfinite(value) and value > last_times[key]:
                        last_times[key], last_advances[key] = value, now
                if all(last_times[key] > 0 for key in required) and not confirmed:
                    confirmed = True
                    on_started()
                if stopping_at is None:
                    if reader_errors:
                        fault = "采集管道读取失败：" + reader_errors[0]
                    elif not confirmed and now - started_at > self.settings["start_timeout_seconds"]:
                        fault = "摄像头/麦克风/编码器启动超时，尚未确认录制"
                    elif confirmed and any(now - last_advances[key] > self.settings["stall_timeout_seconds"] for key in required):
                        stalled = ["视频" if key == "video_time" else "麦克风声音" for key in required
                                   if now - last_advances[key] > self.settings["stall_timeout_seconds"]]
                        fault = "/".join(stalled) + "输出停滞，请检查设备及磁盘"
                    if fault or (self.stop_event.is_set() and confirmed):
                        try:
                            process.stdin.write(b"q\n")
                            process.stdin.flush()
                        except OSError:
                            pass
                        stopping_at = now
                elif now - stopping_at > self.settings["stop_timeout_seconds"]:
                    forced, fault = True, fault or "停止超时，已强制结束，录像需要人工检查"
                    process.kill()
                if now - last_report >= 0.5:
                    on_progress(snapshot)
                    last_report = now
                time.sleep(0.03)
            process.wait()
        except BaseException:
            if process.poll() is None:
                process.kill()
            process.wait()
            raise
        finally:
            for thread in threads:
                thread.join(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()
        if not fault and (process.returncode != 0 or stopping_at is None):
            fault = "FFmpeg 异常退出" if confirmed else "采集或编码启动失败"
        if fault:
            fault += "\n" + "\n".join(errors)[-2500:]
        return SessionResult(confirmed, fault.strip(), process.returncode, forced)
