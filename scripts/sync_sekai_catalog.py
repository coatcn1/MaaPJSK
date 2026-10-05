"""显式同步日服谱面库，或只读查看本地更新状态。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from project_sekai.chart_catalog import DIFFICULTIES, HttpClient, sync_catalog


def main() -> int:
    parser = argparse.ArgumentParser(description="增量更新日服 SUS 谱面与封面；演出期间不运行此维护操作")
    parser.add_argument("--output-root", type=Path, default=PROJECT_ROOT / "resource" / "charts")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--timeout", type=float, default=20)
    parser.add_argument("--retries", type=int, default=2)
    refresh_mode = parser.add_mutually_exclusive_group()
    refresh_mode.add_argument("--check-recent", action="store_true", help="联网检查近三个月的资源，未变资源使用 HTTP 条件缓存")
    refresh_mode.add_argument("--force", action="store_true", help="显式强制重新获取所有资源，包括三个月以前的资源，不使用条件缓存")
    parser.add_argument("--song-ids", type=int, nargs="+", help="只验证指定歌曲，写入独立的 manifest-selection.json")
    parser.add_argument("--difficulties", choices=DIFFICULTIES, nargs="+", default=list(DIFFICULTIES))
    parser.add_argument("--status", action="store_true", help="只读本地 manifest，不访问网络")
    args = parser.parse_args()
    if args.status:
        manifest_path = args.output_root / "manifest.json"
        if not manifest_path.exists():
            print("本地尚未同步日服谱面")
            return 0
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        print(json.dumps({"generated_at": manifest.get("generated_at"), "source": manifest.get("source"),
                          "summary": manifest.get("summary")}, ensure_ascii=False, indent=2))
        return 0
    try:
        manifest = sync_catalog(args.output_root, client=HttpClient(timeout=args.timeout, retries=args.retries),
                                workers=args.workers, force=args.force, check_recent=args.check_recent,
                                difficulties=tuple(args.difficulties),
                                song_ids=tuple(args.song_ids) if args.song_ids else None,
                                progress=lambda message: print(message, flush=True))
        return 2 if manifest["summary"]["recoverable_errors"] else 0
    except (KeyboardInterrupt, InterruptedError):
        print("已取消同步；原有 manifest 保持不变，已校验的下载可继续复用", file=sys.stderr, flush=True)
        return 130
    except (OSError, ValueError, RuntimeError) as error:
        print(f"谱面同步失败：{error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
