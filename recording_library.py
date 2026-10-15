"""Durable station catalogue, transfer queue, exports and order adapter boundary."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def file_sha256(path):
    with open(path, 'rb') as source:
        return hashlib.file_digest(source, 'sha256').hexdigest()


class RecordingLibrary:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'library.sqlite3'
        self.lock = threading.RLock()
        self.leases = {}
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS recordings(
                    id TEXT PRIMARY KEY, barcode TEXT NOT NULL, start_time TEXT, end_time TEXT,
                    duration REAL NOT NULL, path TEXT NOT NULL, size INTEGER NOT NULL,
                    resolution TEXT, fps REAL, station_id TEXT, station_name TEXT,
                    origin TEXT NOT NULL DEFAULT 'local', capture TEXT NOT NULL DEFAULT '{}',
                    sha256 TEXT NOT NULL DEFAULT '', upload_state TEXT NOT NULL DEFAULT 'local',
                    remote_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    deleted_at TEXT, delete_reason TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS recording_time ON recordings(start_time);
                CREATE INDEX IF NOT EXISTS recording_barcode ON recordings(barcode);
                CREATE INDEX IF NOT EXISTS recording_station ON recordings(station_id);
                CREATE TABLE IF NOT EXISTS transfers(
                    id TEXT PRIMARY KEY, record_id TEXT NOT NULL, target TEXT NOT NULL,
                    state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '',
                    sha256 TEXT NOT NULL DEFAULT '', offset INTEGER NOT NULL DEFAULT 0,
                    remote_url TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    UNIQUE(record_id,target));
                CREATE TABLE IF NOT EXISTS exports(
                    id TEXT PRIMARY KEY, record_id TEXT NOT NULL, start REAL NOT NULL,
                    end REAL NOT NULL, state TEXT NOT NULL, path TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS incoming(
                    id TEXT PRIMARY KEY, station_id TEXT NOT NULL, source_id TEXT NOT NULL,
                    sha256 TEXT NOT NULL, size INTEGER NOT NULL, metadata TEXT NOT NULL,
                    path TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'receiving',
                    offset INTEGER NOT NULL DEFAULT 0, record_id TEXT,
                    updated_at REAL NOT NULL, UNIQUE(station_id,source_id));
                CREATE TABLE IF NOT EXISTS devices(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, token_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS orders(
                    barcode TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS audit(
                    id INTEGER PRIMARY KEY, time TEXT NOT NULL, action TEXT NOT NULL,
                    record_id TEXT, detail TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imports(path TEXT PRIMARY KEY, offset INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS volumes(path TEXT PRIMARY KEY,marker TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS changes(revision INTEGER PRIMARY KEY AUTOINCREMENT,record_id TEXT NOT NULL);
                CREATE TRIGGER IF NOT EXISTS recording_insert AFTER INSERT ON recordings BEGIN
                    INSERT INTO changes(record_id) VALUES(new.id); END;
                CREATE TRIGGER IF NOT EXISTS recording_update AFTER UPDATE ON recordings BEGIN
                    INSERT INTO changes(record_id) VALUES(new.id); END;
                INSERT INTO changes(record_id) SELECT id FROM recordings WHERE id NOT IN (SELECT record_id FROM changes);

            ''')
            db.execute("UPDATE transfers SET state='queued' WHERE state='uploading'")
            db.execute("UPDATE recordings SET upload_state='queued' WHERE upload_state='uploading'")
            db.execute("UPDATE exports SET state='queued' WHERE state='running'")
        with self.connect() as db:
            if 'mtime_ns' not in {row[1] for row in db.execute('PRAGMA table_info(recordings)')}:
                db.execute('ALTER TABLE recordings ADD COLUMN mtime_ns INTEGER')
        self.import_jsonl()

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            if os.name != 'nt':
                self.path.chmod(0o600)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA busy_timeout=15000')
            db.execute('PRAGMA synchronous=FULL')
            try:
                with db:
                    yield db
            finally:
                db.close()

    def record(self, record_id, include_deleted=False):
        with self.connect() as db:
            row = db.execute('SELECT * FROM recordings WHERE id=?', (record_id,)).fetchone()
        if not row or (row['deleted_at'] and not include_deleted):
            raise FileNotFoundError('录像不存在或已按保留策略清理')
        result = dict(row)
        result['capture'] = json.loads(result['capture'])
        return result

    def add(self, record):
        identifier = record.get('id') or record.get('recording_id') or uuid.uuid4().hex
        path = str(Path(record.get('path') or record['video_path']).resolve())
        capture = record.get('capture', {})
        if not isinstance(capture, dict):
            raise ValueError('录像完整性元数据必须为对象')
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO recordings
                (id,barcode,start_time,end_time,duration,path,size,resolution,fps,station_id,station_name,
                 origin,capture,sha256,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                (identifier, record.get('barcode', ''), record.get('start_time'), record.get('end_time'),
                 record.get('duration', 0), path, record.get('size', record.get('file_size', 0)),
                 record.get('resolution', ''), record.get('fps', 0), record.get('station_id', ''),
                 record.get('station_name', '本机'), record.get('origin', 'local'),
                 json.dumps(capture, ensure_ascii=False), record.get('sha256', ''), record.get('created_at', utc_now())))
        with self.connect() as db:
            if Path(path).is_file():
                db.execute('UPDATE recordings SET mtime_ns=? WHERE id=? AND mtime_ns IS NULL',
                           (Path(path).stat().st_mtime_ns, identifier))
        if record.get('station_id'):

            with self.connect() as db:
                db.execute("UPDATE recordings SET station_id=?,station_name=? WHERE id=? AND (station_id='' OR station_id IS NULL)",
                           (record['station_id'], record.get('station_name', '本机'), identifier))
        return identifier

    def import_jsonl(self):
        # Append-only journals remain audit records; checkpoint only complete lines.
        for path in self.directory.glob('*.jsonl'):
            with self.connect() as db:
                row = db.execute('SELECT offset FROM imports WHERE path=?', (str(path),)).fetchone()
                offset = row[0] if row and row[0] <= path.stat().st_size else 0
                with path.open('rb') as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b'\n'):
                            break
                        offset += len(line)
                        try:
                            record = json.loads(line)
                            if not record.get('recording_id'):
                                record['recording_id'] = 'legacy-' + hashlib.sha256(line).hexdigest()
                            self.add(record)
                        except (ValueError, KeyError, TypeError, sqlite3.Error):
                            continue
                db.execute('INSERT OR REPLACE INTO imports VALUES(?,?)', (str(path), offset))

    def search(self, search='', date_from='', date_to='', station='', page=1, page_size=50, include_deleted=False):
        page, page_size = max(1, int(page)), min(200, max(1, int(page_size)))
        where, args = [], []
        if not include_deleted:
            where.append('deleted_at IS NULL')
        if search:
            where.append("barcode LIKE ? ESCAPE '\\'")
            args.append('%' + search.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%')
        for value, clause in ((date_from, 'substr(start_time,1,10)>=?'), (date_to, 'substr(start_time,1,10)<=?'),
                              (station, 'station_id=?')):
            if value:
                where.append(clause)
                args.append(value)
        clause = ' AND '.join(where) or '1=1'
        with self.connect() as db:
            total = db.execute('SELECT count(*) FROM recordings WHERE ' + clause, args).fetchone()[0]
            rows = db.execute('SELECT * FROM recordings WHERE ' + clause + ' ORDER BY start_time DESC,id DESC LIMIT ? OFFSET ?',
                              args + [page_size, (page - 1) * page_size]).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item['capture'] = json.loads(item['capture'])
            items.append(item)
        return dict(items=items, total=total, page=page, page_size=page_size)

    @contextmanager
    def borrow(self, identifier):
        with self.lock:
            record = self.record(identifier)
            path = Path(record['path'])
            if (not path.is_file() or path.is_symlink() or path.stat().st_size != record['size'] or
                    (record.get('mtime_ns') is not None and path.stat().st_mtime_ns != record['mtime_ns'])):
                raise FileNotFoundError('录像文件缺失或大小已改变，请人工检查')
            self.leases[identifier] = self.leases.get(identifier, 0) + 1
        try:
            yield record
        finally:
            with self.lock:
                self.leases[identifier] -= 1
                if not self.leases[identifier]:
                    del self.leases[identifier]

    def audit(self, action, record_id='', detail=''):
        with self.connect() as db:
            db.execute('INSERT INTO audit(time,action,record_id,detail) VALUES(?,?,?,?)',
                       (utc_now(), action, record_id, detail))

    def delete(self, identifier, reason, allowed_root, allow_local=False):
        with self.lock:
            record = self.record(identifier)
            with self.connect() as db:
                pending = db.execute("SELECT 1 FROM exports WHERE record_id=? AND state IN ('queued','running') UNION ALL SELECT 1 FROM transfers WHERE record_id=? AND state IN ('queued','uploading','error') LIMIT 1", (identifier, identifier)).fetchone()
            if pending or self.leases.get(identifier):
                return False
            if record['upload_state'] != 'verified' and not allow_local:
                return False
            path, root = Path(record['path']), Path(allowed_root).resolve()
            if path.is_symlink() or not path.resolve().is_relative_to(root):
                raise ValueError('录像路径超出管理目录')
            if path.exists() and (path.stat().st_size != record['size'] or
                                  record.get('mtime_ns') is None or path.stat().st_mtime_ns != record['mtime_ns']):
                raise ValueError('录像已改变，拒绝自动清理')
            # Intent survives a crash between unlink and metadata commit.
            self.audit('cleanup_intent', identifier, reason)
            path.unlink(missing_ok=True)
            with self.connect() as db:
                db.execute('UPDATE recordings SET deleted_at=?,delete_reason=? WHERE id=?',
                           (utc_now(), reason, identifier))
            self.audit('cleanup', identifier, reason)
            return True

    def enqueue(self, record_id, target):
        if target.get('kind') == 'none':
            return
        record = self.record(record_id)
        if record['origin'] != 'local':
            return  # Received videos are not re-uploaded into loops.
        with self.connect() as db:
            db.execute('''INSERT OR IGNORE INTO transfers(id,record_id,target,state,created_at)
                          VALUES(?,?,?,'queued',?)''',
                       (uuid.uuid4().hex, record_id, json.dumps(target, sort_keys=True), utc_now()))
            db.execute("UPDATE recordings SET upload_state='queued' WHERE id=? AND upload_state!='verified'", (record_id,))

    def transfers(self):
        with self.connect() as db:
            return [dict(row) for row in db.execute('''SELECT t.id,t.record_id,r.barcode,t.state,t.attempts,t.offset,
                t.error,t.remote_url,t.next_attempt FROM transfers t JOIN recordings r ON r.id=t.record_id
                ORDER BY t.created_at DESC LIMIT 200''')]

    def retry_transfers(self):
        with self.connect() as db:
            db.execute("UPDATE transfers SET state='queued',next_attempt=0,error='',attempts=0 WHERE state='error'")
            db.execute("UPDATE recordings SET upload_state='queued' WHERE id IN (SELECT record_id FROM transfers WHERE state='queued')")

    def changes(self, after=0, limit=200):
        after, limit = max(0, int(after)), max(1, min(500, int(limit)))
        with self.connect() as db:
            changes = db.execute('SELECT * FROM changes WHERE revision>? ORDER BY revision LIMIT ?', (after, limit)).fetchall()
            head = db.execute('SELECT coalesce(max(revision),0) FROM changes').fetchone()[0]
        items = []
        for change in changes:
            record = self.record(change['record_id'], include_deleted=True)
            item = {key: record[key] for key in ('id','barcode','station_id','station_name','origin','start_time','end_time',
                                               'duration','size','resolution','fps','sha256','upload_state','deleted_at','delete_reason','capture')}
            item['revision'] = change['revision']
            items.append(item)
        return dict(items=items, cursor=changes[-1]['revision'] if changes else after, head=head, has_more=bool(changes and changes[-1]['revision'] < head))
