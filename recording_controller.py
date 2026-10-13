"""Serialized scan/record/archive workflow, independent of the GUI."""
import os
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from app_core import (FailedJobStore, VideoDB, ensure_copy_space, move_recording_file,
                      release_filename_reservation, reserve_unique_filename,
                      sanitize_tracking_filename, wait_for_recording_file_ready)
from ffmpeg_backend import CaptureSession, available_encoders, probe_recording
from recorder_settings import find_tool


class RecordingController:
    def __init__(self, settings, base_dir, notify=lambda *_: None, session_factory=CaptureSession):
        self.settings = dict(settings)
        self.base_dir, self.notify, self.session_factory = Path(base_dir), notify, session_factory
        self.jobs = FailedJobStore(self.base_dir / "records" / "recorder-failed-jobs.json")
        self.database = VideoDB(self.base_dir / "records")
        self.condition = threading.Condition(threading.RLock())
        self.state, self.current, self.pending, self.fault_tracking = "idle", None, None, None
        self.error = ""
        self.session = None
        self.preview_requested = self.recovery_requested = self.closing = False
        self.stop_requested = False
        self.last_scan, self.last_scan_time = None, 0
        self.started_monotonic = None
        self.thread = threading.Thread(target=self._worker, name="recording-controller", daemon=False)
        self.thread.start()

    def snapshot(self):
        with self.condition:
            return dict(state=self.state, current=self.current, pending=self.pending,
                        fault_tracking=self.fault_tracking, error=self.error,
                        started_monotonic=self.started_monotonic, closing=self.closing)

    def _publish(self):
        self.notify("state", self.snapshot())

    def _log(self, text):
        self.notify("log", text)

    def scan(self, tracking):
        tracking = tracking.strip()
        if not tracking:
            return False
        with self.condition:
            if self.closing or self.state == "fault" or self.fault_tracking or self.error:
                self._log(f"故障或退出期间忽略扫码：{tracking}；保留单号：{self.fault_tracking or '-'}")
                self.notify("beep", None)
                return False
            if tracking.upper() == "STOPSTOP":
                self.stop()
                return True
            now = time.monotonic()
            if tracking == self.current or (tracking == self.last_scan and now - self.last_scan_time <= self.settings["duplicate_scan_seconds"]):
                self._log(f"忽略重复扫码：{tracking}")
                return False
            self.last_scan, self.last_scan_time = tracking, now
            self.pending = tracking
            self.preview_requested = False
            if self.state not in ("idle", "fault"):
                self.stop_requested = True
            if self.session:
                self.session.stop()
            self.condition.notify_all()
            self._publish()
            return True

    def stop(self):
        with self.condition:
            self.pending = None
            self.preview_requested = False
            self.stop_requested = True
            if self.session:
                self.session.stop()
            self._publish()

    def preview(self):
        with self.condition:
            if self.state != "idle" or self.pending or self.closing:
                raise RuntimeError("请先停止录像再试拍预览")
            self.preview_requested = True
            self.condition.notify_all()

    def configure(self, settings):
        with self.condition:
            if self.state not in ("idle", "fault") or self.session or self.pending:
                raise RuntimeError("请先停止录像或预览，再修改设置")
            self.settings = dict(settings)

    def retry_fault(self):
        with self.condition:
            if self.state != "fault" or self.session or self.closing:
                return
            number = self.fault_tracking
            self.fault_tracking, self.error = None, ""
            self.state, self.pending = "idle", number
            self.last_scan, self.last_scan_time = None, 0
            self.condition.notify_all()
            self._publish()

    def recover(self):
        with self.condition:
            if self.state not in ("idle", "fault") or self.pending or self.session or self.closing:
                raise RuntimeError("请先停止录像或预览，再恢复保存任务")
            self.recovery_requested = True
            self.condition.notify_all()

    def close(self):
        with self.condition:
            self.closing = True
            self.pending = None
            self.preview_requested = False
            if self.session:
                self.session.stop()
            self.condition.notify_all()
            self._publish()

    def latest_frame(self, previous_session, previous_serial):
        with self.condition:
            session = self.session
        if not session:
            return None, 0, None
        serial, frame = session.latest_frame(previous_serial if session is previous_session else 0)
        return session, serial, frame

    def _fault(self, number, error):
        with self.condition:
            self.state, self.fault_tracking, self.error = "fault", number, str(error)
            self.pending, self.preview_requested = None, False
            self._publish()
            self.notify("beep", None)
        self._log(f"故障锁定，保留单号 {number or '-'}：{error}")

    def _worker(self):
        try:
            while True:
                with self.condition:
                    self.condition.wait_for(lambda: self.closing or self.recovery_requested or
                                            (self.state != "fault" and (self.pending or self.preview_requested)))
                    if self.closing:
                        break
                    if self.recovery_requested:
                        self.recovery_requested = False
                        operation, tracking = "recover", None
                    elif self.pending:
                        operation, tracking = "record", self.pending
                        self.pending = None
                        self.current, self.state = tracking, "starting"
                    else:
                        operation, tracking = "preview", None
                        self.preview_requested = False
                        self.state = "preview_starting"
                    settings = dict(self.settings)
                    self.stop_requested = False
                    self._publish()
                try:
                    if operation == "record":
                        self._record(tracking, settings)
                    elif operation == "preview":
                        self._preview(settings)
                    else:
                        self._recover()
                except Exception as error:
                    self._fault(tracking or self.fault_tracking, error)
                finally:
                    with self.condition:
                        self.session, self.current, self.started_monotonic = None, None, None
                        if self.state != "fault":
                            self.state = "idle"
                        self._publish()
        finally:
            with self.condition:
                self.state = "closed"
                self._publish()

    def _check_space(self, settings):
        archive, staging = Path(settings["record_path"]), self.base_dir / "recordings-in-progress"
        archive.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        for name, path in (("归档盘", archive), ("临时录制盘", staging)):
            free = shutil.disk_usage(path).free / 1024**3
            if free < settings["disk_critical_gb"]:
                raise OSError(f"{name}剩余 {free:.1f} GB，低于禁止阈值 {settings['disk_critical_gb']} GB")
        return staging

    def _attach_session(self, settings, output=None):
        session = self.session_factory(settings, output)
        with self.condition:
            self.session = session
            # A scan/STOP may have arrived while preflight or the journal was running.
            if self.closing or self.pending or self.stop_requested:
                session.stop()
        return session

    def _record(self, tracking, settings):
        staging = self._check_space(settings)
        find_tool("ffprobe", settings["ffprobe_path"])
        if settings["encoder"] not in available_encoders(settings):
            raise RuntimeError(f"FFmpeg 不包含 {settings['encoder']}；不会自动切换到软件编码")
        identifier = uuid.uuid4().hex
        source = staging / (identifier + ".mkv")
        job = dict(id=identifier, recording_id=identifier, tracking_number=tracking,
                   source_path=str(source), state="capturing", start_time=None, end_time=None,
                   settings=settings, archive_date=f"{datetime.now():%Y-%m-%d}",
                   interrupted=True, last_error="录制尚未完成")
        session = self._attach_session(settings, source)
        if hasattr(session, "prepare"):
            session.prepare()
        self.jobs.upsert(job)  # Persist before spawning FFmpeg, not after recording.
        last_space_check = 0
        runtime_fault = ""

        def started():
            with self.condition:
                self.started_monotonic = time.monotonic()
                self.state = "recording"
                job["start_time"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
                self.jobs.upsert(job)
                self._publish()
            self._log(f"已确认录制：{tracking} · {settings['encoder']}")

        def progress(data):
            nonlocal last_space_check, runtime_fault
            self.notify("progress", data)
            now = time.monotonic()
            if self.started_monotonic and now - self.started_monotonic >= settings["max_record_minutes"] * 60:
                self._log("达到最长录像时长，正在停止保存")
                session.stop()
            if now - last_space_check >= 3:
                last_space_check = now
                try:
                    self._check_space(settings)
                except OSError as error:
                    runtime_fault = str(error)
                    session.stop()

        try:
            result = session.run(started, progress)
            job["interrupted"] = bool(result.error or runtime_fault)
            job["last_error"] = result.error or runtime_fault
            job["end_time"] = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
            self.jobs.upsert(job)
            if result.error or runtime_fault:
                self._fault(tracking, result.error or runtime_fault)
            else:
                with self.condition:
                    self.state = "saving"
                    self._publish()
            self._archive(job)
        except Exception as error:
            job["last_error"] = str(error)
            try:
                self.jobs.upsert(job)
            except Exception as store_error:
                self._log(f"恢复任务更新也失败：{store_error}；原始文件：{source}")
            raise

    def _archive(self, job):
        settings = dict(job["settings"])
        # Tool paths can be repaired without rewriting the capture metadata.
        settings["ffprobe_path"] = self.settings["ffprobe_path"]
        source = job["source_path"]
        planned = job.get("planned_destination")
        if source != planned and planned and os.path.isfile(source) and os.path.isfile(planned):
            # Cross-volume crash after atomic destination rename but before unlink.
            if os.path.getsize(source) != os.path.getsize(planned):
                raise RuntimeError("原始文件与已规划目标均存在且内容不一致，请人工核查")
            with open(source, "rb") as left, open(planned, "rb") as right:
                while True:
                    chunk = left.read(1024 * 1024)
                    if chunk != right.read(1024 * 1024):
                        raise RuntimeError("原始文件与已规划目标均存在且内容不一致，请人工核查")
                    if not chunk:
                        break
            probe_recording(planned, settings)
            os.unlink(source)
            source = planned
        if not os.path.isfile(source) and planned and os.path.isfile(planned):
            source = planned  # Crash after move, before journal update.
        wait_for_recording_file_ready(source, stable_checks=0)
        duration = probe_recording(source, settings)
        job["duration"] = duration
        job["end_time"] = job.get("end_time") or f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        reservation = job.get("reservation_path")
        try:
            if source != planned:
                directory = Path(settings["record_path"]) / job["archive_date"]
                directory.mkdir(parents=True, exist_ok=True)
                release_filename_reservation(reservation)
                reservation = None
                destination, reservation = reserve_unique_filename(directory, sanitize_tracking_filename(job["tracking_number"]) + ".mkv")
                job.update(planned_destination=destination, reservation_path=reservation, state="moving")
                self.jobs.upsert(job)
                ensure_copy_space(source, directory, settings["disk_copy_reserve_gb"])
                move_recording_file(source, destination)
                source = destination
            job.update(source_path=source, state="recording_metadata")
            self.jobs.upsert(job)
            self.database.insert_record(job["tracking_number"], job["start_time"], job["end_time"],
                                        duration, source, os.path.getsize(source),
                                        f"{settings['width']}x{settings['height']}", settings["fps"],
                                        recording_id=job["recording_id"],
                                        metadata=dict(backend="ffmpeg", encoder=settings["encoder"],
                                                      quality=settings["quality"], preset=settings["preset"],
                                                      camera=settings["camera"], input_format=settings["input_format"],
                                                      microphone=settings["microphone"] if settings["audio_enabled"] else None,
                                                      audio_enabled=settings["audio_enabled"],
                                                      interrupted=job["interrupted"],
                                                      error=job.get("last_error", "")))
            self.jobs.remove(job["id"])
            self._log(f"{'故障片段' if job['interrupted'] else '录像'}已保存：{source}（{duration:.1f} 秒）")
            self.notify("saved", source)
        finally:
            release_filename_reservation(reservation)

    def _preview(self, settings):
        session = self._attach_session(settings)

        def started():
            with self.condition:
                self.state = "preview"
                self._publish()

        try:
            result = session.run(started, lambda data: self.notify("progress", data))
        except Exception as error:
            self._log("试拍预览失败：" + str(error))
            self.notify("preview_error", str(error))
            return
        if result.error:
            self._log("试拍预览失败：" + result.error)
            self.notify("preview_error", result.error)

    def _recover(self):
        old_state = self.state
        with self.condition:
            self.state = "recovering"
            self._publish()
        successes = 0
        try:
            for job in self.jobs.list():
                if self.closing:
                    break
                try:
                    self._archive(job)
                    successes += 1
                except Exception as error:
                    job["last_error"] = str(error)
                    self.jobs.upsert(job)
                    self._log(f"恢复失败 {job.get('tracking_number', '-')}：{error}")
            self._log(f"恢复完成：成功 {successes} 个，剩余 {self.jobs.count()} 个")
        finally:
            with self.condition:
                self.state = old_state
                self._publish()
