"""便携发行包的校验、准备与可回退更新；不依赖游戏控制器。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile

REPOSITORY = "coatcn1/MaaPJSK"
MANIFEST = "package-manifest.json"
PACKAGE = "MaaPJSK"
PRESERVED = {"config", "profiles", "debug", "logs", "log", "screencap", "runtime", ".update", ".git", ".local"}
MANAGED_ROOTS = {"agent", "project_sekai", "resource", "scripts", "packaging", "libs", "runtimes", "plugins", "licenses", "docs", "cs", "de", "en-us", "es", "fr", "it", "ja", "ja-jp", "ko", "pl", "pt-br", "ru", "tr", "zh-hans", "zh-hant"}
ROOT_FILES = {"interface.json", "runtime-compatibility.json", "build-info.json", "third-party-notices.md", "readme.md", "maapjsk.cmd", "maapjsk.exe", "maapjsk.dll", "maapjsk.deps.json", "maapjsk.runtimeconfig.json", "libloader.dll", "cliff.toml", "license"}
DOTNET_ROOT_FILES = {"clrjit.dll", "coreclr.dll", "hostfxr.dll", "hostpolicy.dll", "system.collections.dll", "system.io.filesystem.dll", "system.memory.dll", "system.private.corelib.dll", "system.runtime.dll", "system.runtime.extensions.dll", "system.runtime.interopservices.dll", "system.runtime.interopservices.runtimeinformation.dll", "system.runtime.loader.dll"}
ROOT_FILES |= DOTNET_ROOT_FILES
VERSION = re.compile(r"^\d+\.\d+\.\d+$")
PYTHON_DEPENDENCIES = ["python", "maafw", "numpy", "opencv-python", "onnxruntime", "PyYAML"]


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        value = hashlib.file_digest(stream, "sha256")
    return value.hexdigest()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def checked_name(name: str, *, managed: bool = True) -> str:
    # Windows 会忽略尾随点／空格，并把冒号解释为数据流；与 ZIP 路径遍历一起拒绝。
    if "\\" in name or any(ord(character) < 32 or character in '<>"|?*' for character in name):
        raise ValueError("非法包路径")
    parts = PurePosixPath(name).parts
    if not parts or name.startswith("/") or any(p in {".", ".."} or ":" in p or p.rstrip(" .") != p for p in parts):
        raise ValueError(f"非法包路径：{name}")
    for part in parts:
        if re.match(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)", part, re.I):
            raise ValueError("禁止 Windows 保留文件名")
    normalized = "/".join(parts)
    if normalized != name:
        raise ValueError("包路径必须使用规范相对路径")
    if managed:
        first = parts[0].lower()
        if first in PRESERVED or name.lower() in {"appsettings.json", MANIFEST} or name.lower().startswith("resource/charts/") or name.lower() == "resource/charts":
            raise ValueError(f"更新不得覆盖用户数据：{name}")
        if (len(parts) == 1 and first not in ROOT_FILES) or (len(parts) > 1 and first not in MANAGED_ROOTS):
            raise ValueError(f"文件不在发布白名单中：{name}")
    return normalized


def target_path(root: Path, name: str, *, managed: bool = True) -> Path:
    checked_name(name, managed=managed)
    target = root / name
    # 既有用户目录若为链接／junction，不沿链接写入其他目录。
    for path in [root, *[parent for parent in target.parents if parent.is_relative_to(root)], target]:
        if path.is_symlink() or (os.name == "nt" and path.exists() and path.stat().st_file_attributes & 0x400):
            raise ValueError("更新目标包含链接或重解析点")
    if not target.resolve().is_relative_to(root.resolve()):
        raise ValueError("更新目标越过安装目录")
    return target


def validate_manifest(value: dict) -> dict:
    if value.get("schema") != 1 or value.get("package") != PACKAGE or value.get("architecture") != "win-x64" or not VERSION.fullmatch(str(value.get("version", ""))):
        raise ValueError("发行包身份或版本无效")
    files = value.get("managed_files")
    if not isinstance(files, dict) or not files:
        raise ValueError("缺少受控文件清单")
    seen = set()
    for name, checksum in files.items():
        checked_name(name)
        if name.lower() in seen or not re.fullmatch(r"[0-9a-f]{64}", str(checksum)):
            raise ValueError("受控文件重复或校验值无效")
        seen.add(name.lower())
    return value


def verify_sidecar(archive: Path, sidecar: Path) -> str:
    text = sidecar.read_text(encoding="utf-8-sig").strip()
    match = re.fullmatch(r"([0-9a-fA-F]{64})(?:\s+\*?([^\r\n]+))?", text)
    if not match or (match[2] and match[2] != archive.name):
        raise ValueError("SHA256 sidecar 格式或文件名不匹配")
    actual = digest(archive)
    if actual != match[1].lower():
        raise ValueError("发行包 SHA256 不匹配")
    return actual


def validate_compatibility(value: dict) -> dict:
    if (not isinstance(value, dict) or type(value.get("schema")) is not int or value["schema"] != 1
            or value.get("platform") != "win-x64"
            or any(not isinstance(value.get(key), str) or not value[key].strip() for key in PYTHON_DEPENDENCIES)):
        raise ValueError("运行环境兼容清单 schema、平台或依赖字段无效")
    return value


def extract_update(archive: Path, destination: Path) -> dict:
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 20000 or sum(x.file_size for x in entries) > 4 * 1024**3:
            raise ValueError("发行包超过允许大小")
        seen = set()
        for entry in entries:
            name = entry.filename
            if name.lower() in seen or entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError("包包含重复文件、目录条目或符号链接")
            seen.add(name.lower())
            checked_name(name, managed=name != MANIFEST)
        value = validate_manifest(json.loads(package.read(MANIFEST)))
        if set(x.filename for x in entries) != set(value["managed_files"]) | {MANIFEST}:
            raise ValueError("ZIP 内容与受控清单不一致")
        for name, checksum in value["managed_files"].items():
            target = destination / name
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.open(name) as source, target.open("wb") as output:
                shutil.copyfileobj(source, output)
            if digest(target) != checksum:
                raise ValueError(f"受控文件 SHA256 不匹配：{name}")
    write_json(destination / MANIFEST, value)
    return value


def prepare_update(root: Path, archive: Path, sidecar: Path, *, expected_version: str | None = None) -> Path:
    root = root.resolve()
    current = validate_manifest(json.loads((root / MANIFEST).read_text(encoding="utf-8-sig")))
    checksum = verify_sidecar(archive, sidecar)
    stage = Path(tempfile.mkdtemp(prefix="maapjsk-update-"))
    try:
        value = extract_update(archive, stage / "payload")
        if expected_version and value["version"] != expected_version:
            raise ValueError("下载标签与包内版本不一致")
        compatibility = "runtime-compatibility.json"
        if compatibility in current["managed_files"] or compatibility in value["managed_files"]:
            before = validate_compatibility(json.loads((root / compatibility).read_text(encoding="utf-8-sig")))
            after = validate_compatibility(json.loads((stage / "payload" / compatibility).read_text(encoding="utf-8-sig")))
            if any(before[key] != after[key] for key in PYTHON_DEPENDENCIES):
                raise ValueError("便携 Python 依赖变更，请使用新的完整包")
        if tuple(map(int, value["version"].split("."))) <= tuple(map(int, current["version"].split("."))):
            raise ValueError("只允许更新到更高版本")
        for name in value["managed_files"]:
            target_path(root, name)
        plan = stage / "plan.json"
        write_json(plan, {"schema": 1, "root": str(root), "payload": str(stage / "payload"), "archive_sha256": checksum, "previous_manifest": current, "manifest": value})
        # helper 使用独立副本，不能在覆盖自身源文件后继续运行旧／新混合模块。
        shutil.copyfile(__file__, stage / "release_update.py")
        shutil.copyfile(root / "scripts/apply-release-update.ps1", stage / "apply-release-update.ps1")
        return plan
    except BaseException:
        shutil.rmtree(stage)
        raise


def apply_update(plan_path: Path) -> dict:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    root = Path(plan["root"]).resolve()
    payload = Path(plan["payload"])
    value = validate_manifest(plan["manifest"])
    old = validate_manifest(plan["previous_manifest"])
    if os.name == "nt":
        # 命令行入口同样不能绕过 GUI／Agent 退出门槛；路径只作为 JSON stdin 传递。
        script = "$r = [Console]::In.ReadToEnd() | ConvertFrom-Json; @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $r.pid -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($r.root + '\\', [StringComparison]::OrdinalIgnoreCase) }).Count"
        result = subprocess.run(["powershell.exe", "-NoProfile", "-Command", script], input=json.dumps({"root": str(root), "pid": os.getpid()}), text=True, capture_output=True, check=True)
        if result.stdout.strip() != "0":
            raise RuntimeError("安装目录还有运行中的 GUI 或 Agent，拒绝覆盖")
    if json.loads((root / MANIFEST).read_text(encoding="utf-8-sig")) != old:
        raise ValueError("安装版本已变更，请重新准备更新")
    for name, checksum in value["managed_files"].items():
        target_path(root, name)
        if digest(payload / name) != checksum:
            raise ValueError("准备后的文件被修改，拒绝更新")
    backup = plan_path.parent / "backup"
    written = []
    partials = []
    try:
        for name in value["managed_files"]:
            target = target_path(root, name)
            exists = target.is_file()
            if exists:
                saved = backup / name
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, saved)
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".update-partial")
            with temporary.open("xb") as output, (payload / name).open("rb") as source:
                partials.append(temporary)
                shutil.copyfileobj(source, output)
            os.replace(temporary, target)
            partials.remove(temporary)
            written.append((name, exists))
        # 清单是安装版本的唯一提交点；所有受控文件成功后才替换它。
        installed = dict(value)
        installed["python_archive_sha256"] = old.get("python_archive_sha256", value.get("python_archive_sha256"))
        write_json(root / MANIFEST, installed)
    except BaseException:
        for temporary in partials:
            temporary.unlink(missing_ok=True)
        for name, existed in reversed(written):
            target = root / name
            if existed:
                os.replace(backup / name, target)
            else:
                target.unlink(missing_ok=True)
        raise
    return {"updated": True, "version": value["version"], "files": len(written)}


def request(url: str) -> tuple[bytes, str]:
    query = urllib.request.Request(url, headers={"User-Agent": "MaaPJSK-release-updater", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(query, timeout=60) as response:
        return response.read(), response.url


def discover_release() -> dict:
    # REST 限流或临时故障只改变元数据入口，资产仍严格取本项目 GitHub HTTPS。
    try:
        body, _ = request(f"https://api.github.com/repos/{REPOSITORY}/releases/latest")
        value = json.loads(body)
        tag = value["tag_name"]
        assets = {x["name"]: x["browser_download_url"] for x in value["assets"]}
    except (urllib.error.URLError, ValueError, KeyError):
        _, latest = request(f"https://github.com/{REPOSITORY}/releases/latest")
        tag = urllib.parse.unquote(latest.rsplit("/", 1)[-1])
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            raise ValueError("GitHub latest 未返回有效稳定标签")
        html, _ = request(f"https://github.com/{REPOSITORY}/releases/expanded_assets/{tag}")
        import html as html_module
        links = re.findall(r'href="([^\"]+)"', html.decode("utf-8"))
        assets = {urllib.parse.unquote(x.rsplit("/", 1)[-1]): urllib.parse.urljoin("https://github.com", html_module.unescape(x)) for x in links}
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        raise ValueError("无有效稳定版本")
    name = f"{PACKAGE}-{tag}-win-x64-update.zip"
    selected = {"version": tag[1:], "name": name, "archive_url": assets[name], "checksum_url": assets[name + ".sha256"]}
    prefix = f"https://github.com/{REPOSITORY}/releases/download/{tag}/"
    if any(not selected[key].startswith(prefix) or selected[key] != prefix + suffix for key, suffix in [("archive_url", name), ("checksum_url", name + ".sha256")]):
        raise ValueError("资产地址与项目／标签／架构不匹配")
    return selected


def main() -> None:
    parser = argparse.ArgumentParser(description="MaaPJSK 安全发行更新")
    parser.add_argument("command", choices=["check", "prepare", "apply"])
    parser.add_argument("--root", type=Path)
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--sidecar", type=Path)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.command == "check":
        result = discover_release()
    elif args.command == "apply":
        result = apply_update(args.plan)
    else:
        if args.archive:
            if not args.sidecar:
                raise ValueError("本地更新必须提供 SHA256 sidecar")
            plan = prepare_update(args.root, args.archive, args.sidecar)
        else:
            release = discover_release()
            with tempfile.TemporaryDirectory(prefix="maapjsk-download-") as folder:
                archive = Path(folder) / release["name"]
                sidecar = archive.with_name(archive.name + ".sha256")
                archive.write_bytes(request(release["archive_url"])[0])
                sidecar.write_bytes(request(release["checksum_url"])[0])
                plan = prepare_update(args.root, archive, sidecar, expected_version=release["version"])
        result = {"prepared": True, "plan": str(plan)}
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
