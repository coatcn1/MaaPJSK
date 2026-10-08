"""发布内容白名单与首启配置；构建不读取用户任务状态。"""
from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

from .release_update import MANIFEST, checked_name, digest, validate_manifest, write_json

TEMPLATE_CONFIGS = {
    ".local/config.json": "maapjsk-templates.json",
    ".local/cooperative-templates/config.json": "cooperative-templates.json",
    ".local/ad-rewards-templates/config.json": "ad-rewards-templates.json",
    ".local/game-timing-templates/config.json": "game-timing-templates.json",
}
MODEL_HASHES = {
    "inference.onnx": "da72dc72ca4dc220df0dfde68c1dedc31c58d3e76a25871122e5056227d50092",
    "inference.yml": "5dfeb2777f6d0db8177d8128a8acfcf6e6276dc4ac73ea3bf0dc06d6a5e85d8e",
}


def copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


def add_templates(source: Path, destination: Path) -> dict:
    config = json.loads(source.read_text(encoding="utf-8-sig"))
    references = list(config.get("templates", {}).values())
    references += [item["path"] for item in config.get("anchors", {}).values()]
    if not references:
        raise ValueError("模板配置没有引用")
    hashes = {}
    for name in references:
        checked_name(name, managed=False)
        template = source.parent / name
        if template.is_symlink() or not template.resolve().is_relative_to(source.parent.resolve()):
            raise ValueError("模板来源越界")
        # 发布只接收字形／按钮裁剪，不允许把完整玩家画面误打包。
        import cv2
        image = cv2.imread(str(template))
        if image is None or image.shape[0] * image.shape[1] >= 1280 * 720 // 2:
            raise ValueError(f"模板不存在或不是小裁剪：{name}")
        checksum = digest(template)
        for item in config.get("anchors", {}).values():
            if item["path"] == name and item.get("sha256") != checksum:
                raise ValueError("模板元数据 SHA256 不匹配")
        target = destination / name
        if target.exists() and digest(target) != checksum:
            raise ValueError("不同模板配置引用了冲突文件名")
        copy(template, target)
        hashes[name] = checksum
    return {"config": config, "hashes": hashes}


def bootstrap(root: Path) -> None:
    from .performance_settings import PerformanceSettings
    root = root.resolve()
    validate_manifest(json.loads((root / MANIFEST).read_text(encoding="utf-8-sig")))
    defaults = root / "packaging/defaults"
    for source in defaults.rglob("*"):
        if source.is_file():
            name = source.relative_to(defaults).as_posix()
            checked_name(name, managed=False)
            destination = root / "config" / name
            if not destination.exists():
                copy(source, destination)
    settings = root / "appsettings.json"
    if not settings.exists():
        write_json(settings, {"NoAutoStart": "True", "DownloadSourceIndex": "0"})
    performance = root / "config/performance-settings.json"
    if not performance.exists():
        write_json(performance, asdict(PerformanceSettings()))
    # 安装目录内的曲库随改名重定位，明确外部曲库及其清单保持用户原路径。
    charts = root / "resource/charts"
    sync_path = root / "config/chart-sync.json"
    previous = json.loads(sync_path.read_text(encoding="utf-8-sig")) if sync_path.exists() else {}
    output_root = previous.get("output_root", str(charts))
    manifest_path = previous.get("manifest_path", str(charts / "manifest.json"))
    old_working = previous.get("working_directory")
    if old_working and previous.get("output_root"):
        old_root = Path(old_working).resolve()
        old_output = Path(output_root)
        if not old_output.is_absolute():
            old_output = old_root / old_output
        if old_output.resolve().is_relative_to(old_root):
            output_root = str(root / old_output.resolve().relative_to(old_root))
            old_manifest = Path(manifest_path)
            if not old_manifest.is_absolute():
                old_manifest = old_root / old_manifest
            if old_manifest.resolve().is_relative_to(old_root):
                manifest_path = str(root / old_manifest.resolve().relative_to(old_root))
    previous.update({
        "child_exec": str(root / "runtime/python/python.exe"),
        "script_path": str(root / "scripts/sync_sekai_catalog.py"),
        "working_directory": str(root), "output_root": output_root,
        "manifest_path": manifest_path,
    })
    write_json(sync_path, previous)


