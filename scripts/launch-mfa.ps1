param(
    [string]$RuntimeSource,
    [string]$Python,
    [switch]$StageOnly,
    [switch]$Help
)

$ErrorActionPreference = 'Stop'
if ($Help) {
    Write-Host 'maapjsk [-RuntimeSource <MFA目录>] [-Python <python.exe>] [-StageOnly]'
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
foreach ($directory in @($agentTarget, $packageTarget, $resourceTarget, (Join-Path $targetRoot 'config'), (Join-Path $targetRoot 'debug'))) {
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
Get-ChildItem -LiteralPath (Join-Path $projectRoot 'project_sekai') -Filter '*.py' | ForEach-Object {
    Copy-Item -LiteralPath $_.FullName -Destination $packageTarget -Force
}
Copy-Item -Path (Join-Path $projectRoot 'resource\*') -Destination $resourceTarget -Recurse -Force
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

Write-Host "MaaPJSK MFA 已部署：$targetRoot"
if ($StageOnly) { return }

$env:PATH = "$(Split-Path -Parent $Python);$env:PATH"
$env:MAAPJSK_TEMPLATE_CONFIG = Join-Path $targetRoot 'config\maapjsk-templates.json'
Start-Process -FilePath $targetExe -WorkingDirectory $targetRoot -WindowStyle Normal
