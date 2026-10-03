param(
    [string]$RuntimeSource,
    [string]$Python,
    [switch]$StageOnly,
    [switch]$VerifyAdbEndpoint,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
if ($Help) {
    Write-Host 'powershell -NoProfile -ExecutionPolicy Bypass -File "<项目目录>\scripts\launch-mfa.ps1" [-VerifyAdbEndpoint] [-RuntimeSource <MFA目录>] [-Python <python.exe>] [-StageOnly]'
    return
}
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $RuntimeSource) { $RuntimeSource = Join-Path $workspaceRoot '.tools\MFAAvalonia' }
if (-not $Python) { $Python = Join-Path $workspaceRoot '.tools\Miniconda3\envs\maabangdream\python.exe' }
$targetRoot = Join-Path $projectRoot '.local\mfa-generic'
$sourceExe = Join-Path $RuntimeSource 'MFAAvalonia.exe'
$targetExe = Join-Path $targetRoot 'MFAAvalonia.exe'
$templateConfig = Join-Path $projectRoot '.local\config.json'

foreach ($path in @($sourceExe, $Python, $templateConfig)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "缺少 MFA 启动必需文件：$path"
    }
}

$agentBinarySource = Join-Path $RuntimeSource 'libs\MaaAgentBinary'
if (-not (Test-Path -LiteralPath $agentBinarySource -PathType Container)) {
    $agentBinarySource = & $Python -c 'from pathlib import Path; from maa.controller import AdbController; print(Path(AdbController.AGENT_BINARY_PATH).resolve())'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $agentBinarySource -PathType Container)) {
        throw '缺少 MaaFramework 输入运行文件，请先安装项目 Python 依赖。'
    }
}

$otherProcesses = @(Get-CimInstance Win32_Process -Filter "Name = 'MaaBanGDream.exe' OR Name = 'MFAAvalonia.exe'" | Where-Object {
    $_.ExecutablePath -and (Split-Path -Parent $_.ExecutablePath) -ine $targetRoot
})
if ($otherProcesses.Count -gt 0) {
    $details = ($otherProcesses | ForEach-Object { "PID $($_.ProcessId): $($_.ExecutablePath)" }) -join '; '
    throw "其他 MFA 实例正在运行。请先关闭后再启动 MaaPJSK：$details"
}

$current = @(Get-CimInstance Win32_Process -Filter "Name = 'MaaBanGDream.exe' OR Name = 'MFAAvalonia.exe'" | Where-Object {
    $_.ExecutablePath -and (Split-Path -Parent $_.ExecutablePath) -ieq $targetRoot
})
if ($current.Count -gt 0) {
    if ($StageOnly) { throw 'MaaPJSK 的 MFA 已运行；请先停止任务并关闭程序后重新部署。' }
    Write-Host 'MaaPJSK MFA 已在运行。'
    return
}

New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null
# MFA 实际从 libs/MaaAgentBinary 加载输入文件，且启动时会清理根目录的同名目录。
$agentBinaryTarget = Join-Path $targetRoot 'libs'
New-Item -ItemType Directory -Force -Path $agentBinaryTarget | Out-Null
Copy-Item -LiteralPath $agentBinarySource -Destination $agentBinaryTarget -Recurse -Force
Get-ChildItem -LiteralPath $RuntimeSource -File | Where-Object { $_.Name -notin @('interface.json', 'appsettings.json') } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $targetRoot -Force
}
foreach ($directory in @('runtimes', 'libs', 'plugins', 'cs', 'de', 'en-US', 'es', 'fr', 'it', 'ja', 'ja-JP', 'ko', 'pl', 'pt-BR', 'ru', 'tr', 'zh-Hans', 'zh-Hant')) {
    $source = Join-Path $RuntimeSource $directory
    if (Test-Path -LiteralPath $source -PathType Container) {
        Copy-Item -LiteralPath $source -Destination $targetRoot -Recurse -Force
    }
}
$sourceBaseResource = Join-Path $RuntimeSource 'resource\base'
if (Test-Path -LiteralPath $sourceBaseResource -PathType Container) {
    New-Item -ItemType Directory -Force -Path (Join-Path $targetRoot 'resource') | Out-Null
    Copy-Item -LiteralPath $sourceBaseResource -Destination (Join-Path $targetRoot 'resource') -Recurse -Force
}
$sourceLayout = Join-Path $RuntimeSource 'resource\mfa_layout.json'
if (Test-Path -LiteralPath $sourceLayout -PathType Leaf) {
    Copy-Item -LiteralPath $sourceLayout -Destination (Join-Path $targetRoot 'resource') -Force
}

