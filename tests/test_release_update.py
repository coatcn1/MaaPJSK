import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

from project_sekai import release_update as release
from project_sekai.release_package import bootstrap
from project_sekai import release_package


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name) / "中文 空格旧目录"
        self.root.mkdir()
        (self.root / "scripts").mkdir()
        (self.root / "scripts/apply-release-update.ps1").write_text("helper", encoding="utf-8")
        self.old = self.manifest("0.8.0", {"agent/server.py": b"old", "libs/core.dll": b"old-core"})
        for name, content in {"agent/server.py": b"old", "libs/core.dll": b"old-core"}.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        release.write_json(self.root / release.MANIFEST, self.old)

    @staticmethod
    def manifest(version, files):
        return {"schema": 1, "package": "MaaPJSK", "architecture": "win-x64", "version": version,
                "python_archive_sha256": "a" * 64,
                "managed_files": {name: hashlib.sha256(content).hexdigest() for name, content in files.items()}}

    def archive(self, files=None, version="0.9.0", extras=None):
        files = files or {"agent/server.py": b"new", "libs/core.dll": b"new-core", "resource/models/ocr.onnx": b"ocr"}
        archive = Path(self.folder.name) / "MaaPJSK-v0.9.0-win-x64-update.zip"
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(release.MANIFEST, json.dumps(self.manifest(version, files)))
            for name, content in files.items():
                package.writestr(name, content)
            for name, content in (extras or {}).items():
                package.writestr(name, content)
        sidecar = archive.with_name(archive.name + ".sha256")
        sidecar.write_text(f"{release.digest(archive)}  {archive.name}\n", encoding="ascii")
        return archive, sidecar

    def prepare(self, **kwargs):
        archive, sidecar = self.archive(**kwargs)
        plan = release.prepare_update(self.root, archive, sidecar)
        self.addCleanup(lambda: __import__("shutil").rmtree(plan.parent, ignore_errors=True))
        return plan

    def apply(self, plan):
        # 进程退出门槛独立测试；这里验证逐文件事务与用户数据保留。
        with patch.object(release.subprocess, "run") as run:
            run.return_value.stdout = "0"
            return release.apply_update(plan)

    def test_update_preserves_users_charts_runtime_and_old_managed(self):
        preserved = ["config/instances/default.json", "config/performance-settings.json", "profiles/user.json",
                     "resource/charts/manifest.json", "runtime/python/python.exe", "debug/report.json", "appsettings.json"]
        for name in preserved:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"unique-user-state")
        (self.root / "agent/old-unused.py").write_bytes(b"keep")
        plan = self.prepare()
        self.assertTrue(self.apply(plan)["updated"])
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"new")
        self.assertEqual((self.root / "agent/old-unused.py").read_bytes(), b"keep")
        for name in preserved:
            self.assertEqual((self.root / name).read_bytes(), b"unique-user-state")
        self.assertEqual(json.loads((self.root / release.MANIFEST).read_text())["version"], "0.9.0")

    def test_second_file_failure_restores_first_and_version(self):
        plan = self.prepare()
        replace = os.replace
        def fail_once(source, target):
            if Path(target) == self.root / "libs/core.dll":
                raise PermissionError("模拟 DLL 被占用")
            return replace(source, target)
        with patch.object(release.os, "replace", side_effect=fail_once):
            with self.assertRaises(PermissionError):
                self.apply(plan)
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"old")
        self.assertEqual((self.root / "libs/core.dll").read_bytes(), b"old-core")
        self.assertEqual(json.loads((self.root / release.MANIFEST).read_text()), self.old)

    def test_manifest_commit_failure_rolls_back_all_files(self):
        plan = self.prepare()
        original = release.write_json
        def fail_marker(path, value):
            if path == self.root / release.MANIFEST:
                raise OSError("模拟清单落盘失败")
            return original(path, value)
        with patch.object(release, "write_json", side_effect=fail_marker):
            with self.assertRaises(OSError): self.apply(plan)
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"old")
        self.assertFalse((self.root / "resource/models/ocr.onnx").exists())
        self.assertEqual(json.loads((self.root / release.MANIFEST).read_text()), self.old)

    def test_post_prepare_tampering_refused_before_any_write(self):
        plan = self.prepare()
        (plan.parent / "payload/agent/server.py").write_bytes(b"tampered")
        with self.assertRaisesRegex(ValueError, "被修改"): self.apply(plan)
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"old")

    def test_preserved_and_traversal_paths_refused(self):
        for name in ["config/user.json", "runtime/python.exe", "resource/charts/user.sus", "appsettings.json", "../escaped", "/absolute", "agent/../../escaped", "agent/x:stream", "agent/CON.txt", "agent/name. ", "agent\\server.py"]:
            with self.subTest(name=name):
                archive, sidecar = self.archive({name: b"bad"})
                with self.assertRaises(ValueError): release.prepare_update(self.root, archive, sidecar)

    def test_unlisted_file_and_symlink_zip_rejected(self):
        archive, sidecar = self.archive(extras={"agent/extra.py": b"extra"})
        with self.assertRaisesRegex(ValueError, "清单不一致"): release.prepare_update(self.root, archive, sidecar)
        with zipfile.ZipFile(archive, "a") as package:
            item = zipfile.ZipInfo("agent/link")
            item.external_attr = (stat.S_IFLNK | 0o777) << 16
            package.writestr(item, "../outside")
        sidecar.write_text(release.digest(archive), encoding="ascii")
        with self.assertRaisesRegex(ValueError, "符号链接"): release.prepare_update(self.root, archive, sidecar)

    def test_checksum_wrong_asset_and_downgrade_rejected(self):
        archive, sidecar = self.archive()
        sidecar.write_text("0" * 64, encoding="ascii")
        with self.assertRaisesRegex(ValueError, "SHA256 不匹配"): release.prepare_update(self.root, archive, sidecar)
        sidecar.write_text(release.digest(archive) + "  other.zip", encoding="ascii")
        with self.assertRaisesRegex(ValueError, "文件名不匹配"): release.prepare_update(self.root, archive, sidecar)
        archive, sidecar = self.archive(version="0.8.0")
        with self.assertRaisesRegex(ValueError, "更高版本"): release.prepare_update(self.root, archive, sidecar)

    def test_expected_tag_must_equal_manifest(self):
        archive, sidecar = self.archive()
        with self.assertRaisesRegex(ValueError, "标签"): release.prepare_update(self.root, archive, sidecar, expected_version="0.9.1")

    def test_update_cannot_change_preserved_python_dependencies(self):
        name = "runtime-compatibility.json"
        compatible = self.compatibility()
        before = json.dumps(compatible).encode()
        after = json.dumps({**compatible, "python": "3.13.0"}).encode()
        (self.root / name).write_bytes(before)
        self.old["managed_files"][name] = hashlib.sha256(before).hexdigest()
        release.write_json(self.root / release.MANIFEST, self.old)
        archive, sidecar = self.archive({"agent/server.py": b"new", name: after})
        with self.assertRaisesRegex(ValueError, "新的完整包"): release.prepare_update(self.root, archive, sidecar)
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"old")

    @staticmethod
    def compatibility():
        return {"schema": 1, "platform": "win-x64", "python": "3.12.13", "maafw": "5.10.2", "numpy": "2.5.1",
                "opencv-python": "4.13.0.92", "onnxruntime": "1.29.0", "PyYAML": "6.0.3"}

    def test_update_rejects_unknown_compatibility_schema_platform_and_missing_dependency(self):
        name = "runtime-compatibility.json"
        compatible = self.compatibility()
        before = json.dumps(compatible).encode()
        (self.root / name).write_bytes(before)
        self.old["managed_files"][name] = hashlib.sha256(before).hexdigest()
        release.write_json(self.root / release.MANIFEST, self.old)
        for changed in [{**compatible, "schema": 2}, {**compatible, "platform": "win-arm64"},
                        {key: value for key, value in compatible.items() if key != "numpy"}]:
            with self.subTest(changed=changed):
                archive, sidecar = self.archive({"agent/server.py": b"new", name: json.dumps(changed).encode()})
                with self.assertRaisesRegex(ValueError, "schema"):
                    release.prepare_update(self.root, archive, sidecar)
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"old")

    def test_windows_apply_rejects_remaining_process(self):
        if os.name != "nt": self.skipTest("Windows 进程门槛")
        plan = self.prepare()
        with patch.object(release.subprocess, "run") as run:
            run.return_value.stdout = "1"
            with self.assertRaisesRegex(RuntimeError, "运行中"): release.apply_update(plan)
        self.assertEqual((self.root / "agent/server.py").read_bytes(), b"old")

    def test_rest_rate_limit_fallback_selects_only_update_zip_and_sidecar(self):
        tag = "v0.9.0"
        name = f"MaaPJSK-{tag}-win-x64-update.zip"
        base = f"https://github.com/{release.REPOSITORY}/releases/download/{tag}/"
        html = (f'<a href="{base}{name}.sha256">sha</a><a href="{base}{name}">update</a>'
                f'<a href="{base}MaaPJSK-{tag}-win-x64.zip">full</a><a href="{base}MaaPJSK-{tag}-linux-x64-update.zip">linux</a>')
        with patch.object(release, "request", side_effect=[urllib.error.HTTPError("url", 403, "rate", {}, None), (b"", f"https://github.com/{release.REPOSITORY}/releases/tag/{tag}"), (html.encode(), "expanded")]) as request:
            result = release.discover_release()
        self.assertEqual(result["archive_url"], base + name)
        self.assertEqual(result["checksum_url"], base + name + ".sha256")
        self.assertIn("expanded_assets/v0.9.0", request.call_args_list[-1].args[0])

    def test_rest_foreign_asset_refused(self):
        name = "MaaPJSK-v0.9.0-win-x64-update.zip"
        payload = {"tag_name": "v0.9.0", "assets": [{"name": n, "browser_download_url": "https://example.com/" + n} for n in [name, name + ".sha256"]]}
        with patch.object(release, "request", return_value=(json.dumps(payload).encode(), "api")):
            with self.assertRaisesRegex(ValueError, "地址"): release.discover_release()

    def test_bootstrap_rename_rebinds_paths_preserving_user_settings(self):
        defaults = self.root / "packaging/defaults"
        (defaults / "templates").mkdir(parents=True)
        (defaults / "templates/home.png").write_bytes(b"crop")
        (defaults / "maapjsk-templates.json").write_text('{"templates":{"home":"templates/home.png"}}')
        bootstrap(self.root)
        settings = self.root / "appsettings.json"
        performance = self.root / "config/performance-settings.json"
        settings.write_bytes(b'{"DownloadSourceIndex":0,"NoAutoStart":"True","custom":"keep"}')
        performance.write_bytes(b'{"offset":-31,"medicine":5}')
        before = settings.read_bytes(), performance.read_bytes()
        renamed = self.root.with_name("用户手动 改名")
        self.root.rename(renamed)
        bootstrap(renamed)
        sync = json.loads((renamed / "config/chart-sync.json").read_text())
        self.assertEqual(sync["child_exec"], str(renamed / "runtime/python/python.exe"))
        self.assertEqual(sync["output_root"], str(renamed / "resource/charts"))
        self.assertEqual(before, ((renamed / "appsettings.json").read_bytes(), (renamed / "config/performance-settings.json").read_bytes()))

    def test_bootstrap_global_defaults_match_mfa_string_dictionary_and_keep_existing_bytes(self):
        bootstrap(self.root)
        settings_path = self.root / "appsettings.json"
        defaults = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(defaults, {"NoAutoStart": "True", "DownloadSourceIndex": "0"})
        self.assertTrue(all(isinstance(value, str) for value in defaults.values()))
        existing = b'{\r\n  "NoAutoStart": "False", "DownloadSourceIndex": "1", "SavedSetting": "custom"\r\n}\r\n'
        settings_path.write_bytes(existing)
        bootstrap(self.root)
        self.assertEqual(settings_path.read_bytes(), existing)

    def test_bootstrap_rename_keeps_external_library_and_rebinds_service_only(self):
        external = Path(self.folder.name) / "外部曲库"
        sync = {"working_directory": str(self.root), "output_root": str(external),
                "manifest_path": str(external / "custom-manifest.json"), "custom": "retain"}
        release.write_json(self.root / "config/chart-sync.json", sync)
        renamed = self.root.with_name("已改名 新安装目录")
        self.root.rename(renamed)
        bootstrap(renamed)
        actual = json.loads((renamed / "config/chart-sync.json").read_text())
        self.assertEqual(actual["output_root"], sync["output_root"])
        self.assertEqual(actual["manifest_path"], sync["manifest_path"])
        self.assertEqual(actual["custom"], "retain")
        self.assertEqual(actual["working_directory"], str(renamed))
        self.assertEqual(actual["child_exec"], str(renamed / "runtime/python/python.exe"))

    def test_bootstrap_rename_rebases_internal_custom_library(self):
        release.write_json(self.root / "config/chart-sync.json", {
            "working_directory": str(self.root), "output_root": str(self.root / "resource/custom-charts"),
            "manifest_path": str(self.root / "resource/custom-charts/custom.json")})
        renamed = self.root.with_name("旧根改名后")
        self.root.rename(renamed)
        bootstrap(renamed)
        actual = json.loads((renamed / "config/chart-sync.json").read_text())
        self.assertEqual(actual["output_root"], str(renamed / "resource/custom-charts"))
        self.assertEqual(actual["manifest_path"], str(renamed / "resource/custom-charts/custom.json"))

    def test_windows_ps51_clean_path_verifies_archive_without_get_file_hash(self):
        if os.name != "nt": self.skipTest("Windows PS 5.1 首启")
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        script = Path(__file__).resolve().parents[1] / "scripts/start-release.ps1"
        destination = self.root / "scripts/start-release.ps1"
        destination.write_bytes(script.read_bytes())
        (self.root / "runtime").mkdir()
        (self.root / "runtime/maapjsk-python.zip").write_bytes(b"deliberately-wrong-archive")
        environment = dict(os.environ)
        environment["PATH"] = str(shell.parent) + os.pathsep + str(Path(os.environ["SystemRoot"]) / "System32")
        environment.pop("PSModulePath", None)
        result = subprocess.run([str(shell), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(destination), "-PrepareOnly"], env=environment, capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        error = result.stderr.decode("utf-8", errors="replace")
        self.assertIn("Python ZIP", error)
        self.assertNotIn("Get-FileHash", error)
        self.assertFalse((self.root / "runtime/python").exists())

    def test_builder_exact_asset_names_and_excludes_supplied_user_state(self):
        source = Path(self.folder.name) / "source"
        mfa = Path(self.folder.name) / "publish"
        output = Path(self.folder.name) / "assets"
        def put(root, name, content=b"fixture"):
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        for name in ["agent/server.py", "project_sekai/__init__.py", "project_sekai/native/maapjsk_native.pyd",
                     "project_sekai/native/vendor/minitouch/LICENSE", "mfa-chart-ui/MaaPjskReleaseUpdate.cs",
                     "scripts/start-release.ps1", "scripts/apply-release-update.ps1", "scripts/update-release.py", "scripts/sync_sekai_catalog.py",
                     "packaging/MaaPJSK.cmd", "packaging/PP-OCRv5-NOTICE.md", "runtime-compatibility.json", "README.md", "THIRD-PARTY-NOTICES.md",
                     "docs/release-package.md", "docs/release-notes-v0.8.0.md", "third_party/maabangdream/LICENSE",
                     "packaging/licenses/MFAAvalonia-GPL-3.0.txt", "packaging/licenses/MaaFramework-LGPL-3.0.txt", "packaging/licenses/PaddleOCR-Apache-2.0.txt"]:
            put(source, name)
        put(source, "interface.json", b'{"version":"0.8.0","agent":{"child_exec":"python"}}')
        put(source, "resource/pipeline/test.json", b"{}")
        model_hashes = {}
        for name in ["inference.onnx", "inference.yml"]:
            put(source, "resource/models/song_title_ocr/" + name, name.encode())
            model_hashes[name] = hashlib.sha256(name.encode()).hexdigest()
        import cv2
        import numpy as np
        crop = cv2.imencode(".png", np.zeros((3, 4, 3), dtype=np.uint8))[1].tobytes()
        for config_path in release_package.TEMPLATE_CONFIGS:
            put(source, config_path, b'{"templates":{"home":"templates/home.png"}}')
            put((source / config_path).parent, "templates/home.png", crop)
        for name in ["MaaPJSK.exe", "libs/MFAAvalonia.Core.dll", "coreclr.dll", "System.Private.CoreLib.dll"]:
            put(mfa, name)
        release.write_json(mfa / "packaging/overlay-build.json", {"mfa_ref":"v2.12.0", "overlay_sha256":release.digest(source / "mfa-chart-ui/MaaPjskReleaseUpdate.cs"), "core_sha256":release.digest(mfa / "libs/MFAAvalonia.Core.dll")})
        for name in ["config/instances/default.json", "appsettings.json", "profiles/user.json", "debug/log.txt", "resource/charts/manifest.json", "docs/images/full-screenshot.png"]:
            put(mfa, name, b"private-user-state")
        runtime = Path(self.folder.name) / "python.zip"
        runtime.write_bytes(b"neutral-runtime-fixture")
        with patch.object(release_package.subprocess, "check_output", side_effect=["", "a" * 40]), patch.object(release_package, "MODEL_HASHES", model_hashes):
            target = release_package.build(source, mfa, runtime, output, "0.8.0")
        expected = {"MaaPJSK-v0.8.0-win-x64.zip", "MaaPJSK-v0.8.0-win-x64-update.zip"}
        self.assertEqual({p.name for p in output.glob("*.zip")}, expected)
        for name in expected:
            self.assertTrue((output / (name + ".sha256")).is_file())
        with zipfile.ZipFile(output / "MaaPJSK-v0.8.0-win-x64-update.zip") as package:
            self.assertIn("resource/models/song_title_ocr/NOTICE.md", package.namelist())
            self.assertFalse(any(name.startswith(("runtime/", "config/", "profiles/", "debug/", "resource/charts/")) for name in package.namelist()))
            self.assertNotIn("docs/images/full-screenshot.png", package.namelist())
        self.assertEqual(release_package.check_package(target)["release_validation"], "passed")


if __name__ == "__main__":
    unittest.main()
