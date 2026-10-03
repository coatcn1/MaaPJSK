"""日服谱面库的显式在线维护与离线读取。演出任务不调用同步器。"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import cv2
import numpy as np


DIFFICULTIES = ("easy", "normal", "hard", "expert", "master", "append")
GIT_REFS_URL = "https://github.com/Sekai-World/sekai-master-db-diff.git/info/refs?service=git-upload-pack"
MASTER_ROOT = "https://raw.githubusercontent.com/Sekai-World/sekai-master-db-diff"
ASSET_ROOT = "https://storage.sekai.best/sekai-jp-assets"
SCHEMA_VERSION = 1
Progress = Callable[[str], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _write_json(path: Path, value: Any) -> None:
    _write_atomic(path, (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _check_stop(stop_requested: Callable[[], bool]) -> None:
    if stop_requested():
        raise InterruptedError("谱面同步已取消；已下载并校验的文件可在下次同步时复用")


@contextmanager
def catalog_lock(root: Path):
    # 使用操作系统锁，强制关闭同步进程后也会自动释放；锁文件保留以避免删除造成并发竞态。
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".sync.lock").open("a+b") as stream:
        stream.seek(0, os.SEEK_END)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("另一个同步器正在更新这个谱面库，请等待它结束") from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    content_type: str = ""
    etag: str = ""
    last_modified: str = ""


class HttpClient:
    def __init__(self, *, timeout: float = 20, retries: int = 2,
                 stop_requested: Callable[[], bool] | None = None) -> None:
        if not 0 < timeout <= 120 or not 0 <= retries <= 5:
            raise ValueError("网络超时须为 0–120 秒，重试次数须为 0–5")
        self.timeout = timeout
        self.retries = retries
        self.stop_requested = stop_requested or (lambda: False)

    def get(self, url: str, *, etag: str = "", last_modified: str = "",
            max_bytes: int = 16 * 1024 * 1024) -> HttpResult:
        headers = {"User-Agent": "MaaPJSK/0.1 chart-catalog", "Cache-Control": "no-cache"}
        if etag:
            headers["If-None-Match"] = etag
        elif last_modified:
            headers["If-Modified-Since"] = last_modified
        for attempt in range(self.retries + 1):
            _check_stop(self.stop_requested)
            try:
                with urlopen(Request(url, headers=headers), timeout=self.timeout) as response:
                    body = response.read(max_bytes + 1)
                    if len(body) > max_bytes:
                        raise ValueError("在线资源超出允许大小")
                    return HttpResult(response.status, body, response.headers.get("Content-Type", ""),
                                      response.headers.get("ETag", ""), response.headers.get("Last-Modified", ""))
            except HTTPError as error:
                if error.code == 304:
                    error.close()
                    return HttpResult(304, b"", etag=etag, last_modified=last_modified)
                retryable = error.code in {408, 429, 500, 502, 503, 504}
                error.close()
                if not retryable or attempt >= self.retries:
                    raise
            except (URLError, TimeoutError, OSError):
                if attempt >= self.retries:
                    raise
            # 短分段等待便于停止；失效路径和 404 不重试，避免把镜像缺失当成网络抖动。
            until = time.monotonic() + min(2 ** attempt, 8)
            while time.monotonic() < until:
                _check_stop(self.stop_requested)
                time.sleep(0.1)
        raise AssertionError("不可达的网络重试状态")


def _latest_revision(client: HttpClient) -> str:
    # 从公开 Git 引用固定一次快照，不依赖匿名 GitHub API 配额，也不需要本机 Git 程序。
    result = client.get(GIT_REFS_URL, max_bytes=1024 * 1024)
    if result.status != 200 or "git-upload-pack-advertisement" not in result.content_type:
        raise ValueError("无法读取日服 master 数据的 Git 引用")
    head = re.search(rb"([0-9a-f]{40}) HEAD(?:\x00|\n)", result.body)
    if head is None:
        raise ValueError("日服 master 数据没有可确认的 HEAD 版本")
    return head.group(1).decode("ascii")


def _fetch_index(client: HttpClient, revision: str, name: str) -> tuple[list[dict[str, Any]], bytes, str]:
    url = f"{MASTER_ROOT}/{revision}/{name}.json"
    result = client.get(url, max_bytes=8 * 1024 * 1024)
    if result.status != 200 or not any(t in result.content_type for t in ("json", "text/plain")):
        raise ValueError(f"{name} 返回的不是 master JSON 数据")
    data = json.loads(result.body.decode("utf-8-sig"))
    if not isinstance(data, list) or not data or not all(isinstance(item, dict) for item in data):
        raise ValueError(f"{name} 必须是非空对象列表")
    return data, result.body, url


def validate_sus(body: bytes) -> dict[str, Any]:
    text = body.decode("utf-8-sig")
    ticks = re.search(r'^#REQUEST\s+"ticks_per_beat\s+(\d+)"\s*$', text, re.MULTILINE)
    bpms = re.findall(r"^#BPM[0-9A-Za-z]{2}\s*:\s*([\d.+eE-]+)\s*$", text, re.MULTILINE)
    rows = re.findall(r"^#\d{3}[1-5][0-9A-Za-z]{1,2}\s*:\s*([0-9A-Za-z]+)\s*$", text, re.MULTILINE)
    if ticks is None or int(ticks.group(1)) <= 0 or not bpms or not rows:
        raise ValueError("资源不是有效的 Project SEKAI SUS 谱面（缺少时基、BPM 或音符行）")
    bpm_values = [float(value) for value in bpms]
    if any(not math.isfinite(value) or value <= 0 for value in bpm_values) or any(len(row) % 2 for row in rows):
        raise ValueError("SUS 的 BPM 或音符行长度无效")
    return {"format": "sus", "ticks_per_beat": int(ticks.group(1)),
            "initial_bpm": bpm_values[0], "note_rows": len(rows)}


def _validate_jacket(body: bytes) -> dict[str, Any]:
    if not body.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("封面响应不是 PNG 图像")
    image = cv2.imdecode(np.frombuffer(body, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None or not all(0 < dimension <= 4096 for dimension in image.shape[:2]):
        raise ValueError("封面图像无法解码或尺寸无效")
    return {"format": "png", "width": image.shape[1], "height": image.shape[0]}


def _safe_local_path(root: Path, relative: Any) -> Path:
    if not isinstance(relative, str) or Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise ValueError("本地谱面库路径无效")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("本地谱面库路径越界")
    return path


def _verified_cache(root: Path, receipt: Path, url: str, validate: Callable[[bytes], dict[str, Any]]) -> dict[str, Any] | None:
    try:
        cached = _read_json(receipt)
        if not isinstance(cached, dict) or cached.get("source_url") != url:
            return None
        path = _safe_local_path(root, cached["path"])
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        body = path.read_bytes()
        if _sha256(body) != cached["sha256"] or len(body) != cached["size"]:
            return None
        validate(body)
        return cached
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _sync_asset(root: Path, relative_directory: Path, stem: str, extension: str, url: str,
                client: HttpClient, validate: Callable[[bytes], dict[str, Any]], *, force: bool) -> tuple[dict[str, Any] | None, str | None]:
    receipt = root / ".cache" / f"{_sha256(url.encode('utf-8'))}.json"
    cached = _verified_cache(root, receipt, url, validate)
    try:
        response = client.get(url, etag=cached.get("etag", "") if cached and not force else "",
                              last_modified=cached.get("last_modified", "") if cached and not force else "")
        if response.status == 304:
            if cached is None:
                raise ValueError("服务器返回未修改，但没有已校验的本地文件")
            entry = dict(cached, status="unchanged", checked_at=_utc_now())
        elif response.status == 200:
            required_type = "image/png" if extension == "png" else "text/plain"
            if required_type not in response.content_type:
                raise ValueError(f"资源内容类型错误：需要 {required_type}")
            details = validate(response.body)
            digest = _sha256(response.body)
            relative = relative_directory / f"{stem}-{digest}.{extension}"
            path = _safe_local_path(root, relative.as_posix())
            # 用内容哈希命名，更新或取消时不会破坏仍被旧 manifest 引用的版本。
            if not path.exists() or _sha256(path.read_bytes()) != digest:
                _write_atomic(path, response.body)
            entry = {"path": relative.as_posix(), "source_url": url, "sha256": digest,
                     "size": len(response.body), "etag": response.etag, "last_modified": response.last_modified,
                     "downloaded_at": _utc_now(), "checked_at": _utc_now(),
                     "status": "unchanged" if cached and cached["sha256"] == digest else "updated", **details}
        else:
            raise ValueError(f"在线资源返回意外状态 {response.status}")
        _write_json(receipt, entry)
        return entry, None
    except InterruptedError:
        raise
    except (OSError, ValueError) as error:
        if cached is not None:
            # 失败时保留可用旧版，但明确标记为未确认最新，不能把它算作本次更新成功。
            return dict(cached, status="stale", update_error=str(error)), str(error)
        return None, str(error)


def _positive_integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"master 数据的 {name} 必须为正整数")
    return value


def _catalog_entries(musics: list[dict[str, Any]], difficulties: list[dict[str, Any]],
                     now_ms: int, selected_difficulties: tuple[str, ...]) -> tuple[list[dict[str, Any]], int]:
    by_id: dict[int, dict[str, Any]] = {}
    unpublished = 0
    for music in musics:
        song_id = _positive_integer(music.get("id"), "歌曲 ID")
        if song_id in by_id:
            raise ValueError("master 歌曲 ID 重复")
        published = music.get("publishedAt")
        if isinstance(published, bool) or not isinstance(published, int) or not isinstance(music.get("title"), str):
            raise ValueError(f"歌曲 {song_id} 缺少标题或发布时间")
        by_id[song_id] = {"music": music, "charts": []}
        if published > now_ms:
            unpublished += 1
    seen: set[tuple[int, str]] = set()
    for metadata in difficulties:
        song_id = _positive_integer(metadata.get("musicId"), "谱面歌曲 ID")
        difficulty = metadata.get("musicDifficulty")
        if difficulty not in DIFFICULTIES:
            raise ValueError(f"master 数据出现未知难度：{difficulty}")
        if song_id not in by_id or (song_id, difficulty) in seen:
            raise ValueError("master 谱面的歌曲引用无效或难度重复")
        seen.add((song_id, difficulty))
        _positive_integer(metadata.get("playLevel"), "难度等级")
        _positive_integer(metadata.get("totalNoteCount"), "音符总数")
        if difficulty in selected_difficulties:
            by_id[song_id]["charts"].append(dict(metadata))
    entries = [entry for entry in by_id.values() if entry["music"]["publishedAt"] <= now_ms]
    return sorted(entries, key=lambda entry: entry["music"]["id"]), unpublished


def _sync_song(root: Path, catalog_entry: dict[str, Any], client: HttpClient, *, force: bool,
               asset_root: str, stop_requested: Callable[[], bool]) -> dict[str, Any]:
    _check_stop(stop_requested)
    music = catalog_entry["music"]
    song_id = music["id"]
    song = {"id": song_id, "title": music["title"], "metadata": music,
            "charts": {}, "jacket": None, "errors": []}
    directory = Path("sekai-jp") / str(song_id)
    for metadata in sorted(catalog_entry["charts"], key=lambda item: DIFFICULTIES.index(item["musicDifficulty"])):
        _check_stop(stop_requested)
        difficulty = metadata["musicDifficulty"]
        url = f"{asset_root}/music/music_score/{song_id:04d}_01/{difficulty}.txt"
        entry, error = _sync_asset(root, directory, difficulty, "sus", url, client, validate_sus, force=force)
        if entry is not None:
            song["charts"][difficulty] = {**entry, "music_id": song_id, "difficulty": difficulty,
                                         "play_level": metadata["playLevel"], "total_note_count": metadata["totalNoteCount"]}
        if error:
            song["errors"].append({"kind": "chart", "difficulty": difficulty, "source_url": url, "message": error})
    if not catalog_entry["charts"]:
        song["errors"].append({"kind": "metadata", "message": "这首歌曲没有所选难度的谱面元数据"})
    bundle = music.get("assetbundleName", "")
    # assetbundleName 是封面名，不能用它拼接 music_score 的目录。
    if not isinstance(bundle, str) or not re.fullmatch(r"jacket_s_[0-9]+", bundle):
        song["errors"].append({"kind": "jacket", "message": "封面资源名无效"})
    else:
        _check_stop(stop_requested)
        url = f"{asset_root}/music/jacket/{bundle}/{bundle}.png"
        song["jacket"], error = _sync_asset(root, directory, "jacket", "png", url, client, _validate_jacket, force=force)
        if error:
            song["errors"].append({"kind": "jacket", "source_url": url, "message": error})
    return song


def _summarize(songs: list[dict[str, Any]], *, indexed: int, unpublished: int) -> dict[str, int]:
    assets = [chart for song in songs for chart in song["charts"].values()]
    jackets = [song["jacket"] for song in songs if song["jacket"]]
    statuses = [asset["status"] for asset in [*assets, *jackets]]
    return {"indexed_songs": indexed, "stored_songs": len(songs),
            "songs_with_charts": sum(bool(song["charts"]) for song in songs),
            "charts": len(assets), "jackets": len(jackets), "skipped_unpublished": unpublished,
            "updated": statuses.count("updated"), "unchanged": statuses.count("unchanged"),
            "stale": statuses.count("stale"), "recoverable_errors": sum(len(song["errors"]) for song in songs),
            "fatal_errors": 0}


def sync_catalog(output_root: Path, *, client: HttpClient | None = None, workers: int = 6,
                 force: bool = False, difficulties: tuple[str, ...] = DIFFICULTIES,
                 song_ids: tuple[int, ...] | None = None, progress: Progress | None = None,
                 stop_requested: Callable[[], bool] | None = None,
                 asset_root: str = ASSET_ROOT) -> dict[str, Any]:
    if not 1 <= workers <= 12 or not difficulties or set(difficulties) - set(DIFFICULTIES):
        raise ValueError("并发数须为 1–12，难度须为日服已有难度")
    stop = stop_requested or (lambda: False)
    client = client or HttpClient(stop_requested=stop)
    report = progress or (lambda _message: None)
    root = Path(output_root).resolve()
    with catalog_lock(root):
        _check_stop(stop)
        report("读取日服 master 数据最新版本…")
        revision = _latest_revision(client)
        report(f"固定 master 快照 {revision[:12]}，读取歌曲及难度目录…")
        musics, music_body, music_url = _fetch_index(client, revision, "musics")
        difficulty_metadata, difficulty_body, difficulty_url = _fetch_index(client, revision, "musicDifficulties")
        entries, unpublished = _catalog_entries(musics, difficulty_metadata, int(time.time() * 1000), difficulties)
        all_entry_ids = {entry["music"]["id"] for entry in entries}
        if song_ids is not None:
            if not song_ids or any(song_id not in all_entry_ids for song_id in song_ids):
                raise ValueError("指定歌曲不存在或尚未公开")
            entries = [entry for entry in entries if entry["music"]["id"] in song_ids]
        if not entries:
            raise ValueError("日服 master 数据中没有可同步的已公开歌曲")
        master = {}
        for name, body, url in (("musics", music_body, music_url), ("musicDifficulties", difficulty_body, difficulty_url)):
            digest = _sha256(body)
            relative = Path("master") / f"{name}-{digest}.json"
            _write_atomic(root / relative, body)
            master[name] = {"path": relative.as_posix(), "sha256": digest, "source_url": url}
        report(f"开始同步 {len(entries)} 首歌曲，跳过 {unpublished} 首未公开歌曲；难度：{', '.join(difficulties)}")
        songs = []
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(_sync_song, root, entry, client, force=force, asset_root=asset_root,
                                       stop_requested=stop) for entry in entries]
            try:
                for future in as_completed(futures):
                    _check_stop(stop)
                    song = future.result()
                    songs.append(song)
                    report(f"[{len(songs)}/{len(entries)}] {song['id']} {song['title']} · "
                           f"谱面 {len(song['charts'])}，封面 {int(song['jacket'] is not None)}，错误 {len(song['errors'])}")
                    for error in song["errors"]:
                        report(f"  失败 {song['id']} {error.get('difficulty', error['kind'])}：{error['message']}")
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        _check_stop(stop)
        songs.sort(key=lambda song: song["id"])
        manifest = {"schema_version": SCHEMA_VERSION, "game": "project-sekai", "server": "jp",
                    "generated_at": _utc_now(), "source": {"repository": "Sekai-World/sekai-master-db-diff",
                    "revision": revision, "asset_root": asset_root, "runtime_network_access": False},
                    "scope": {"difficulties": list(difficulties), "song_ids": list(song_ids) if song_ids is not None else None},
                    "master": master, "summary": _summarize(songs, indexed=len(musics), unpublished=unpublished),
                    "songs": songs}
        # 指定歌曲的验证快照独立保存，不能把全库 manifest 缩成少数歌曲。
        destination = root / ("manifest.json" if song_ids is None and difficulties == DIFFICULTIES else "manifest-selection.json")
        _write_json(destination, manifest)
        summary = manifest["summary"]
        report(f"同步结束：{summary['stored_songs']} 首 / {summary['charts']} 张谱面 / {summary['jackets']} 个封面；"
               f"更新 {summary['updated']}，未变化 {summary['unchanged']}，旧版 {summary['stale']}，错误 {summary['recoverable_errors']}")
        if summary["recoverable_errors"]:
            report("未更新成功的资源（完整错误记录见本地 manifest）：")
            for song in songs:
                for error in song["errors"]:
                    report(f"  {song['id']} {song['title']} {error.get('difficulty', error['kind'])}：{error['message']}")
        return manifest


class ChartRepository:
    """只从 manifest 加载原始 SUS；不访问网络，也不负责谱面演奏与触控。"""

    def __init__(self, root: Path, *, manifest_name: str = "manifest.json") -> None:
        self.root = Path(root).resolve()
        self.manifest = _read_json(_safe_local_path(self.root, manifest_name))
        if (self.manifest.get("schema_version") != SCHEMA_VERSION or self.manifest.get("server") != "jp"
                or self.manifest.get("game") != "project-sekai"):
            raise ValueError("本地谱面库版本或区服不匹配")
        self.songs = {song["id"]: song for song in self.manifest["songs"]}

    def load_chart(self, song_id: int, difficulty: str) -> str:
        if difficulty not in DIFFICULTIES:
            raise ValueError("谱面难度无效")
        entry = self.songs[song_id]["charts"][difficulty]
        if entry.get("music_id") != song_id or entry.get("difficulty") != difficulty:
            raise ValueError("谱面歌曲或难度不匹配")
        path = _safe_local_path(self.root, entry["path"])
        if path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError("本地谱面超出允许大小")
        body = path.read_bytes()
        if len(body) != entry["size"] or _sha256(body) != entry["sha256"]:
            raise ValueError("本地谱面 SHA256 校验失败")
        validate_sus(body)
        return body.decode("utf-8-sig")
