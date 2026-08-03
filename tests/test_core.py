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
    FailedJobStore,
    VideoDB,
    move_recording_file,
    sanitize_tracking_filename,
    unique_filename,
    video_settings_metadata,
)


class CoreTests(unittest.TestCase):
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

    def test_failed_job_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FailedJobStore(Path(directory) / "failed_jobs.json")
            job_id = store.add({"tracking_number": "A", "source_path": "a.mkv"})
            self.assertEqual(store.count(), 1)
            self.assertEqual(store.list()[0]["id"], job_id)
            store.replace([])
            self.assertEqual(store.count(), 0)

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
            real_replace = os.replace

            def replace_with_cross_device_error(src, dst):
                if str(src) == str(source) and str(dst) == str(destination):
                    raise OSError(errno.EXDEV, "cross-device link")
                return real_replace(src, dst)

            with patch("app_core.os.replace", side_effect=replace_with_cross_device_error):
                move_recording_file(source, destination, retries=1, retry_delay=0)
            self.assertEqual(destination.read_bytes(), b"video-data")
            self.assertFalse(source.exists())


if __name__ == "__main__":
    unittest.main()
