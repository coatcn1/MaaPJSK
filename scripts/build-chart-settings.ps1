param(
    [string]$MfaSource,
    [string]$RuntimeSource
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $MfaSource) { $MfaSource = Join-Path $workspaceRoot 'MFAAvalonia' }
if (-not $RuntimeSource) { $RuntimeSource = Join-Path $workspaceRoot '.tools\MFAAvalonia' }
$stockLibrary = Join-Path $RuntimeSource 'libs\MFAAvalonia.Core.dll'
if (-not (Test-Path -LiteralPath $stockLibrary)) { throw "缺少通用 MFA 的 Core 库：$stockLibrary" }
$outputRoot = Join-Path $projectRoot '.local\chart-settings'
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$archive = Join-Path $outputRoot 'source.zip'
# 在项目忽略目录中构建固定上游版本，保留原许可证，不改动 MaaBanGDream 的 MFA 源码或运行目录。
git -C $MfaSource archive --format=zip "--output=$archive" v2.12.0
if ($LASTEXITCODE -ne 0) { throw '无法导出 MFA v2.12.0 源码' }
$sourceRoot = Join-Path $outputRoot 'source'
Expand-Archive -LiteralPath $archive -DestinationPath $sourceRoot -Force
$modelTarget = Join-Path $sourceRoot 'MFAAvalonia\ViewModels\UsersControls\Settings'
$viewTarget = Join-Path $sourceRoot 'MFAAvalonia\Views\UserControls\Settings'
foreach ($name in @('ChartCatalogSettingsUserControlModel.cs', 'PerformanceSettingsUserControlModel.cs')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "mfa-chart-ui\$name") -Destination $modelTarget -Force
}
foreach ($name in @('ChartCatalogSettingsUserControl.axaml', 'ChartCatalogSettingsUserControl.axaml.cs', 'PerformanceSettingsUserControl.axaml', 'PerformanceSettingsUserControl.axaml.cs')) {
    Copy-Item -LiteralPath (Join-Path $projectRoot "mfa-chart-ui\$name") -Destination $viewTarget -Force
}

$settingsPath = Join-Path $sourceRoot 'MFAAvalonia\Views\Pages\SettingsView.axaml'
$settings = [IO.File]::ReadAllText($settingsPath)
$marker = '<suki:SettingsLayout.Items>'
if (($settings.Split(@($marker), [StringSplitOptions]::None)).Count -ne 2) { throw '上游设置页入口不匹配' }
$entry = @'
<suki:SettingsLayout.Items>
                    <suki:SettingsLayoutItem Header="演奏设置">
                        <suki:SettingsLayoutItem.Content>
                            <settings:PerformanceSettingsUserControl />
                        </suki:SettingsLayoutItem.Content>
                    </suki:SettingsLayoutItem>
                    <suki:SettingsLayoutItem Header="谱面管理">
                        <suki:SettingsLayoutItem.Content>
                            <settings:ChartCatalogSettingsUserControl />
                        </suki:SettingsLayoutItem.Content>
                    </suki:SettingsLayoutItem>
'@
[IO.File]::WriteAllText($settingsPath, $settings.Replace($marker, $entry), [Text.UTF8Encoding]::new($false))

$taskPath = Join-Path $sourceRoot 'MFAAvalonia\ViewModels\Pages\TaskQueueViewModel.cs'
$taskSource = [IO.File]::ReadAllText($taskPath)
$pattern = 'public void StartTask\(\)\s*\{'
if ([regex]::Matches($taskSource, $pattern).Count -ne 1) { throw '上游任务启动入口不匹配' }
$guard = @'
public void StartTask()
    {
        // 维护期间所有实例都暂停新任务启动，避免下载和演出同时进行。
        if (MFAAvalonia.ViewModels.UsersControls.Settings.ChartCatalogMaintenance.IsBusy)
        {
            ToastHelper.Warn("谱面同步中", "请等待同步结束或取消同步后，再开始演出任务。");
            return;
        }
'@
[IO.File]::WriteAllText($taskPath, [regex]::Replace($taskSource, $pattern, $guard), [Text.UTF8Encoding]::new($false))

$buildRoot = Join-Path $outputRoot 'build'
dotnet build (Join-Path $sourceRoot 'MFAAvalonia\MFAAvalonia.csproj') -c Release -r win-x64 -o $buildRoot --nologo -v quiet
if ($LASTEXITCODE -ne 0) { throw '谱面管理设置页编译失败' }
Copy-Item -LiteralPath (Join-Path $buildRoot 'MFAAvalonia.Core.dll') -Destination $outputRoot -Force
@{
    source_ref = 'v2.12.0'
    stock_sha256 = (Get-FileHash -LiteralPath $stockLibrary -Algorithm SHA256).Hash
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outputRoot 'compatibility.json') -Encoding UTF8
Write-Host '谱面管理设置页已生成，下次部署 MaaPJSK 时自动应用。'
