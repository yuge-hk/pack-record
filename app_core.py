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
                      video_path, file_size, resolution, fps):
        record = {
            "schema_version": 1,
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
            with path.open("a", encoding="utf-8", newline="\n") as output:
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
                output.flush()
                os.fsync(output.fileno())

    def close(self):
        pass


class FailedJobStore:
    """原子保存未能整理的 OBS 输出，供界面稍后重试。"""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()

    def _load_unlocked(self):
        if not self.path.exists():
            return []
        try:
            jobs = json.loads(self.path.read_text(encoding="utf-8"))
            return jobs if isinstance(jobs, list) else []
        except (OSError, json.JSONDecodeError):
            return []

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


def ensure_copy_space(source, destination_directory, reserve_gb=1):
    """确保目标盘能容纳源文件，并保留额外安全空间。"""
    source_size = os.path.getsize(source)
    free_bytes = shutil.disk_usage(destination_directory).free
    required_bytes = source_size + reserve_gb * 1024**3
    if free_bytes < required_bytes:
        raise OSError(
            f"目标磁盘空间不足：需要约 {required_bytes / 1024**3:.1f} GB，"
            f"剩余 {free_bytes / 1024**3:.1f} GB"
        )


def move_recording_file(source, destination, retries=10, retry_delay=0.5):
    """移动 OBS 输出；同盘原子重命名，跨磁盘安全复制后删除源文件。"""
    last_error = None
    partial_destination = f"{destination}.part"
    for _ in range(retries):
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
