"""Evidence retention, resumable transfers, durable sync and HTTP role boundaries."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from recorder_settings import RecorderSettingsStore
from recording_controller import RecordingController
from recording_library import RecordingLibrary, file_sha256
from storage_manager import StorageManager
from transfer_manager import TransferManager
from order_alerts import OrderAlerts
from recorder_service import RecorderService, RecorderHTTPServer


@pytest.fixture
def env(tmp_path):
    settings = RecorderSettingsStore(tmp_path).defaults()
    settings.update(disk_critical_gb=1, disk_warn_gb=2, disk_copy_reserve_gb=0,
                    encoder='libx264', width=320, height=180, microphone='default')
    library = RecordingLibrary(tmp_path / 'records')
    storage = StorageManager(library, tmp_path)
    Path(storage.select(settings)).mkdir(exist_ok=True)
    return settings, library, storage


def add_record(env, identifier='record-1', content=b'video evidence', **updates):
    settings, library, _ = env
    path = Path(settings['record_path']) / (identifier + '.mkv')
    path.write_bytes(content)
    row = dict(id=identifier, barcode='单号_100%', start_time='2020-01-01 12:00:00',
               end_time='2020-01-01 12:00:05', duration=5, size=len(content), path=str(path),
               station_id=settings['station_id'], station_name='工位 A', capture={'interrupted': True})
    row.update(updates)
    library.add(row)
    return library.record(identifier)


def verified(library, identifier):
    with library.connect() as db:
        db.execute("UPDATE recordings SET upload_state='verified' WHERE id=?", (identifier,))


def test_library_identity_search_sync_tombstone_survive_restart(env):
    settings, lib, storage = env
    first = add_record(env)
    add_record(env, 'record-2')  # Same barcode is a separate recording.
    assert lib.search(search='_100%')['total'] == 2
    assert lib.search(search='_missing')['total'] == 0
    initial = lib.changes(limit=1)
    cursor = initial['cursor']
    assert initial['has_more']
    verified(lib, first['id'])
    assert lib.delete(first['id'], 'retention', settings['record_path'])
    restarted = RecordingLibrary(lib.directory)
    changes = restarted.changes(after=cursor)
    assert any(row['id'] == first['id'] and row['deleted_at'] for row in changes['items'])
    assert all('path' not in row for row in changes['items'])
    assert restarted.search()['total'] == 1
    # A replayed JSONL/import must not resurrect a deleted original.
    restarted.add(first)
    with pytest.raises(FileNotFoundError):
        restarted.record(first['id'])


def test_cleanup_protects_local_pending_exports_and_playback(env):
    s, lib, storage = env
    row = add_record(env)
    s.update(cleanup_policy='verified', retention_days=1)
    assert storage.cleanup(s)['deleted'] == 0
    verified(lib, row['id'])
    with lib.borrow(row['id']):
        assert storage.cleanup(s)['deleted'] == 0
    with lib.connect() as db:
        db.execute("INSERT INTO exports VALUES('job',?,0,1,'queued','out.mp4','','2020')", (row['id'],))
    assert storage.cleanup(s)['deleted'] == 0
    with lib.connect() as db:
        db.execute("UPDATE exports SET state='ready'")
    assert storage.cleanup(s)['deleted'] == 1
    assert not Path(row['path']).exists()


def test_upload_cache_never_uses_local_cleanup_before_receipt(env):
    s, lib, storage = env
    row = add_record(env)
    s.update(cleanup_policy='local', upload_kind='directory')
    assert storage.cleanup(s)['deleted'] == 0
    lib.enqueue(row['id'], dict(kind='directory', path='/missing', url='', token=''))
    verified(lib, row['id'])
    assert storage.cleanup(s)['deleted'] == 0  # Pending target still protected.
    with lib.connect() as db:
        db.execute("UPDATE transfers SET state='verified'")
    assert storage.cleanup(s)['deleted'] == 1


def test_changed_same_size_original_is_not_cleaned_or_uploaded(env):
    s, lib, storage = env
    row = add_record(env)
    verified(lib, row['id'])
    path = Path(row['path'])
    stat = path.stat()
    path.write_bytes(b'x' * row['size'])
    import os
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1000000))
    with pytest.raises(ValueError, match='改变'):
        lib.delete(row['id'], 'retention', s['record_path'])
    with pytest.raises(FileNotFoundError):
        with lib.borrow(row['id']):
            pass
    assert path.exists()


def test_missing_volume_marker_blocks_empty_mount_fallback(env):
    s, lib, storage = env
    root = Path(s['record_path'])
    marker = root / '.pack-record-volume'
    marker.rename(root / 'old-marker')
    assert storage.status(s)[0]['free_gb'] is None
    with pytest.raises(OSError):
        storage.select(s)
    secondary = root.parent / 'second-disk'
    secondary.mkdir()
    s['record_paths'] = [str(secondary)]
    assert storage.select(s) == str(secondary)
    assert (secondary / '.pack-record-volume').exists()


def test_verified_directory_copy_and_idempotent_retry_keeps_original(env, tmp_path):
    s, lib, storage = env
    row = add_record(env)
    destination = tmp_path / 'nas'
    destination.mkdir()
    s.update(upload_kind='directory', upload_path=str(destination))
    manager = TransferManager(lib, storage, lambda: s, tmp_path)
    lib.enqueue(row['id'], manager.target())
    lib.enqueue(row['id'], manager.target())
    assert len(lib.transfers()) == 1
    assert manager.run_one()
    result = lib.record(row['id'])
    assert result['upload_state'] == 'verified'
    assert file_sha256(result['remote_url']) == file_sha256(row['path'])
    assert Path(row['path']).exists()
    with lib.connect() as db:
        db.execute("UPDATE transfers SET state='queued'")  # Simulate lost receipt.
    assert manager.run_one()
    assert lib.transfers()[0]['state'] == 'verified'


def test_failed_upload_persists_and_is_retried_after_restart(env, tmp_path):
    s, lib, storage = env
    row = add_record(env)
    s.update(upload_kind='directory', upload_path=str(tmp_path / 'offline'))
    manager = TransferManager(lib, storage, lambda: s, tmp_path)
    lib.enqueue(row['id'], manager.target())
    manager.run_one()
    failed = lib.transfers()[0]
    assert failed['state'] == 'queued' and failed['attempts'] == 1
    assert Path(row['path']).exists()
    with lib.connect() as db:
        db.execute("UPDATE transfers SET state='uploading'")
    restarted = RecordingLibrary(lib.directory)
    assert restarted.transfers()[0]['state'] == 'queued'


def test_order_announcements_validate_staleness_and_active_updates(env):
    s, lib, _ = env
    events = []
    orders = OrderAlerts(lib, lambda: s, lambda kind, data: events.append((kind, data)))
    orders.upsert([dict(tracking_number='A', quantity=3, buyer_remark='轻放', refund_status='pending')])
    orders.announce('A', 'session1')
    orders.announce('A', 'session1')
    assert len(events) == 1
    assert events[0][1]['warning'] and '3 件' in events[0][1]['text'] and '轻放' in events[0][1]['text']
    orders.upsert([dict(tracking_number='A', quantity=4, refund_status='refunded')])
    assert len(events) == 2 and '4 件' in events[1][1]['text']
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    orders.upsert([dict(tracking_number='B', quantity=8, updated_at=stale)])
    orders.announce('B', 'session2')
    assert len(events) == 2
    with pytest.raises(ValueError):
        orders.upsert([dict(tracking_number='C', quantity=True)])
    assert orders.lookup('C') is None


@pytest.fixture
def video(tmp_path):
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        pytest.skip('FFmpeg required')
    path = tmp_path / 'sample.mkv'
    subprocess.run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                    'testsrc2=size=320x180:rate=15', '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '2', '-c:v', 'libx264', '-threads', '1', '-c:a', 'aac', str(path)], check=True)
    return path


def incoming_values(s, video):
    return dict(id='source-1', station_id='station-remote', station_name='Remote', barcode='TRACK-1',
                start_time='2026-10-16 12:00:00', end_time='2026-10-16 12:00:02',
                duration=2, size=video.stat().st_size, sha256=file_sha256(video), extension='.mkv',
                capture={'interrupted': True})


def test_incoming_replay_crash_after_rename_and_hash_conflicts(env, video, tmp_path):
    s, lib, storage = env
    manager = TransferManager(lib, storage, lambda: s, tmp_path)
    values = incoming_values(s, video)
    with pytest.raises(PermissionError):
        manager.start_incoming(values, 'another-station')
    start = manager.start_incoming(values, 'station-remote')
    identifier = start['upload_id']
    content = video.read_bytes()
    chunk = content[:100]
    digest = hashlib.sha256(chunk).hexdigest()
    assert manager.receive_chunk(identifier, 'station-remote', 0, chunk, digest)['offset'] == 100
    assert manager.receive_chunk(identifier, 'station-remote', 0, chunk, digest)['offset'] == 100
    with pytest.raises(ValueError):
        manager.receive_chunk(identifier, 'station-remote', 0, b'x'*100, hashlib.sha256(b'x'*100).hexdigest())
    assert manager.start_incoming(values, 'station-remote')['offset'] == 100
    rest = content[100:]
    manager.receive_chunk(identifier, 'station-remote', 100, rest, hashlib.sha256(rest).hexdigest())
    row = manager.incoming(identifier, 'station-remote')
    destination = manager.incoming_destination(row)
    destination.parent.mkdir(parents=True)
    Path(row['path']).replace(destination)  # Crash between rename and catalogue commit.
    restarted = TransferManager(RecordingLibrary(lib.directory), storage, lambda: s, tmp_path)
    assert restarted.start_incoming(values, 'station-remote')['offset'] == len(content)
    receipt = restarted.complete_incoming(identifier, 'station-remote')
    assert receipt == restarted.complete_incoming(identifier, 'station-remote')
    assert lib.record(receipt['record_id'])['capture']['interrupted'] is True
    with pytest.raises(ValueError, match='内容发生变化'):
        restarted.start_incoming(dict(values, sha256='a'*64), 'station-remote')
    assert video.exists()


@pytest.fixture
def http_station(tmp_path):
    service = RecorderService(tmp_path / "http-station")
    service.controller.settings.update(disk_critical_gb=1, disk_warn_gb=2, encoder='libx264', width=320, height=180)
    server = RecorderHTTPServer(('127.0.0.1', 0), service, 'admin', 'viewer')
    thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01})
    thread.start()
    base = 'http://127.0.0.1:' + str(server.server_port)
    def request(path, body=None, token='admin', headers=None, method=None):
        data = None if body is None else json.dumps(body).encode()
        req = Request(base + path, data=data, headers={'Authorization': 'Bearer '+token,
                      'Content-Type': 'application/json', **(headers or {})}, method=method)
        return urlopen(req, timeout=10)
    try:
        yield service, request, base
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
        service.close()


def test_http_viewer_paths_ranges_head_and_acl(http_station):
    service, request, base = http_station
    c = service.controller
    c.storage.select(c.settings)
    row = add_record((c.settings, c.library, c.storage))
    result = json.load(request('/v1/library', token='viewer'))
    assert 'path' not in result['items'][0] and result['total'] == 1
    status = json.load(request('/v1/status', token='viewer'))
    assert 'record_path' not in status['settings'] and not status['events']
    for path, body in [('/v1/commands/scan', {'tracking':'A'}), ('/v1/cleanup', {}), ('/v1/setup/devices', {})]:
        with pytest.raises(HTTPError) as denied:
            request(path, body, token='viewer')
        assert denied.value.code == 403
    response = request('/v1/recordings/'+row['id']+'/file?download=1', token='viewer', headers={'Range':'bytes=0-4'})
    assert response.status == 206 and response.read() == b'video'
    assert "UTF-8''" in response.headers['Content-Disposition']
    response = request('/v1/recordings/'+row['id']+'/file', token='viewer', method='HEAD')
    assert response.read() == b'' and int(response.headers['Content-Length']) == row['size']
    media = json.load(request('/v1/playback', dict(record_id=row['id']), token='viewer'))
    assert urlopen(base + media['url']).read() == b'video evidence'
    with pytest.raises(HTTPError) as invalid:
        request('/v1/recordings/'+row['id']+'/file', headers={'Range':'bytes=999-'})
    assert invalid.value.code == 416
    credential = json.load(request('/v1/devices', dict(station_id='station-remote', name='Remote')))
    with pytest.raises(HTTPError) as denied:
        request('/v1/library', token=credential['token'])
    assert denied.value.code == 403
    request('/v1/devices/revoke', dict(station_id='station-remote'))
    with pytest.raises(HTTPError) as denied:
        request('/v1/node', token=credential['token'])
    assert denied.value.code == 401


def test_http_center_upload_receipt_and_compatible_clip(http_station, video, env, tmp_path):
    service, request, base = http_station
    s, lib, storage = env
    row = add_record(env, content=video.read_bytes(), duration=2)
    credential = json.load(request('/v1/devices', dict(station_id=s['station_id'], name='Source')))
    s.update(upload_kind='center', upload_url=base, upload_token=credential['token'])
    transfer = TransferManager(lib, storage, lambda: s, tmp_path)
    lib.enqueue(row['id'], transfer.target())
    assert transfer.run_one()
    assert lib.transfers()[0]['state'] == 'verified', lib.transfers()
    library = json.load(request('/v1/library'))
    assert library['total'] == 1 and library['items'][0]['capture']['interrupted'] is True
    # Centre capture settings must not constrain a remote video's resolution or export codec.
    service.controller.settings.update(encoder='hevc_qsv', width=2560, height=1440)
    job = json.load(request('/v1/exports', dict(record_id=library['items'][0]['id'], start=.2, end=1.2), token='viewer'))
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        result = json.load(request('/v1/exports/'+job['id'], token='viewer'))
        if result['state'] in ('ready','error'):
            break
        time.sleep(.1)
    assert result['state'] == 'ready', result
    output = service.controller.manager.export(job['id'])['path']
    streams = json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',output]))['streams']
    assert any(stream['codec_name']=='h264' for stream in streams)
    assert any(stream['codec_name']=='aac' for stream in streams)
    assert Path(row['path']).exists()


def test_setup_real_capture_blocks_scan_requires_matching_configuration(http_station):
    from ffmpeg_backend import CaptureSession
    service, request, base = http_station
    c = service.controller
    c.session_factory = lambda settings, path: CaptureSession(settings, path, synthetic=True)
    c.settings.update(microphone='default')
    with pytest.raises(HTTPError):
        request('/v1/setup/complete', {})
    job = json.load(request('/v1/setup/test', {}))
    assert not c.scan('DURING-TEST')
    deadline = time.monotonic()+15
    while time.monotonic()<deadline:
        result = json.load(request('/v1/tests/'+job['id']))
        if result['state'] != 'running':
            break
        time.sleep(.1)
    assert result['state']=='ready', result
    assert json.load(request('/v1/setup/complete', {'test_id':job['id']})) is True
    assert RecorderSettingsStore(service.directory).load()['setup_complete']
    c.settings['width']=640
    with pytest.raises(HTTPError):
        request('/v1/setup/complete', {'test_id':job['id']})
    assert c.library.search()['total']==0  # Diagnostic footage is not a shipment.


def test_static_console_is_offline_and_cannot_escape_asset_root(http_station, tmp_path, monkeypatch):
    import web_ui
    root = tmp_path / 'static'
    (root / 'assets').mkdir(parents=True)
    (root / 'index.html').write_text('<html><script src="/assets/app.js"></script></html>')
    (root / 'assets' / 'app.js').write_text('console.log("offline")')
    monkeypatch.setattr(web_ui, 'web_root', lambda: root)
    service, request, base = http_station
    response = urlopen(base+'/')
    assert response.status == 200
    assert b'/assets/app.js' in response.read()
    assert "script-src 'self'" in response.headers['Content-Security-Policy']
    response = urlopen(base+'/assets/app.js')
    assert 'javascript' in response.headers['Content-Type']
    assert response.read() == b'console.log("offline")'
    outside = tmp_path / 'private.token'
    outside.write_text('private-credential')
    with pytest.raises(HTTPError) as denied:
        urlopen(base+'/assets/../../private.token')
    assert denied.value.code == 404
    service.controller.settings['role']='viewer'
    assert json.load(request('/v1/status'))['access'] == 'admin'
    assert json.load(request('/v1/status', token='viewer'))['access'] == 'viewer'
