param([Parameter(Mandatory=$true)][string]$Plan, [Parameter(Mandatory=$true)][int]$ParentId)
$ErrorActionPreference = 'Stop'
$updatePlan = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$packageRoot = [IO.Path]::GetFullPath($updatePlan.root)
$pythonExe = Join-Path $packageRoot 'runtime\python\python.exe'
try {
    $parent = Get-Process -Id $ParentId -ErrorAction SilentlyContinue
    if ($parent -and -not $parent.WaitForExit(60000)) { throw 'MaaPJSK 未退出，未写入更新。' }
    # 仅观察本安装目录的进程；不结束其他程序，Agent 未退出时禁止覆盖。
    $deadline = [DateTime]::UtcNow.AddSeconds(30)
    do {
        $active = @(Get-CimInstance Win32_Process | Where-Object { $_.ProcessId -ne $PID -and $_.ExecutablePath -and $_.ExecutablePath.StartsWith($packageRoot + '\', [StringComparison]::OrdinalIgnoreCase) })
        if ($active.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)
    if ($active.Count -gt 0) { throw '本安装目录还有运行中的 GUI 或 Agent，未写入更新。' }
    $helper = Join-Path (Split-Path -Parent $Plan) 'release_update.py'
    & $pythonExe -X utf8 $helper apply --plan $Plan
    if ($LASTEXITCODE -ne 0) { throw '发行更新失败，原版本已回退。' }
    Start-Process -FilePath 'powershell.exe' -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',('"' + (Join-Path $packageRoot 'scripts\start-release.ps1') + '"')) -WorkingDirectory $packageRoot -WindowStyle Hidden
} catch {
    $_ | Out-String | Set-Content -LiteralPath (Join-Path (Split-Path -Parent $Plan) 'update-error.txt') -Encoding UTF8
    throw
}
