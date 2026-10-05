import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("powershell"), "需要 PowerShell 检查部署进程门禁")
class MfaLaunchTests(unittest.TestCase):
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
