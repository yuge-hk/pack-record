import errno
import ast
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app_core import (
    AppSettingsStore,
    FailedJobStore,
    VideoDB,
    ensure_copy_space,
    move_recording_file,
    release_filename_reservation,
    reserve_unique_filename,
    sanitize_tracking_filename,
    unique_filename,
    video_settings_metadata,
    wait_for_recording_file_ready,
)


class CoreTests(unittest.TestCase):
    def test_settings_defaults_round_trip_and_relative_record_path(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AppSettingsStore(root / "settings.json", root)
            defaults = store.load()
            self.assertEqual(defaults["obs_ws_port"], 4455)
            defaults["record_path"] = "custom-recordings"
            defaults["max_record_minutes"] = 35
            saved = store.save(defaults)
            self.assertEqual(
                saved["record_path"],
                str((root / "custom-recordings").resolve()),
            )
            self.assertEqual(store.load()["max_record_minutes"], 35)

    def test_settings_reject_invalid_disk_thresholds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = AppSettingsStore(root / "settings.json", root)
            values = store.defaults()
            values["disk_warn_gb"] = 10
            values["disk_critical_gb"] = 10
            with self.assertRaisesRegex(ValueError, "禁止阈值"):
                store.save(values)

    def test_corrupt_settings_are_backed_up_and_defaults_restored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text("{broken", encoding="utf-8")
            store = AppSettingsStore(path, root)
            loaded = store.load()
            self.assertEqual(loaded["max_record_minutes"], 20)
            self.assertFalse(path.exists())
            self.assertEqual(len(list(root.glob("settings.json.corrupt-*"))), 1)
            self.assertIn("已备份", store.consume_warnings()[0])

    def test_settings_merge_new_defaults_from_old_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "settings.json"
            path.write_text(
                json.dumps({"obs_ws_host": "192.168.0.10"}),
                encoding="utf-8",
            )
            loaded = AppSettingsStore(path, root).load()
            self.assertEqual(loaded["obs_ws_host"], "192.168.0.10")
            self.assertTrue(loaded["auto_launch_obs"])

    def test_video_settings_metadata_supports_obsws_snake_case(self):
        settings = type(
            "Settings",
            (),
            {
                "base_width": 1280,
                "base_height": 800,
                "fps_numerator": 30000,
                "fps_denominator": 1001,
            },
        )()
        self.assertEqual(
            video_settings_metadata(settings),
            ("1280x800", 30000 / 1001),
        )

    def test_scan_handoff_uses_one_latest_value_not_a_queue(self):
        main_path = Path(__file__).resolve().parents[1] / "main.py"
        source = main_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        assignments = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Attribute)
                and target.attr == "pending_tracking_number"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
        ]
        self.assertEqual(len(assignments), 2)  # 初始化 + 最新值覆盖方法
        self.assertNotIn("deque", source)
        self.assertNotIn("pending_tracking_numbers", source)

    def test_sanitize_tracking_filename(self):
        self.assertEqual(
            sanitize_tracking_filename(' A/B:C*D?"E<F>G|. '),
            "A_B_C_D__E_F_G",
        )
        self.assertEqual(len(sanitize_tracking_filename("X" * 200)), 100)
        self.assertTrue(sanitize_tracking_filename("...___").startswith("UNKNOWN_"))
        self.assertEqual(sanitize_tracking_filename("CON"), "_CON")
        self.assertEqual(sanitize_tracking_filename("lpt1.txt"), "_lpt1.txt")

    def test_unique_filename(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ABC.mkv").touch()
            (root / "ABC(1).mkv").touch()
            self.assertEqual(
                unique_filename(root, "ABC.mkv"),
                str(root / "ABC(2).mkv"),
            )

    def test_video_db_concurrent_jsonl_append(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = VideoDB(root)

            def write(index):
                database.insert_record(
                    f"TRACK-{index}", "start", "end", index,
                    f"video-{index}", index, "1920x1080", 30,
                )

            threads = [threading.Thread(target=write, args=(index,)) for index in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            jsonl_path = next(root.glob("*.jsonl"))
            records = [
                json.loads(line)
                for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 20)
            self.assertEqual(
                {record["barcode"] for record in records},
                {f"TRACK-{index}" for index in range(20)},
            )

    def test_video_db_recording_id_is_idempotent_across_daily_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "2000-01-01.jsonl").write_text(
                json.dumps({"recording_id": "same-id"}) + "\n",
                encoding="utf-8",
            )
            database = VideoDB(root)
            inserted = database.insert_record(
                "TRACK", "start", "end", 1, "video", 1, "1920x1080", 30,
                recording_id="same-id",
            )
            self.assertFalse(inserted)
            self.assertEqual(len(list(root.glob("*.jsonl"))), 1)

    def test_failed_job_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FailedJobStore(Path(directory) / "failed_jobs.json")
            job_id = store.add({"tracking_number": "A", "source_path": "a.mkv"})
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.list()[0]["id"], job_id)
            store.replace([])
            self.assertEqual(store.count(), 0)

    def test_failed_job_store_upsert_and_remove(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FailedJobStore(Path(directory) / "failed_jobs.json")
            store.upsert({"id": "job-1", "state": "moving"})
            store.upsert({"id": "job-1", "state": "recording_metadata"})
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.list()[0]["state"], "recording_metadata")
            store.remove("job-1")
            self.assertEqual(store.count(), 0)

    def test_corrupt_failed_job_store_is_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed_jobs.json"
            path.write_text("{not-json", encoding="utf-8")
            store = FailedJobStore(path)
            self.assertEqual(store.list(), [])
            self.assertFalse(path.exists())
            backups = list(path.parent.glob("failed_jobs.json.corrupt-*"))
            self.assertEqual(len(backups), 1)
            self.assertIn("已备份", store.consume_warnings()[0])

    def test_non_list_failed_job_store_is_backed_up(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "failed_jobs.json"
            path.write_text('{"unexpected": true}', encoding="utf-8")
            store = FailedJobStore(path)
            self.assertEqual(store.count(), 0)
            self.assertEqual(
                len(list(path.parent.glob("failed_jobs.json.corrupt-*"))),
                1,
            )

    def test_retry_merge_preserves_new_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FailedJobStore(Path(directory) / "failed_jobs.json")
            old_id = store.add({"tracking_number": "OLD"})
            jobs_to_retry = store.list()
            store.add({"tracking_number": "NEW"})
            jobs_to_retry[0]["last_error"] = "still failing"
            store.finish_retry({old_id}, jobs_to_retry)
            self.assertEqual(
                {job["tracking_number"] for job in store.list()},
                {"OLD", "NEW"},
            )

    def test_cross_device_move_uses_copy_then_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            source.write_bytes(b"video-data")
            real_rename = os.rename

            def rename_with_cross_device_error(src, dst):
                if str(src) == str(source) and str(dst) == str(destination):
                    raise OSError(errno.EXDEV, "cross-device link")
                return real_rename(src, dst)

            with patch("app_core.os.rename", side_effect=rename_with_cross_device_error):
                move_recording_file(source, destination, retries=1, retry_delay=0)
            self.assertEqual(destination.read_bytes(), b"video-data")
            self.assertFalse(source.exists())

    def test_existing_same_size_destination_never_deletes_unrelated_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            source.write_bytes(b"source-data")
            destination.write_bytes(b"other-video")
            with self.assertRaises(RuntimeError):
                move_recording_file(source, destination, retries=1, retry_delay=0)
            self.assertEqual(source.read_bytes(), b"source-data")
            self.assertEqual(destination.read_bytes(), b"other-video")

    def test_cross_device_unlink_failure_is_reconciled_without_recopied_data(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            destination = root / "destination.mkv"
            source.write_bytes(b"video-data")
            real_rename = os.rename
            real_unlink = os.unlink
            source_unlink_calls = 0

            def rename_with_cross_device_error(src, dst):
                if str(src) == str(source) and str(dst) == str(destination):
                    raise OSError(errno.EXDEV, "cross-device link")
                return real_rename(src, dst)

            def unlink_fails_once(path):
                nonlocal source_unlink_calls
                if str(path) == str(source):
                    source_unlink_calls += 1
                    if source_unlink_calls == 1:
                        raise PermissionError("file temporarily locked")
                return real_unlink(path)

            with (
                patch("app_core.os.rename", side_effect=rename_with_cross_device_error),
                patch("app_core.os.unlink", side_effect=unlink_fails_once),
            ):
                move_recording_file(source, destination, retries=2, retry_delay=0)
            self.assertFalse(source.exists())
            self.assertEqual(destination.read_bytes(), b"video-data")

    def test_unique_filename_reservations_are_concurrency_safe(self):
        with tempfile.TemporaryDirectory() as directory:
            results = []
            results_lock = threading.Lock()

            def reserve():
                result = reserve_unique_filename(directory, "TRACK.mkv")
                with results_lock:
                    results.append(result)

            threads = [threading.Thread(target=reserve) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            try:
                self.assertEqual(len({destination for destination, _ in results}), 20)
                self.assertTrue(all(Path(marker).is_file() for _, marker in results))
            finally:
                for _, marker in results:
                    release_filename_reservation(marker)

    def test_same_filesystem_move_does_not_require_copy_capacity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mkv"
            source.write_bytes(b"data")
            with patch("app_core.shutil.disk_usage") as disk_usage:
                ensure_copy_space(source, root, reserve_gb=999)
            disk_usage.assert_not_called()

    def test_empty_or_tiny_recording_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crashed.mp4"
            path.write_bytes(b"")
            with self.assertRaisesRegex(RuntimeError, "过小"):
                wait_for_recording_file_ready(
                    path,
                    minimum_size=1024,
                    timeout=0,
                    interval=0,
                )

    def test_stable_recording_file_is_accepted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "finished.mp4"
            path.write_bytes(b"x" * 2048)
            self.assertEqual(
                wait_for_recording_file_ready(
                    path,
                    minimum_size=1024,
                    timeout=1,
                    interval=0,
                    stable_checks=1,
                ),
                2048,
            )


if __name__ == "__main__":
    unittest.main()
