# -*- coding: utf-8 -*-
"""与界面和 OBS 解耦的存储、文件处理基础逻辑。"""

import errno
import json
import os
import re
import shutil
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path


class VideoDB:
    """以 JSONL 按日追加录制记录，避免记录量增长导致整文件重写。"""

    def __init__(self, json_dir):
        self.json_dir = Path(json_dir)
        self.json_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _get_json_path(self):
        return self.json_dir / f"{time.strftime('%Y-%m-%d')}.jsonl"

    def insert_record(self, barcode, start_time, end_time, duration,
                      video_path, file_size, resolution, fps, recording_id=None):
        record = {
            "schema_version": 2,
            "recording_id": recording_id,
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
            if recording_id:
                for existing_path in self.json_dir.glob("*.jsonl"):
                    with existing_path.open("r", encoding="utf-8") as existing:
                        for line in existing:
                            try:
                                if json.loads(line).get("recording_id") == recording_id:
                                    return False
                            except json.JSONDecodeError:
                                continue
            with path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())
            return True

    def close(self):
        pass


class FailedJobStore:
    """原子保存未能整理的 OBS 输出，供界面稍后重试。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.warnings = []

    def _load_unlocked(self):
        if not self.path.exists():
            return []
        try:
            jobs = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(jobs, list):
                raise ValueError("恢复任务根节点必须是 JSON 数组")
            return jobs
        except (json.JSONDecodeError, ValueError) as exc:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            backup_path = self.path.with_name(
                f"{self.path.name}.corrupt-{timestamp}"
            )
            try:
                os.replace(self.path, backup_path)
                self.warnings.append(
                    f"恢复任务文件损坏，已备份到 {backup_path}：{exc}"
                )
            except OSError as backup_error:
                self.warnings.append(
                    f"恢复任务文件损坏且无法备份：{backup_error}"
                )
                raise
            return []
        except OSError:
            raise

    def _save_unlocked(self, jobs):
        temporary_path = self.path.with_suffix(".json.tmp")
        with temporary_path.open("w", encoding="utf-8", newline="\n") as output:
            json.dump(jobs, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, self.path)

    def add(self, job):
        with self.lock:
            jobs = self._load_unlocked()
            job = dict(job)
            job.setdefault("id", uuid.uuid4().hex)
            job.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
            jobs.append(job)
            self._save_unlocked(jobs)
            return job["id"]

    def upsert(self, job):
        with self.lock:
            jobs = self._load_unlocked()
            job = dict(job)
            job.setdefault("id", uuid.uuid4().hex)
            job.setdefault("created_at", datetime.now().isoformat(timespec="seconds"))
            for index, existing in enumerate(jobs):
                if existing.get("id") == job["id"]:
                    jobs[index] = job
                    break
            else:
                jobs.append(job)
            self._save_unlocked(jobs)
            return job["id"]

    def remove(self, job_id):
        with self.lock:
            jobs = [
                job for job in self._load_unlocked()
                if job.get("id") != job_id
            ]
            self._save_unlocked(jobs)

    def consume_warnings(self):
        with self.lock:
            warnings = list(self.warnings)
            self.warnings.clear()
            return warnings

    def list(self):
        with self.lock:
            return list(self._load_unlocked())

    def replace(self, jobs):
        with self.lock:
            self._save_unlocked(list(jobs))

    def finish_retry(self, processed_ids, remaining_jobs):
        """提交一次重试结果，同时保留重试期间新加入的任务。"""
        with self.lock:
            untouched_jobs = [
                job for job in self._load_unlocked()
                if job.get("id") not in processed_ids
            ]
            self._save_unlocked(untouched_jobs + list(remaining_jobs))

    def count(self):
        with self.lock:
            return len(self._load_unlocked())


def unique_filename(base_dir, filename):
    """在 base_dir 下生成不重复的文件名。"""
    name, extension = os.path.splitext(filename)
    counter = 1
    new_name = filename
    while os.path.exists(os.path.join(base_dir, new_name)):
        new_name = f"{name}({counter}){extension}"
        counter += 1
    return os.path.join(base_dir, new_name)


def reserve_unique_filename(base_dir, filename):
    """以独占保留文件消除并发命名的检查/使用竞态。"""
    name, extension = os.path.splitext(filename)
    counter = 0
    while True:
        suffix = "" if counter == 0 else f"({counter})"
        destination = os.path.join(base_dir, f"{name}{suffix}{extension}")
        reservation = f"{destination}.reserve"
        try:
            descriptor = os.open(
                reservation,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            os.close(descriptor)
        except FileExistsError:
            counter += 1
            continue
        if os.path.exists(destination):
            os.unlink(reservation)
            counter += 1
            continue
        return destination, reservation


def release_filename_reservation(reservation):
    if reservation and os.path.exists(reservation):
        os.unlink(reservation)


def sanitize_tracking_filename(tracking_number, max_length=100):
    """生成 Windows 安全文件名；原始单号仍保存在 JSONL。"""
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", str(tracking_number))
    value = re.sub(r"\s+", " ", value).strip(" ._")
    value = value[:max_length].rstrip(" ._")
    if not value:
        return f"UNKNOWN_{datetime.now():%Y%m%d_%H%M%S}"
    reserved_names = {"CON", "PRN", "AUX", "NUL"}
    reserved_names.update(f"COM{index}" for index in range(1, 10))
    reserved_names.update(f"LPT{index}" for index in range(1, 10))
    if value.split(".", 1)[0].upper() in reserved_names:
        value = f"_{value}"
    return value


def same_filesystem(source, destination_directory):
    """判断源文件与目标目录是否位于同一文件系统/Windows 卷。"""
    source = os.path.abspath(source)
    destination_directory = os.path.abspath(destination_directory)
    if os.name == "nt":
        return os.path.splitdrive(source)[0].casefold() == os.path.splitdrive(
            destination_directory
        )[0].casefold()
    return os.stat(source).st_dev == os.stat(destination_directory).st_dev


def ensure_copy_space(source, destination_directory, reserve_gb=1):
    """确保目标盘能容纳源文件，并保留额外安全空间。"""
    if same_filesystem(source, destination_directory):
        return
    source_size = os.path.getsize(source)
    free_bytes = shutil.disk_usage(destination_directory).free
    required_bytes = source_size + reserve_gb * 1024**3
    if free_bytes < required_bytes:
        raise OSError(
            f"目标磁盘空间不足：需要约 {required_bytes / 1024**3:.1f} GB，"
            f"剩余 {free_bytes / 1024**3:.1f} GB"
        )


def video_settings_metadata(settings):
    """兼容 obsws-python 的 snake_case 和旧式 camelCase 字段。"""
    base_width = getattr(settings, "base_width", None)
    base_height = getattr(settings, "base_height", None)
    numerator = getattr(
        settings,
        "fps_numerator",
        getattr(settings, "fpsNumerator", 0),
    )
    denominator = getattr(
        settings,
        "fps_denominator",
        getattr(settings, "fpsDenominator", 0),
    )
    resolution = (
        f"{base_width}x{base_height}"
        if base_width is not None and base_height is not None
        else "-"
    )
    fps = numerator / denominator if denominator else 0
    if isinstance(fps, float) and fps.is_integer():
        fps = int(fps)
    return resolution, fps


def wait_for_recording_file_ready(
    path,
    minimum_size=1024,
    timeout=5,
    interval=0.25,
    stable_checks=2,
):
    """等待录像文件达到最小大小且停止增长，避免归档空文件或未刷盘文件。"""
    deadline = time.monotonic() + timeout
    previous_size = None
    stable_count = 0
    last_size = None
    while True:
        try:
            last_size = os.path.getsize(path)
        except OSError:
            last_size = None
        if last_size is not None and last_size >= minimum_size:
            if stable_checks <= 0:
                return last_size
            if last_size == previous_size:
                stable_count += 1
                if stable_count >= stable_checks:
                    return last_size
            else:
                stable_count = 0
            previous_size = last_size
        else:
            stable_count = 0
            previous_size = last_size
        if time.monotonic() >= deadline:
            if last_size is None:
                raise FileNotFoundError(f"录像文件不存在：{path}")
            raise RuntimeError(
                f"录像文件过小或仍未写入完成：{path}（{last_size} 字节）"
            )
        time.sleep(interval)


def move_recording_file(source, destination, retries=10, retry_delay=0.5):
    """移动 OBS 输出；同盘原子重命名，跨磁盘安全复制后删除源文件。"""
    last_error = None
    partial_destination = f"{destination}.{uuid.uuid4().hex}.part"
    copied_by_this_call = False
    for _ in range(retries):
        if copied_by_this_call and os.path.isfile(source) and os.path.isfile(destination):
            try:
                if os.path.getsize(source) == os.path.getsize(destination):
                    os.unlink(source)
                    return destination
            except OSError as exc:
                last_error = exc
                time.sleep(retry_delay)
                continue
        try:
            if os.path.exists(destination):
                raise FileExistsError(f"目标文件已存在：{destination}")
            os.rename(source, destination)
            return destination
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                last_error = exc
                time.sleep(retry_delay)
                continue
        try:
            with open(source, "rb") as input_file, open(partial_destination, "xb") as output_file:
                shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
                output_file.flush()
                os.fsync(output_file.fileno())
            shutil.copystat(source, partial_destination)
            if os.path.exists(destination):
                raise FileExistsError(f"目标文件已存在：{destination}")
            os.rename(partial_destination, destination)
            copied_by_this_call = True
            os.unlink(source)
            return destination
        except OSError as exc:
            last_error = exc
            try:
                if os.path.exists(partial_destination):
                    os.unlink(partial_destination)
            except OSError:
                pass
            time.sleep(retry_delay)
    try:
        if os.path.exists(partial_destination):
            os.unlink(partial_destination)
    except OSError:
        pass
    raise RuntimeError(f"无法移动录制文件: {last_error}") from last_error