def build(source: Path, mfa: Path, python_archive: Path, output: Path, version: str, *, allow_dirty: bool = False) -> Path:
    status = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=all"], cwd=source, text=True)
    if status and not allow_dirty:
        raise ValueError("正式发行构建要求干净工作树；AllowDirty 仅用于本地 probe")
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    package_name = f"MaaPJSK-v{version}-win-x64"
    target = output / package_name
    if target.exists():
        raise ValueError("构建目录已存在，请选择新的输出目录，不覆盖既有资产")
    target.mkdir(parents=True)
    for file in mfa.rglob("*"):
        if not file.is_file():
            continue
        relative = file.relative_to(mfa).as_posix()
        if file.suffix.lower() == ".pdb" or relative.lower() in {"appsettings.json", "interface.json"} or relative.lower().startswith("docs/"):
            continue
        try:
            checked_name(relative)
        except ValueError:
            # 上游样例配置、安装脚本和未选择内容不属于发行名单。
            continue
        copy(file, target / relative)
    if not all((target / name).is_file() for name in ["MaaPJSK.exe", "libs/MFAAvalonia.Core.dll", "coreclr.dll", "System.Private.CoreLib.dll"]):
        raise ValueError("需要带项目 overlay 的自包含 MaaPJSK publish 目录")
    proof = json.loads((mfa / "packaging/overlay-build.json").read_text(encoding="utf-8-sig"))
    if proof.get("mfa_ref") != "v2.12.0" or proof.get("overlay_sha256") != digest(source / "mfa-chart-ui/MaaPjskReleaseUpdate.cs") or proof.get("core_sha256") != digest(target / "libs/MFAAvalonia.Core.dll"):
        raise ValueError("发行 overlay 构建证据与当前源码／Core 不符")
    for directory in ["agent", "project_sekai"]:
        for file in (source / directory).glob("*.py"):
            copy(file, target / directory / file.name)
    native = source / "project_sekai/native"
    copy(native / "maapjsk_native.pyd", target / "project_sekai/native/maapjsk_native.pyd")
    for file in (native / "vendor/minitouch").rglob("*"):
        if file.is_file():
            copy(file, target / "project_sekai/native/vendor/minitouch" / file.relative_to(native / "vendor/minitouch"))
    for file in (source / "resource/pipeline").rglob("*.json"):
        copy(file, target / "resource/pipeline" / file.relative_to(source / "resource/pipeline"))
    for file in (source / "resource").glob("*.json"):
        copy(file, target / "resource" / file.name)
    models = source / "resource/models/song_title_ocr"
    for file_name, checksum in MODEL_HASHES.items():
        if digest(models / file_name) != checksum:
            raise ValueError("OCR 模型哈希不匹配")
        copy(models / file_name, target / "resource/models/song_title_ocr" / file_name)
    copy(source / "packaging/PP-OCRv5-NOTICE.md", target / "resource/models/song_title_ocr/NOTICE.md")
    template_hashes = {}
    for config_name, published_name in TEMPLATE_CONFIGS.items():
        result = add_templates(source / config_name, target / "packaging/defaults")
        write_json(target / "packaging/defaults" / published_name, result["config"])
        template_hashes.update(result["hashes"])
    write_json(target / "packaging/template-hashes.json", template_hashes)
    interface = json.loads((source / "interface.json").read_text(encoding="utf-8-sig"))
    if interface["version"] != version:
        raise ValueError("interface 版本与构建版本不同")
    interface["agent"]["child_exec"] = "{PROJECT_DIR}/runtime/python/python.exe"
    write_json(target / "interface.json", interface)
    for script in ["start-release.ps1", "apply-release-update.ps1", "update-release.py", "sync_sekai_catalog.py"]:
        copy(source / "scripts" / script, target / "scripts" / script)
    copy(source / "packaging/MaaPJSK.cmd", target / "MaaPJSK.cmd")
    for name in ["runtime-compatibility.json", "README.md", "THIRD-PARTY-NOTICES.md"]:
        copy(source / name, target / name)
    for name in ["release-package.md", "release-notes-v0.8.0.md"]:
        copy(source / "docs" / name, target / "docs" / name)
    for file in (source / "packaging/licenses").glob("*"):
        if file.is_file():
            copy(file, target / "licenses" / file.name)
    copy(source / "third_party/maabangdream/LICENSE", target / "licenses/MaaBanGDream-PolyForm.txt")
    copy(native / "vendor/minitouch/LICENSE", target / "licenses/minitouch.txt")
    if not {"MFAAvalonia-GPL-3.0.txt", "MaaFramework-LGPL-3.0.txt", "PaddleOCR-Apache-2.0.txt"} <= {p.name for p in (target / "licenses").iterdir()}:
        raise ValueError("缺少发行许可证原文")
    copy(python_archive, target / "runtime/maapjsk-python.zip")
    write_json(target / "BUILD-INFO.json", {"schema": 1, "version": version, "source_commit": commit, "mfa_source_commit": "7cb1e404", "dirty_probe": bool(status), "python_archive_sha256": digest(python_archive)})
    files = {p.relative_to(target).as_posix(): digest(p) for p in sorted(target.rglob("*")) if p.is_file() and not p.relative_to(target).as_posix().startswith("runtime/")}
    value = validate_manifest({"schema": 1, "package": "MaaPJSK", "architecture": "win-x64", "version": version, "source_commit": commit, "python_archive_sha256": digest(python_archive), "managed_files": files})
    write_json(target / MANIFEST, value)
    for suffix, selected in [(".zip", [p for p in target.rglob("*") if p.is_file()]), ("-update.zip", [target / file for file in files] + [target / MANIFEST])]:
        archive = output / (package_name + suffix)
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as package:
            for file in sorted(selected):
                # 固定 ZIP 时间戳，内容相同即可重复生成相同校验值。
                item = zipfile.ZipInfo(file.relative_to(target).as_posix(), (2026, 1, 1, 0, 0, 0))
                item.compress_type = zipfile.ZIP_DEFLATED
                package.writestr(item, file.read_bytes())
        archive.with_name(archive.name + ".sha256").write_text(f"{digest(archive)}  {archive.name}\n", encoding="ascii")
    return target


