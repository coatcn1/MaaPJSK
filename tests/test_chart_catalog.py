from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

import cv2
import numpy as np

from project_sekai.chart_catalog import (
    ASSET_ROOT, GIT_REFS_URL, MASTER_ROOT, ChartRepository, HttpClient, HttpResult,
    catalog_lock, sync_catalog, validate_sus,
)


REVISION = "a" * 40
SUS = b'#REQUEST "ticks_per_beat 480"\n#BPM01: 120\n#00011:11110000\n'
CHANGED_SUS = SUS.replace(b"120", b"140")


class FakeClient:
    def __init__(self) -> None:
        self.resources: dict[str, tuple[bytes, str, str] | Exception] = {}
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.after_get = lambda _url: None
        musics = [{"id": song_id, "title": f"测试歌曲 {song_id}", "publishedAt": published,
                   "assetbundleName": f"jacket_s_{song_id + 8:03d}"}
                  for song_id, published in [(1, 1), (2, 1), (3, 9_999_999_999_999)]]
        difficulties = [{"musicId": song_id, "musicDifficulty": difficulty,
                         "playLevel": 25, "totalNoteCount": 4}
                        for song_id, difficulty in [(1, "expert"), (1, "master"), (2, "master"), (3, "master")]]
        self.resources[GIT_REFS_URL] = (b"# service=git-upload-pack\n" + REVISION.encode() + b" HEAD\x00capabilities\n",
                                       "application/x-git-upload-pack-advertisement", "")
        for name, data in [("musics", musics), ("musicDifficulties", difficulties)]:
            self.resources[f"{MASTER_ROOT}/{REVISION}/{name}.json"] = (json.dumps(data).encode(), "text/plain", "")
        _, png = cv2.imencode(".png", np.zeros((16, 16, 3), dtype=np.uint8))
        for song_id in [1, 2]:
            bundle = f"jacket_s_{song_id + 8:03d}"
            self.resources[f"{ASSET_ROOT}/music/jacket/{bundle}/{bundle}.png"] = (png.tobytes(), "image/png", "png-v1")
        for song_id, difficulty in [(1, "expert"), (1, "master"), (2, "master")]:
            self.resources[self.chart_url(song_id, difficulty)] = (SUS, "text/plain; charset=utf-8", "sus-v1")

    @staticmethod
    def chart_url(song_id: int, difficulty: str = "master") -> str:
        return f"{ASSET_ROOT}/music/music_score/{song_id:04d}_01/{difficulty}.txt"

    def get(self, url: str, *, etag: str = "", **_kwargs) -> HttpResult:
        with self.lock:
            self.requests.append((url, etag))
            resource = self.resources[url]
        if isinstance(resource, Exception):
            raise resource
        body, content_type, remote_etag = resource
        result = HttpResult(304, b"", etag=etag) if etag and etag == remote_etag else HttpResult(200, body, content_type, remote_etag)
        self.after_get(url)
        return result


class ChartCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.client = FakeClient()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def sync(self, **kwargs):
        return sync_catalog(self.root, client=self.client, workers=2, **kwargs)

    def test_full_catalog_only_fetches_published_existing_difficulties_from_one_revision(self) -> None:
        manifest = self.sync()
        summary = manifest["summary"]
        self.assertEqual((summary["stored_songs"], summary["charts"], summary["jackets"], summary["skipped_unpublished"]), (2, 3, 2, 1))
        self.assertEqual(summary["recoverable_errors"], 0)
        self.assertEqual(manifest["source"]["revision"], REVISION)
        self.assertEqual(sum(url == GIT_REFS_URL for url, _ in self.client.requests), 1)
        self.assertFalse(any("append.txt" in url or "0003_01" in url for url, _ in self.client.requests))
        self.assertTrue(any("jacket_s_009/jacket_s_009.png" in url for url, _ in self.client.requests))
        self.assertTrue(any("0001_01/master.txt" in url for url, _ in self.client.requests))

    def test_conditional_update_reuses_files_and_loads_offline(self) -> None:
        first = self.sync()
        paths = [chart["path"] for song in first["songs"] for chart in song["charts"].values()]
        mtimes = [(self.root / path).stat().st_mtime_ns for path in paths]
        second = self.sync()
        self.assertEqual(second["summary"]["unchanged"], 5)
        self.assertEqual(second["summary"]["updated"], 0)
        self.assertEqual(mtimes, [(self.root / path).stat().st_mtime_ns for path in paths])
        with patch("project_sekai.chart_catalog.urlopen", side_effect=AssertionError("离线读取不能联网")):
            self.assertEqual(ChartRepository(self.root).load_chart(1, "master"), SUS.decode())

    def test_remote_change_updates_chart_without_destroying_old_manifest_files(self) -> None:
        first = self.sync()
        old_path = first["songs"][0]["charts"]["master"]["path"]
        self.client.resources[self.client.chart_url(1)] = (CHANGED_SUS, "text/plain", "sus-v2")
        second = self.sync()
        new_path = second["songs"][0]["charts"]["master"]["path"]
        self.assertNotEqual(old_path, new_path)
        self.assertEqual((self.root / old_path).read_bytes(), SUS)
        self.assertEqual(ChartRepository(self.root).load_chart(1, "master"), CHANGED_SUS.decode())
        self.assertEqual(second["summary"]["updated"], 1)

    def test_corrupt_cached_file_is_downloaded_without_conditional_header(self) -> None:
        manifest = self.sync()
        (self.root / manifest["songs"][0]["charts"]["master"]["path"]).write_bytes(b"broken")
        self.client.requests.clear()
        self.sync()
        self.assertIn((self.client.chart_url(1), ""), self.client.requests)
        self.assertEqual(ChartRepository(self.root).load_chart(1, "master"), SUS.decode())

    def test_missing_remote_asset_preserves_verified_old_version_but_records_stale(self) -> None:
        self.sync()
        url = self.client.chart_url(1)
        self.client.resources[url] = HTTPError(url, 404, "missing", {}, None)
        manifest = self.sync()
        entry = manifest["songs"][0]["charts"]["master"]
        self.assertEqual(entry["status"], "stale")
        self.assertEqual(manifest["summary"]["stale"], 1)
        self.assertEqual(manifest["summary"]["recoverable_errors"], 1)
        self.assertEqual(ChartRepository(self.root).load_chart(1, "master"), SUS.decode())

    def test_html_or_invalid_sus_is_not_saved_as_a_chart_and_other_songs_continue(self) -> None:
        for body, content_type in [(b"<html>error</html>", "text/html"), (b"not a SUS", "text/plain")]:
            with self.subTest(content_type=content_type):
                self.client.resources[self.client.chart_url(1)] = (body, content_type, "bad")
                manifest = self.sync()
                self.assertNotIn("master", manifest["songs"][0]["charts"])
                self.assertIn("master", manifest["songs"][1]["charts"])
                self.assertEqual(manifest["summary"]["recoverable_errors"], 1)

    def test_invalid_metadata_does_not_replace_previous_manifest(self) -> None:
        self.sync()
        previous = (self.root / "manifest.json").read_bytes()
        url = f"{MASTER_ROOT}/{REVISION}/musicDifficulties.json"
        self.client.resources[url] = (b'[{"musicId": 1, "musicDifficulty": "unknown"}]', "text/plain", "")
        with self.assertRaisesRegex(ValueError, "未知难度"):
            self.sync()
        self.assertEqual((self.root / "manifest.json").read_bytes(), previous)
        self.assertEqual(ChartRepository(self.root).load_chart(1, "master"), SUS.decode())

    def test_cancellation_preserves_previous_manifest_and_resumes_completed_download(self) -> None:
        self.sync()
        previous = (self.root / "manifest.json").read_bytes()
        self.client.resources[self.client.chart_url(1, "expert")] = (CHANGED_SUS, "text/plain", "sus-v2")
        stop = threading.Event()
        self.client.after_get = lambda url: stop.set() if url == self.client.chart_url(1, "expert") else None
        with self.assertRaises(InterruptedError):
            self.sync(stop_requested=stop.is_set)
        self.assertEqual((self.root / "manifest.json").read_bytes(), previous)
        self.assertEqual(ChartRepository(self.root).load_chart(1, "expert"), SUS.decode())
        self.client.after_get = lambda _url: None
        self.client.requests.clear()
        self.sync()
        self.assertIn((self.client.chart_url(1, "expert"), "sus-v2"), self.client.requests)
        self.assertEqual(ChartRepository(self.root).load_chart(1, "expert"), CHANGED_SUS.decode())

    def test_selected_song_validation_does_not_shrink_full_catalog(self) -> None:
        self.sync()
        previous = (self.root / "manifest.json").read_bytes()
        selection = self.sync(song_ids=(1,))
        self.assertEqual(selection["summary"]["stored_songs"], 1)
        self.assertEqual((self.root / "manifest.json").read_bytes(), previous)
        self.assertTrue((self.root / "manifest-selection.json").exists())

    def test_force_refresh_does_not_send_conditional_headers(self) -> None:
        self.sync()
        self.client.requests.clear()
        self.sync(force=True)
        self.assertFalse(any(etag for _url, etag in self.client.requests))

    def test_concurrent_sync_is_rejected_and_os_lock_releases_after_failure(self) -> None:
        with catalog_lock(self.root):
            with self.assertRaisesRegex(RuntimeError, "另一个同步器"):
                self.sync()
        self.assertEqual(self.sync()["summary"]["recoverable_errors"], 0)

    def test_offline_loader_rejects_path_traversal_and_modified_bytes(self) -> None:
        manifest = self.sync()
        chart = manifest["songs"][0]["charts"]["master"]
        (self.root / chart["path"]).write_bytes(CHANGED_SUS)
        with self.assertRaisesRegex(ValueError, "SHA256"):
            ChartRepository(self.root).load_chart(1, "master")
        chart["path"] = "../outside.sus"
        (self.root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "路径"):
            ChartRepository(self.root).load_chart(1, "master")

    def test_sus_validator_rejects_invalid_bpm_and_odd_note_row(self) -> None:
        for body in [SUS.replace(b"120", b"-1"), SUS.replace(b"11110000", b"111"), b"{}"]:
            with self.subTest(body=body), self.assertRaises(ValueError):
                validate_sus(body)


class HttpClientTests(unittest.TestCase):
    def test_real_http_conditional_request_and_size_limit(self) -> None:
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.headers.get("If-None-Match"))
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("ETag", '"v1"')
                self.end_headers()
                self.wfile.write(SUS)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/chart"
            client = HttpClient(timeout=2, retries=0)
            self.assertEqual(client.get(url).body, SUS)
            self.assertEqual(client.get(url, etag='"v1"').status, 304)
            with self.assertRaisesRegex(ValueError, "大小"):
                client.get(url, max_bytes=2)
            self.assertEqual(seen[:2], [None, '"v1"'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
