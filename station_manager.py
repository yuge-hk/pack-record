"""Application services shared by the Qt workbench and local/LAN HTTP API."""
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import threading
import time
import uuid

from ffmpeg_backend import CaptureSession, process_options, probe_recording
from order_alerts import OrderAlerts
from recorder_settings import find_tool
from recording_library import utc_now
from transfer_manager import TransferManager


class StationManager:
    def __init__(self, controller):
        self.controller, self.library, self.storage = controller, controller.library, controller.storage
        self.base_dir = controller.base_dir
        self.orders = OrderAlerts(self.library, lambda: dict(controller.settings), lambda *event: controller.notify(*event))
        self.transfers = TransferManager(self.library, self.storage, lambda: dict(controller.settings), self.base_dir)
        self.stopping = threading.Event()
        self.wake = threading.Event()
        self.last_error = ''
        self.testing = None
        self.test_results = {}
        self.test_lock = threading.Lock()
        self.media_lock = threading.RLock()
        self.tickets = {}
        self.worker = threading.Thread(target=self._worker, name='station-maintenance', daemon=False)
        self.worker.start()
        self.export_worker = threading.Thread(target=self._export_worker, name='video-exports', daemon=False)
        self.export_worker.start()

    def search(self, **kwargs):
        return self.library.search(**kwargs)

    def status(self):
        with self.library.connect() as db:
            exports = [dict(row) for row in db.execute('SELECT * FROM exports ORDER BY created_at DESC LIMIT 100')]
            audit = [dict(row) for row in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 100')]
            devices = [dict(row) for row in db.execute('SELECT id,name,enabled,created_at FROM devices ORDER BY name')]
        return dict(volumes=self.storage.status(self.controller.settings), transfers=self.library.transfers(),
                    exports=exports, devices=devices, audit=audit, error=self.last_error,
                    role=self.controller.settings['role'], station_id=self.controller.settings['station_id'])

    def queue_uploads(self):
        result = self.transfers.queue_all()
        self.wake.set()
        return result

    def retry_uploads(self):
        self.library.retry_transfers()
        self.wake.set()
        return True

    def save_orders(self, orders):
        return self.orders.upsert(orders)

    def test_result(self, identifier):
        return self.test_results.get(identifier, dict(state='error', error='试录任务不存在'))

    def on_saved(self, record_id):
        self.library.enqueue(record_id, self.transfers.target())
        self.wake.set()

    def create_export(self, record_id, start=0, end=None):
        record = self.library.record(record_id)
        end = record['duration'] if end is None else float(end)
        start = float(start)
        if not math.isfinite(start) or not math.isfinite(end) or not 0 <= start < end <= record['duration'] + .001:
            raise ValueError('剪辑范围必须位于录像时长内，结束时间需晚于开始时间')
        identifier = uuid.uuid4().hex
        output = self.base_dir / 'exports' / (identifier + '.mp4')
        with self.library.lock:
            with self.library.borrow(record_id):
                with self.library.connect() as db:
                    if db.execute("SELECT count(*) FROM exports WHERE state IN ('queued','running')").fetchone()[0] >= 20:
                        raise ValueError('待导出任务已达 20 个，请等待完成后重试')
                    db.execute('INSERT INTO exports(id,record_id,start,end,state,path,created_at) VALUES(?,?,?,?,?,?,?)',
                               (identifier, record_id, start, end, 'queued', str(output), utc_now()))
        self.wake.set()
        return dict(id=identifier, state='queued')

    def export(self, identifier):
        with self.library.connect() as db:
            row = db.execute('SELECT * FROM exports WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise FileNotFoundError('导出任务不存在')
        return dict(row)

    def _export_one(self):
        with self.library.connect() as db:
            row = db.execute("SELECT * FROM exports WHERE state='queued' ORDER BY created_at LIMIT 1").fetchone()
            if not row:
                return False
            job = dict(row)
            db.execute("UPDATE exports SET state='running' WHERE id=?", (job['id'],))
        destination = Path(job['path'])
        partial = destination.with_suffix('.part.mp4')
        try:
            with self.library.borrow(job['record_id']) as record:
                destination.parent.mkdir(exist_ok=True)
                reserve = self.controller.settings['disk_critical_gb'] * 1024**3
                if shutil.disk_usage(destination.parent).free < reserve + record['size']:
                    raise OSError('导出盘空间不足')
                command = [find_tool('ffmpeg', self.controller.settings['ffmpeg_path']), '-hide_banner', '-loglevel', 'error',
                           '-n', '-ss', str(job['start']), '-i', record['path'], '-t', str(job['end'] - job['start']),
                           '-map', '0:v:0', '-map', '0:a:0?', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                           '-threads', '1', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-movflags', '+faststart', str(partial)]
                partial.unlink(missing_ok=True)
                with subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **process_options()) as process:
                    while True:
                        try:
                            _, errors = process.communicate(timeout=.5)
                            break
                        except subprocess.TimeoutExpired:
                            if self.stopping.is_set():
                                process.kill()
                                process.communicate()
                                raise RuntimeError('服务关闭，导出任务可重新创建')
                    if process.returncode:
                        raise RuntimeError('剪辑失败：' + errors.decode(errors='replace')[-1000:])
                probe_recording(partial, dict(self.controller.settings, encoder='libx264',
                                audio_enabled=bool(record['capture'].get('audio_enabled', False))), match_dimensions=False)
                os.replace(partial, destination)
                with self.library.connect() as db:
                    db.execute("UPDATE exports SET state='ready',error='' WHERE id=?", (job['id'],))
                self.library.audit('export', record['id'], f"{job['start']}..{job['end']} -> {destination.name}")
        except Exception as error:
            with self.library.connect() as db:
                db.execute("UPDATE exports SET state='error',error=? WHERE id=?", (str(error), job['id']))
        finally:
            partial.unlink(missing_ok=True)
        return True

    def cleanup(self):
        return self.storage.cleanup(self.controller.settings, pressure=True)

    def issue_device(self, station_id, name):
        if (not isinstance(station_id, str) or not 1 <= len(station_id) <= 64 or
                not all(c.isalnum() or c in '_-' for c in station_id) or not isinstance(name, str) or not 1 <= len(name) <= 80):
            raise ValueError('请填写有效的工位 ID 和名称')
        token = secrets.token_urlsafe(32)
        with self.library.connect() as db:
            db.execute('INSERT OR REPLACE INTO devices VALUES(?,?,?,?,?)',
                       (station_id, name, hashlib.sha256(token.encode()).hexdigest(), 1, utc_now()))
        self.library.audit('device_credential', station_id, 'issued/rotated')
        return dict(station_id=station_id, token=token)

    def identify_device(self, token):
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.library.connect() as db:
            row = db.execute('SELECT id FROM devices WHERE token_hash=? AND enabled=1', (digest,)).fetchone()
        return row[0] if row else None

    def revoke_device(self, station_id):
        with self.library.connect() as db:
            db.execute('UPDATE devices SET enabled=0 WHERE id=?', (station_id,))
        return True

    def media_ticket(self, record_id=None, export_id=None, test_id=None):
        lease = None
        if record_id:
            lease = self.library.borrow(record_id)
            record = lease.__enter__()
            path = record['path']
        elif export_id:
            job = self.export(export_id)
            if job['state'] != 'ready':
                raise ValueError('导出尚未完成')
            path = job['path']
        elif test_id:
            result = self.test_results.get(test_id, {})
            if result.get('state') != 'ready':
                raise ValueError('试录尚未完成')
            path = result['path']
        else:
            raise ValueError('请选择录像或导出文件')
        ticket = secrets.token_urlsafe(32)
        with self.media_lock:
            self._expire_tickets()
            if len(self.tickets) >= 256:
                if lease:
                    lease.__exit__(None, None, None)
                raise RuntimeError('播放链接过多，请关闭旧页面并稍后重试')
            self.tickets[ticket] = dict(path=path, expires=time.monotonic() + 3600, lease=lease)
        return dict(ticket=ticket, path=path, expires_in=3600)

    def release_media(self, ticket):
        with self.media_lock:
            value = self.tickets.pop(ticket.get('ticket', ''), None)
            if value and value['lease']:
                value['lease'].__exit__(None, None, None)
        return True

    def _expire_tickets(self):
        with self.media_lock:
            for key, value in list(self.tickets.items()):
                if value['expires'] < time.monotonic():
                    if value['lease']:
                        value['lease'].__exit__(None, None, None)
                    del self.tickets[key]

    def resolve_ticket(self, ticket):
        with self.media_lock:
            self._expire_tickets()
            if ticket not in self.tickets:
                raise PermissionError('播放链接已过期，请重新打开')
            return self.tickets[ticket]['path']

    def diagnostic(self):
        with self.controller.condition:
            if self.testing or self.controller.state != 'idle' or self.controller.pending:
                raise RuntimeError('请先停止录像或试拍，再进行向导试录')
            identifier = uuid.uuid4().hex
            settings = dict(self.controller.settings)
            if settings['role'] not in ('integrated', 'station'):
                raise ValueError('当前角色无需摄像头试录')
            if shutil.disk_usage(self.base_dir).free < settings['disk_critical_gb'] * 1024**3:
                raise OSError('试录盘剩余空间低于禁止录制水位')
            self.controller.state = 'testing'
            self.controller._publish()
            self.test_results[identifier] = dict(id=identifier, state='running')
            self.testing = threading.Thread(target=self._diagnostic, args=(identifier, settings), daemon=False)
            self.testing.start()
        return dict(id=identifier, state='running')

    def _diagnostic(self, identifier, settings):
        directory = self.base_dir / 'setup-tests'
        directory.mkdir(exist_ok=True)
        output = directory / (identifier + '.mkv')
        timer = None
        try:
            session = self.controller.session_factory(settings, output)
            with self.controller.condition:
                self.controller.session = session
                if self.controller.closing:
                    session.stop()
            def started():
                nonlocal timer
                timer = threading.Timer(3, session.stop)
                timer.start()
            result = session.run(started, lambda _: None)
            if result.error or not result.confirmed:
                raise RuntimeError(result.error or '尚未确认音视频输出')
            duration = probe_recording(output, settings)
            fingerprint = self.settings_fingerprint(settings)
            self.test_results[identifier] = dict(id=identifier, state='ready', path=str(output), duration=duration,
                                                fingerprint=fingerprint, audio=settings['audio_enabled'])
        except Exception as error:
            self.test_results[identifier] = dict(id=identifier, state='error', error=str(error))
        finally:
            if timer:
                timer.cancel()
                timer.join()
            with self.controller.condition:
                self.controller.session = None
                self.controller.state = 'idle'
                self.controller._publish()
                self.controller.condition.notify_all()
            self.testing = None

    @staticmethod
    def settings_fingerprint(settings):
        keys = ('camera', 'microphone', 'audio_enabled', 'audio_backend', 'input_format', 'width', 'height',
                'fps', 'encoder', 'quality', 'preset', 'ffmpeg_path', 'ffprobe_path')
        return hashlib.sha256(json.dumps({key: settings[key] for key in keys}, sort_keys=True).encode()).hexdigest()

    def complete_setup(self, test_id=None):
        if self.controller.settings['role'] in ('integrated', 'station'):
            result = self.test_results.get(test_id, {})
            if result.get('state') != 'ready' or result['fingerprint'] != self.settings_fingerprint(self.controller.settings):
                raise ValueError('请使用当前设备设置完成试录并检查回放')
        if self.controller.settings['role'] != 'viewer':
            self.storage.select(self.controller.settings)
        values = dict(self.controller.settings, setup_complete=True)
        self.controller.configure(values)
        from recorder_settings import RecorderSettingsStore
        RecorderSettingsStore(self.base_dir).save(values)
        return True

    def _worker(self):
        next_cleanup = time.monotonic() + 30
        while not self.stopping.is_set():
            worked = False
            try:
                worked = self.transfers.run_one()
                self._expire_tickets()
                if time.monotonic() >= next_cleanup:
                    self.cleanup()
                    next_cleanup = time.monotonic() + 30
                self.last_error = ''
            except Exception as error:
                self.last_error = str(error)
            self.wake.wait(.2 if worked else 2)
            self.wake.clear()

    def _export_worker(self):
        while not self.stopping.is_set():
            try:
                worked = self._export_one()
            except Exception as error:
                self.last_error = str(error)
                worked = False
            self.stopping.wait(.1 if worked else 1)

    def close(self):
        self.stopping.set()
        self.transfers.stop_event.set()
        self.wake.set()
        self.worker.join()
        self.export_worker.join()
        if self.testing:
            self.testing.join()
        with self.media_lock:
            for value in self.tickets.values():
                if value['lease']:
                    value['lease'].__exit__(None, None, None)
            self.tickets.clear()