$agentTarget = Join-Path $targetRoot 'agent'
$packageTarget = Join-Path $targetRoot 'project_sekai'
$resourceTarget = Join-Path $targetRoot 'resource'
$scriptsTarget = Join-Path $targetRoot 'scripts'
foreach ($directory in @($agentTarget, $packageTarget, $resourceTarget, $scriptsTarget, (Join-Path $targetRoot 'config'), (Join-Path $targetRoot 'debug'))) {
    New-Item -ItemType Directory -Force -Path $directory | Out-Null
}
$interface = Get-Content -LiteralPath (Join-Path $projectRoot 'interface.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$interface.agent.child_exec = $Python.Replace('\', '/')
$interface | ConvertTo-Json -Depth 32 | Set-Content -LiteralPath (Join-Path $targetRoot 'interface.json') -Encoding UTF8
$settingsPath = Join-Path $targetRoot 'appsettings.json'
if (-not (Test-Path -LiteralPath $settingsPath -PathType Leaf)) {
    Copy-Item -LiteralPath (Join-Path $RuntimeSource 'appsettings.json') -Destination $settingsPath -Force
}
$settings = Get-Content -LiteralPath $settingsPath -Raw -Encoding UTF8 | ConvertFrom-Json
$settings.NoAutoStart = 'True'
$settings | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $settingsPath -Encoding UTF8
Copy-Item -LiteralPath (Join-Path $projectRoot 'agent\server.py') -Destination $agentTarget -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'agent\auto_live.py') -Destination $agentTarget -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'agent\solo_live.py') -Destination $agentTarget -Force
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'project_sekai') -Filter '*.py' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $packageTarget -Force
}
$nativeSource = Join-Path $projectRoot 'project_sekai\native'
if (Test-Path -LiteralPath $nativeSource -PathType Container) {
    Copy-Item -LiteralPath $nativeSource -Destination $packageTarget -Recurse -Force
}
# 设置只在首次移动选项时迁移；后续部署保留 MFA 设置页保存的值。
Push-Location -LiteralPath $projectRoot
try { & $Python -X utf8 -m project_sekai.performance_settings --migrate-root $targetRoot }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw '演奏设置迁移失败，已停止启动。' }
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'resource') | Where-Object { $_.Name -ne 'charts' } | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $resourceTarget -Recurse -Force
}
# 本地谱面库共用源码目录中的一份数据；部署不复制整库，避免重复占用空间或覆盖更新结果。
Copy-Item -LiteralPath (Join-Path $projectRoot 'scripts\sync_sekai_catalog.py') -Destination $scriptsTarget -Force
$chartRoot = Join-Path $projectRoot 'resource\charts'
@{
    child_exec = $Python
    script_path = Join-Path $scriptsTarget 'sync_sekai_catalog.py'
    working_directory = $targetRoot
    output_root = $chartRoot
    manifest_path = Join-Path $chartRoot 'manifest.json'
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $targetRoot 'config\chart-sync.json') -Encoding UTF8
Copy-Item -LiteralPath $templateConfig -Destination (Join-Path $targetRoot 'config\maapjsk-templates.json') -Force
$templateTarget = Join-Path $targetRoot 'config\templates'
New-Item -ItemType Directory -Force -Path $templateTarget | Out-Null
Get-ChildItem -LiteralPath (Join-Path $projectRoot '.local\templates') -Filter '*.png' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $templateTarget -Force
}
$compactLibrary = Join-Path $projectRoot '.local\compact-toasts\SukiUI.dll'
if (Test-Path -LiteralPath $compactLibrary -PathType Leaf) {
    $compatibility = Get-Content -LiteralPath (Join-Path $projectRoot '.local\compact-toasts\compatibility.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $stockHash = (Get-FileHash -LiteralPath (Join-Path $RuntimeSource 'libs\SukiUI.dll') -Algorithm SHA256).Hash
    if ($compatibility.stock_sha256 -ne $stockHash) {
        throw 'MFA UI 库已变化，请重新运行 build-compact-toasts.ps1 后部署。'
    }
    Copy-Item -LiteralPath $compactLibrary -Destination (Join-Path $targetRoot 'libs\SukiUI.dll') -Force
}
$chartSettingsLibrary = Join-Path $projectRoot '.local\chart-settings\MFAAvalonia.Core.dll'
if (Test-Path -LiteralPath $chartSettingsLibrary -PathType Leaf) {
    $compatibility = Get-Content -LiteralPath (Join-Path $projectRoot '.local\chart-settings\compatibility.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    $stockHash = (Get-FileHash -LiteralPath (Join-Path $RuntimeSource 'libs\MFAAvalonia.Core.dll') -Algorithm SHA256).Hash
    if ($compatibility.stock_sha256 -ne $stockHash) {
        throw 'MFA Core 库已变化，请重新运行 build-chart-settings.ps1 后部署。'
    }
    Copy-Item -LiteralPath $chartSettingsLibrary -Destination (Join-Path $targetRoot 'libs\MFAAvalonia.Core.dll') -Force
}

Write-Host "MaaPJSK MFA 已部署：$targetRoot"
if ($VerifyAdbEndpoint) {
    # 检查最后使用实例保存的端点，不自动替换设备，也不发送游戏输入。
    $instanceId = $settings.'Instances.LastActive'
    if (-not $instanceId) { $instanceId = 'default' }
    if ($instanceId -notmatch '^[A-Za-z0-9_-]+$') { throw 'MFA 已保存实例 ID 无效，请在界面重新选择实例。' }
    $instancePath = Join-Path $targetRoot "config\instances\$instanceId.json"
    $savedDevice = $null
    if (Test-Path -LiteralPath $instancePath) {
        $instance = Get-Content -LiteralPath $instancePath -Raw -Encoding UTF8 | ConvertFrom-Json
        $savedDevice = $instance.AdbDevice
    }
    if ($savedDevice -and $savedDevice.AdbPath -and $savedDevice.AdbSerial) {
        $adbPath = [string]$savedDevice.AdbPath
        if (-not (Test-Path -LiteralPath $adbPath -PathType Leaf)) {
            Write-Warning '保存的 ADB 程序不存在，启动后请在 MFA 中刷新并重新选择模拟器。'
        } else {
            # 尚未连接的 TCP 设备不会出现在 ADB 列表中；诊断失败不能阻止 MFA 自己建立连接。
            $savedErrorPreference = $ErrorActionPreference
            try {
                $ErrorActionPreference = 'Continue'
                $endpointState = & $adbPath -s ([string]$savedDevice.AdbSerial) get-state 2>&1
                $endpointExitCode = $LASTEXITCODE
            } finally { $ErrorActionPreference = $savedErrorPreference }
            if ($endpointExitCode -ne 0 -or ($endpointState -join "`n").Trim() -ne 'device') {
                Write-Warning '保存的 ADB 端点尚不可用，启动后请在 MFA 中刷新或重新连接 MuMu。'
            } else {
                Write-Host '已保存 ADB 端点检查通过。'
            }
        }
    } else {
        Write-Host '尚未保存 ADB 端点，请在 MFA 窗口中选择模拟器。'
    }
}
if ($StageOnly) { return }

$env:PATH = "$(Split-Path -Parent $Python);$env:PATH"
$env:MAAPJSK_TEMPLATE_CONFIG = Join-Path $targetRoot 'config\maapjsk-templates.json'
Start-Process -FilePath $targetExe -WorkingDirectory $targetRoot -WindowStyle Normal
