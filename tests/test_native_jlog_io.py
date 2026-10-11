import io
import json
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch

from project_sekai.native_minitouch import NativeMinitouchDevice


class BlockedDisk(io.StringIO):
    def __init__(self, operation="flush"):
        super().__init__()
        self.operation = operation
        self.entered, self.resume = threading.Event(), threading.Event()

    def flush(self):
        if self.operation == "flush":
            self.entered.set()
            self.resume.wait(2)
        return super().flush()

    def close(self):
        if self.operation == "close":
            self.entered.set()
            self.resume.wait(2)
        super().close()


class JlogIoTests(unittest.TestCase):
    def test_open_failure_does_not_affect_memory_reader_or_release(self):
        device = NativeMinitouchDevice("adb", "test", jlog_path=Path(".local/test-jlog-error.jsonl"))
        with patch.object(Path, "open", side_effect=OSError("open failed")):
            writer = device._start_jlog_writer()
            writer.thread.join(1)
        device._record_log_line("memory")
        self.assertEqual(device.logs_since(0), (1, ["memory"]))
        self.assertTrue(device.stop())
        diagnostics = device.jlog_io_diagnostics
        self.assertIn("open failed", diagnostics["error"])
        self.assertIn("disk-error", diagnostics["warnings"])
        self.assertFalse(diagnostics["complete"])

    def test_write_flush_and_close_failures_are_diagnostic_only(self):
        for operation in ("write", "flush", "close"):
            with self.subTest(operation=operation):
                device = NativeMinitouchDevice("adb", "test")
                class ErrorDisk(io.StringIO):
                    def write(self, text):
                        if operation == "write": raise OSError("write failed")
                        return super().write(text)
                    def flush(self):
                        if operation == "flush": raise OSError("flush failed")
                    def close(self):
                        super().close()
                        if operation == "close": raise OSError("close failed")
                writer = device._start_jlog_writer(stream=ErrorDisk())
                device._record_log_line("memory")
                writer.stop(); writer.thread.join(1)
                self.assertEqual(device.logs_since(0), (1, ["memory"]))
                self.assertTrue(device.stop())
                self.assertIn(operation + " failed", device.jlog_io_diagnostics["error"])
                self.assertFalse(device.jlog_io_diagnostics["complete"])

    def test_queue_full_drops_disk_evidence_without_losing_memory(self):
        device = NativeMinitouchDevice("adb", "test")
        disk = BlockedDisk(); writer = device._start_jlog_writer(stream=disk, capacity=2)
        try:
            device._record_log_line("first")
            self.assertTrue(disk.entered.wait(1))
            for index in range(10): device._record_log_line(str(index))
            diagnostics = device.jlog_io_diagnostics
            self.assertEqual(diagnostics["dropped_lines"], 8)
            self.assertIn("diagnostic-lines-dropped", diagnostics["warnings"])
            self.assertEqual(len(device.logs_since(0)[1]), 11)
        finally:
            disk.resume.set(); writer.stop(); writer.thread.join(1)

    def test_blocked_close_does_not_block_stop_or_new_generation_open(self):
        device = NativeMinitouchDevice("adb", "test", jlog_path=Path(".local/test-jlog-generation.jsonl"))
        disk = BlockedDisk("close"); writer = device._start_jlog_writer(stream=disk)
        try:
            writer.stop(); self.assertTrue(disk.entered.wait(1))
            started = time.perf_counter()
            self.assertTrue(device.stop())
            self.assertLess(time.perf_counter() - started, .1)
            with patch.object(Path, "open") as opener:
                other = NativeMinitouchDevice("adb", "test", jlog_path=device._jlog_path)
                other._reset_generation = 2
                self.assertIsNone(other._start_jlog_writer())
                other._record_log_line("new memory")
                opener.assert_not_called()
            self.assertEqual(other.logs_since(0), (1, ["new memory"]))
            self.assertEqual(other.jlog_io_diagnostics["missing_lines"], 1)
            self.assertIn("previous-generation-writer-pending", other.jlog_io_diagnostics["warnings"])
            device._reset_generation += 1
            self.assertIsNone(device._start_jlog_writer())
        finally:
            disk.resume.set(); writer.thread.join(1)

    def test_old_reader_generation_cannot_provide_reset_evidence_or_new_memory(self):
        device = NativeMinitouchDevice("adb", "test")
        device._reset_generation = 2
        device._reset_requested = True
        device._reset_request_cursor = 0
        device._record_log_line('jlog {"st":1,"et":2,"c":1,"cmd":"r"}', source_generation=1)
        self.assertEqual(device.logs_since(0), (0, []))
        self.assertFalse(device.reset_executed)

    def test_late_receive_after_stop_and_new_generation_cannot_enter_new_run(self):
        device = NativeMinitouchDevice("adb", "test")
        entered, resume = threading.Event(), threading.Event()
        class Client:
            connected = True
            def receive(self, *_):
                entered.set(); resume.wait(1)
                return 'jlog {"st":1,"et":2,"c":1,"cmd":"r"}\n'
            def close(self): self.connected = False
        old = Client()
        device._closed = False; device._client = old; device._reset_generation = 1
        reader = threading.Thread(target=device._read_logs, args=(old, 1))
        reader.start(); self.assertTrue(entered.wait(1))
        try:
            self.assertTrue(device.stop())
            device._closed = False; device._client = Client(); device._reset_generation = 2
            device._reset_requested = True; device._reset_request_cursor = 0
            resume.set(); reader.join(1)
            self.assertFalse(reader.is_alive())
            self.assertEqual(device.logs_since(0), (0, []))
            self.assertFalse(device.reset_executed)
        finally:
            resume.set(); reader.join(1)

    def test_blocked_open_remains_diagnostic_and_cannot_hold_release(self):
        device = NativeMinitouchDevice("adb", "test", jlog_path=Path(".local/test-jlog-open.jsonl"))
        entered, resume = threading.Event(), threading.Event()
        def open_stream(*_, **__):
            entered.set(); resume.wait(1)
            return io.StringIO()
        with patch.object(Path, "open", side_effect=open_stream):
            writer = device._start_jlog_writer()
            try:
                self.assertTrue(entered.wait(1))
                device._record_log_line("memory")
                self.assertEqual(device.logs_since(0), (1, ["memory"]))
                self.assertEqual(device.jlog_io_diagnostics["io_operation"], "open")
                self.assertTrue(device.stop())
                self.assertTrue(writer.thread.is_alive())
            finally:
                resume.set(); writer.thread.join(1)

    def test_healthy_writer_preserves_all_lines_and_reports_completion(self):
        device = NativeMinitouchDevice("adb", "test")
        class SavingDisk(io.StringIO):
            def close(self):
                self.saved = self.getvalue()
                super().close()
        disk = SavingDisk(); writer = device._start_jlog_writer(stream=disk)
        for index in range(100): device._record_log_line(str(index))
        writer.stop(); writer.thread.join(1)
        self.assertEqual(disk.saved.splitlines(), [str(index) for index in range(100)])
        diagnostics = device.jlog_io_diagnostics
        self.assertEqual(diagnostics["written_lines"], 100)
        self.assertTrue(diagnostics["complete"])
        self.assertEqual(diagnostics["missing_lines"], 0)

    def test_blocked_flush_does_not_block_memory_reset_or_stop(self):
        device = NativeMinitouchDevice("adb", "test")
        disk = BlockedDisk()
        writer = device._start_jlog_writer(stream=disk)
        try:
            device._record_log_line("first")
            self.assertTrue(disk.entered.wait(1))
            device._reset_requested = True
            device._reset_request_cursor = 1
            device._reset_requested_at_s = time.perf_counter()
            line = "jlog " + json.dumps({"st": 1, "et": 2, "c": 1, "cmd": "r"})
            started = time.perf_counter()
            device._record_log_line(line)
            sequence, rows = device.log_records_since(0)
            self.assertLess(time.perf_counter() - started, .05)
            self.assertEqual(sequence, 2)
            self.assertEqual(len(rows), 2)
            self.assertTrue(device.reset_executed)
            started = time.perf_counter()
            self.assertTrue(device.stop())
            self.assertLess(time.perf_counter() - started, .1)
        finally:
            disk.resume.set()
            writer.stop()
            writer.thread.join(1)