def check_package(root: Path) -> dict:
    value = validate_manifest(json.loads((root / MANIFEST).read_text(encoding="utf-8-sig")))
    for name, checksum in value["managed_files"].items():
        if digest(root / name) != checksum:
            raise ValueError(f"发行文件与清单不符：{name}")
    required = {"MaaPJSK.exe", "MaaPJSK.cmd", "scripts/start-release.ps1", "scripts/update-release.py",
                "scripts/apply-release-update.ps1", "libs/MFAAvalonia.Core.dll", "project_sekai/native/maapjsk_native.pyd",
                "resource/models/song_title_ocr/inference.onnx", "resource/models/song_title_ocr/inference.yml",
                "coreclr.dll", "System.Private.CoreLib.dll"}
    if not required <= set(value["managed_files"]):
        raise ValueError("发行包缺少必需文件")
    for name in value["managed_files"]:
        if name.endswith(".pdb") or "__pycache__" in name:
            raise ValueError("发行包含调试／缓存内容")
    archive = root / "runtime/maapjsk-python.zip"
    if not archive.is_file() or digest(archive) != value["python_archive_sha256"]:
        raise ValueError("完整包 Python ZIP 不符")
    interface = json.loads((root / "interface.json").read_text(encoding="utf-8"))
    if interface["version"] != value["version"] or interface["agent"]["child_exec"] != "{PROJECT_DIR}/runtime/python/python.exe":
        raise ValueError("发行接口版本或 Python 路径无效")
    for directory in ["config", "profiles", "debug", "logs", "resource/charts"]:
        if (root / directory).exists():
            raise ValueError("未启动发行目录携带用户状态")
    return {"release_validation": "passed", "version": value["version"], "managed_files": len(value["managed_files"])}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["build", "bootstrap"])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--mfa", type=Path)
    parser.add_argument("--python-archive", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--version", default="0.8.0")
    parser.add_argument("--allow-dirty", action="store_true")
    args = parser.parse_args()
    if args.command == "bootstrap":
        bootstrap(args.root)
    else:
        print(build(args.root, args.mfa, args.python_archive, args.output, args.version, allow_dirty=args.allow_dirty))


if __name__ == "__main__":
    main()
