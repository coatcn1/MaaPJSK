import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("powershell"), "需要 PowerShell 检查部署进程门禁")
class MfaLaunchTests(unittest.TestCase):
    def probe_launch_environment(self, easing=None, hidden_chain=None, trace_contacts=None, *, title_gray=None,
                                 gold_pair=None, single_follower=None):
        probe = """import json
from types import SimpleNamespace
from project_sekai import sus_chart, chart_player
from project_sekai.song_identity import SongMatcher
chart = sus_chart.parse_sus('#REQUEST "ticks_per_beat 480"\\n#00002:4\\n#BPM01:120\\n#00008:01\\n#00017:12\\n#00057:22')
matcher = SongMatcher(SimpleNamespace(songs={}), None)
print(json.dumps({'easing': sus_chart.SUS_EASING_DIRECTION_GUARD,
                  'hidden_chain': sus_chart.NONJUDGEMENT_SLIDE_GUARD,
                  'trace_contacts': chart_player.TRACE_CONTACT_CHAIN_GUARD,
                  'title_gray': matcher.cooperative_title_grayscale,
                  'start_anchor_version': chart_player.START_ANCHOR_VERSION,
                  'version': chart_player.TOUCH_PLAN_VERSION, 'flick': chart.first.flick}))
"""
        script = r"""[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$ErrorActionPreference = 'Stop'
$StageOnly = $false
$projectRoot = 'PJSK_PROJECT_ROOT'
$targetRoot = Join-Path $projectRoot '.local/mfa-generic'
$targetExe = Join-Path $targetRoot 'MFAAvalonia.exe'
$Python = 'PJSK_PYTHON'
$pjskProbe = 'PJSK_PROBE'
# 替换窗口启动，只让实际入口末段把环境传给真实 Python 导入；不创建控制器或操作设备。
function Start-Process {
    param($FilePath, $WorkingDirectory, $WindowStyle)
    $pjskProbe | & $Python -X utf8 -
    if ($LASTEXITCODE -ne 0) { throw '启动环境探针失败' }
}
$pjskAst = [System.Management.Automation.Language.Parser]::ParseFile('PJSK_SCRIPT_PATH', [ref]$null, [ref]$null)
$pjskStatements = @($pjskAst.EndBlock.Statements)
$pjskStageExit = @($pjskStatements | Where-Object { $_.Extent.Text -eq 'if ($StageOnly) { return }' })
if ($pjskStageExit.Count -ne 1) { throw '未找到纯部署出口，拒绝执行启动脚本其他部分' }
$pjskLaunch = @($pjskStatements | Where-Object { $_.Extent.StartOffset -gt $pjskStageExit[0].Extent.EndOffset })
if ($pjskLaunch[-1].Extent.Text -notmatch '^Start-Process ') { throw '未找到窗口启动边界' }
& ([scriptblock]::Create(($pjskLaunch | ForEach-Object { $_.Extent.Text }) -join "`n"))
"""
        for name, value in {
            "PJSK_PROJECT_ROOT": str(ROOT), "PJSK_PYTHON": sys.executable,
            "PJSK_PROBE": probe, "PJSK_SCRIPT_PATH": str(ROOT / "scripts/launch-mfa.ps1"),
        }.items():
            script = script.replace(name, value.replace("'", "''"))
        env = os.environ.copy()
        for name in ("MAAPJSK_SUS_EASING_DIRECTION_GUARD", "MAAPJSK_NONJUDGEMENT_SLIDE_GUARD",
                     "MAAPJSK_ERASED_SLIDE_GUARD", "MAAPJSK_HIDDEN_SLIDE_FRAGMENT_GUARD",
                     "MAAPJSK_TRACE_CONTACT_CHAIN_GUARD", "MAAPJSK_COOPERATIVE_TITLE_GRAYSCALE",
                     "MAAPJSK_GOLD_TAP_PAIR_GUARD", "MAAPJSK_GOLD_TAP_SINGLE_FOLLOWER_GUARD",
                     "MAAPJSK_GOLD_TAP_HEAD_GUARD", "MAAPJSK_GOLD_TAP_FOLLOWERS_GUARD"):
            env.pop(name, None)
        for name, value in (("MAAPJSK_SUS_EASING_DIRECTION_GUARD", easing),
                            ("MAAPJSK_NONJUDGEMENT_SLIDE_GUARD", hidden_chain),
                            ("MAAPJSK_TRACE_CONTACT_CHAIN_GUARD", trace_contacts),
                            ("MAAPJSK_COOPERATIVE_TITLE_GRAYSCALE", title_gray),
                            ("MAAPJSK_GOLD_TAP_PAIR_GUARD", gold_pair),
                            ("MAAPJSK_GOLD_TAP_SINGLE_FOLLOWER_GUARD", single_follower)):
            if value is not None:
                env[name] = value
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], cwd=ROOT,
                                env=env, capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def test_normal_launcher_enables_both_semantic_repairs_without_parameters(self):
        self.assertEqual(self.probe_launch_environment(),
                         {"easing": True, "hidden_chain": True, "trace_contacts": True, "title_gray": True,
                          "start_anchor_version": 30, "version": 50, "flick": 0})

    def test_launcher_preserves_explicit_independent_rollbacks(self):
        for easing, hidden_chain, version in (("0", None, 42), (None, "0", 44), ("0", "0", 36)):
            with self.subTest(easing=easing, hidden_chain=hidden_chain):
                self.assertEqual(self.probe_launch_environment(easing, hidden_chain),
                                 {"easing": easing != "0", "hidden_chain": hidden_chain != "0", "trace_contacts": True,
                                  "title_gray": True, "start_anchor_version": 30,
                                  "version": version, "flick": 2 if easing == "0" else 0})

    def test_trace_contacts_can_roll_back_without_disabling_sus_repairs(self):
        self.assertEqual(self.probe_launch_environment(trace_contacts="0"),
                         {"easing": True, "hidden_chain": True, "trace_contacts": False, "title_gray": True,
                          "start_anchor_version": 30, "version": 18, "flick": 0})

    def test_opening_repairs_have_independent_rollbacks(self):
        grayscale_off = self.probe_launch_environment(title_gray="0")
        self.assertFalse(grayscale_off["title_gray"])
        self.assertEqual(grayscale_off["start_anchor_version"], 30)
        self.assertEqual(self.probe_launch_environment(single_follower="0")["start_anchor_version"], 29)
        pair_off = self.probe_launch_environment(gold_pair="0")
        self.assertTrue(pair_off["title_gray"])
        self.assertEqual(pair_off["start_anchor_version"], 28)
        self.assertEqual((grayscale_off["version"], pair_off["version"]), (50, 50))

    def check_guard(self, stage_only, processes):
        script = r"""[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding
$ErrorActionPreference = 'Stop'
$targetRoot = 'D:\TestWorkspace\MaaPJSK\.local\mfa-generic'
$StageOnly = PJSK_STAGE_ONLY
$pjskFakeProcesses = ConvertFrom-Json -InputObject 'PJSK_FAKE_PROCESSES'
function Get-CimInstance { return $pjskFakeProcesses }
$pjskAst = [System.Management.Automation.Language.Parser]::ParseFile('PJSK_SCRIPT_PATH', [ref]$null, [ref]$null)
$pjskGuards = @($pjskAst.EndBlock.Statements | Where-Object {
    $_.Extent.Text -match 'Get-CimInstance|otherProcesses.Count|current.Count'
})
if ($pjskGuards.Count -lt 3) { throw '没有找到实际启动脚本的进程门禁' }
$pjskCode = ($pjskGuards | ForEach-Object { $_.Extent.Text }) -join "`n"
try {
    & ([scriptblock]::Create($pjskCode)) | Out-Null
    @{allowed=$true; error=$null} | ConvertTo-Json -Compress
} catch {
    @{allowed=$false; error=$_.Exception.Message} | ConvertTo-Json -Compress
}
"""
        script = script.replace("PJSK_STAGE_ONLY", "$true" if stage_only else "$false")
        script = script.replace("PJSK_FAKE_PROCESSES", json.dumps(processes).replace("'", "''"))
        script = script.replace("PJSK_SCRIPT_PATH", str(ROOT / "scripts/launch-mfa.ps1").replace("'", "''"))
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                                capture_output=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout.splitlines()[-1])

    def foreign_process(self):
        return {"ProcessId": 321, "ExecutablePath": r"D:\OtherWorkspace\MFA\MaaBanGDream.exe"}

    def own_process(self):
        return {"ProcessId": 123, "ExecutablePath": r"D:\TestWorkspace\MaaPJSK\.local\mfa-generic\MFAAvalonia.exe"}

    def test_stage_only_allows_other_project_mfa(self):
        self.assertTrue(self.check_guard(True, [self.foreign_process()])["allowed"])

    def test_stage_only_rejects_own_runtime_even_when_other_project_is_present(self):
        result = self.check_guard(True, [self.foreign_process(), self.own_process()])
        self.assertFalse(result["allowed"])
        self.assertIn("MaaPJSK 的 MFA 已运行", result["error"])

    def test_launch_keeps_the_controller_exclusivity_guard(self):
        result = self.check_guard(False, [self.foreign_process()])
        self.assertFalse(result["allowed"])
        self.assertIn("其他 MFA 实例正在运行", result["error"])

    def test_closed_runtime_allows_staging_and_launch(self):
        self.assertTrue(self.check_guard(True, [])["allowed"])
        self.assertTrue(self.check_guard(False, [])["allowed"])


if __name__ == "__main__":
    unittest.main()
