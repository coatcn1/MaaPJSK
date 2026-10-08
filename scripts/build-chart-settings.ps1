param(
    [string]$MfaSource,
    [string]$RuntimeSource,
    [switch]$ReleasePackage,
    [string]$PublishOutput
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
Copy-Item -LiteralPath (Join-Path $projectRoot 'mfa-chart-ui\MaaPjskTaskStatus.cs') -Destination (Join-Path $sourceRoot 'MFAAvalonia\Extensions\MaaFW') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'mfa-chart-ui\SystemSleepHelper.cs') -Destination (Join-Path $sourceRoot 'MFAAvalonia\Helper') -Force
Copy-Item -LiteralPath (Join-Path $projectRoot 'mfa-chart-ui\MaaPjskReleaseUpdate.cs') -Destination (Join-Path $sourceRoot 'MFAAvalonia\Helper') -Force

# 便携包全部更新入口由项目校验器接管；没有清单的开发版继续使用原上游流程。
$versionPath = Join-Path $sourceRoot 'MFAAvalonia\Helper\VersionChecker.cs'
$versionSource = [IO.File]::ReadAllText($versionPath)
$updateEntries = @{
    'public static async Task CheckForResourceUpdatesAsync\(bool isGithub = true\)\s*\{' = 'if (MaaPjskReleaseUpdate.IsPortable) { await MaaPjskReleaseUpdate.CheckAsync(); return; }'
    'public static async Task CheckForMFAUpdatesAsync\(bool isGithub = true\)\s*\{' = 'if (MaaPjskReleaseUpdate.IsPortable) { await MaaPjskReleaseUpdate.CheckAsync(); return; }'
    'public async static Task UpdateResource\(bool isGithub = true, bool closeDialog = false, bool noDialog = false, Action action = null, string currentVersion = "", string\? localPackagePath = null\)\s*\{' = 'if (MaaPjskReleaseUpdate.IsPortable) { await MaaPjskReleaseUpdate.UpdateAsync(localPackagePath); return; }'
    'public async static Task UpdateMFA\(bool isGithub, bool noDialog = false\)\s*\{' = 'if (MaaPjskReleaseUpdate.IsPortable) { await MaaPjskReleaseUpdate.UpdateAsync(); return; }'
}
foreach ($pattern in $updateEntries.Keys) {
    if ([regex]::Matches($versionSource, $pattern).Count -ne 1) { throw '固定 MFA 更新入口不匹配，禁止生成可绕过保护的包。' }
    $versionSource = [regex]::Replace($versionSource, $pattern, ('$0' + "`n        " + $updateEntries[$pattern]))
}
[IO.File]::WriteAllText($versionPath, $versionSource, [Text.UTF8Encoding]::new($false))

$processorPath = Join-Path $sourceRoot 'MFAAvalonia\Extensions\MaaFW\MaaProcessor.cs'
$processor = [IO.File]::ReadAllText($processorPath)
$statusPattern = 'if \(InstanceConfiguration.GetValue\(ConfigurationKeys.ContinueRunningWhenError, true\)\)\s*job.Wait\(\);\s*else\s*job.Wait\(\).ThrowIfNot\(MaaJobStatus.Succeeded\);'
if ([regex]::Matches($processor, $statusPattern).Count -ne 1) { throw '上游队列任务状态入口不匹配' }
$statusCheck = @'
var jobStatus = job.Wait();
            token.ThrowIfCancellationRequested();
            MaaPjskTaskStatus.Check(task, jobStatus,
                InstanceConfiguration.GetValue(ConfigurationKeys.ContinueRunningWhenError, true));
'@
[IO.File]::WriteAllText($processorPath, [regex]::Replace($processor, $statusPattern, $statusCheck), [Text.UTF8Encoding]::new($false))

$processor = [IO.File]::ReadAllText($processorPath)
$executionPattern = 'async private Task ExecuteTasks\(CancellationToken token\)\s*\{'
if ([regex]::Matches($processor, $executionPattern).Count -ne 1) { throw '上游实际任务执行入口不匹配' }
$executionScope = @'
async private Task ExecuteTasks(CancellationToken token)
    {
        // 队列最后一项提前出队时仍在执行；作用域持续到实际任务返回和清理结束。
        using var sleepScope = TaskQueue.Count > 0 && !token.IsCancellationRequested
            ? await SystemSleepHelper.BeginTaskExecutionAsync(token) : null;
        using var releaseScope = TaskQueue.Count > 0 && !token.IsCancellationRequested
            ? MaaPjskReleaseUpdate.BeginExecution() : null;
'@
[IO.File]::WriteAllText($processorPath, [regex]::Replace($processor, $executionPattern, $executionScope), [Text.UTF8Encoding]::new($false))

$performanceModelPath = Join-Path $modelTarget 'PerformanceUserControlModel.cs'
$performanceModel = [IO.File]::ReadAllText($performanceModelPath)
$preferenceRead = 'ConfigurationManager.Current.GetValue(ConfigurationKeys.PreventSleep, false)'
$preferenceWritePattern = 'partial void OnPreventSleepChanged\(bool value\) => HandlePropertyChanged\(ConfigurationKeys.PreventSleep, value, \(v\) =>\s*\{\s*SystemSleepHelper.ApplyPreventSleep\(v\);\s*\}\);'
if ($performanceModel.Split(@($preferenceRead), [StringSplitOptions]::None).Count -ne 2 -or
    [regex]::Matches($performanceModel, $preferenceWritePattern).Count -ne 1) { throw '上游防息屏开关入口不匹配' }
$performanceModel = $performanceModel.Replace($preferenceRead, 'SystemSleepHelper.GetPreventSleepSetting()')
$performanceModel = [regex]::Replace($performanceModel, $preferenceWritePattern, 'partial void OnPreventSleepChanged(bool value) => SystemSleepHelper.SavePreventSleepSetting(value);')
[IO.File]::WriteAllText($performanceModelPath, $performanceModel, [Text.UTF8Encoding]::new($false))

$appPath = Join-Path $sourceRoot 'MFAAvalonia\App.axaml.cs'
$appSource = [IO.File]::ReadAllText($appPath)
$shutdownPattern = 'private void OnShutdownRequested\(object sender, ShutdownRequestedEventArgs e\)\s*\{'
if ([regex]::Matches($appSource, $shutdownPattern).Count -ne 1) { throw '上游退出清理入口不匹配' }
$shutdown = @'
private void OnShutdownRequested(object sender, ShutdownRequestedEventArgs e)
    {
        SystemSleepHelper.Shutdown();
'@
[IO.File]::WriteAllText($appPath, [regex]::Replace($appSource, $shutdownPattern, $shutdown), [Text.UTF8Encoding]::new($false))

# 只修改当前开关的文本，保持原有配置键与关闭默认值。
$sleepLabels = @{
    'Strings.resx' = '任务运行中阻止息屏'
    'Strings.zh-Hant.resx' = '任務執行中阻止螢幕休眠'
    'Strings.en-US.resx' = 'Prevent display sleep while tasks run'
}
foreach ($file in $sleepLabels.Keys) {
    $languagePath = Join-Path $sourceRoot "MFAAvalonia\Assets\Localization\$file"
    $language = [IO.File]::ReadAllText($languagePath)
    $labelPattern = '(<data name="PreventSleep" xml:space="preserve">\s*<value>)[^<]*(</value>)'
    if ([regex]::Matches($language, $labelPattern).Count -ne 1) { throw "上游防息屏翻译入口不匹配：$file" }
    $language = [regex]::Replace($language, $labelPattern, ('${1}' + $sleepLabels[$file] + '${2}'))
    [IO.File]::WriteAllText($languagePath, $language, [Text.UTF8Encoding]::new($false))
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
        if (MFAAvalonia.ViewModels.UsersControls.Settings.ChartCatalogMaintenance.IsBusy || MaaPjskReleaseUpdate.IsBusy)
        {
            ToastHelper.Warn("资源维护中", "请等待谱面同步或发行更新结束后，再开始演出任务。");
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
if ($ReleasePackage) {
    if (-not $PublishOutput) { $PublishOutput = Join-Path $projectRoot '.local\release-v0.8.0\mfa-self-contained' }
    $desktopProject = Join-Path $sourceRoot 'MFAAvalonia.Desktop\MFAAvalonia.Desktop.csproj'
    $desktop = [IO.File]::ReadAllText($desktopProject)
    $branding = @'
    <PropertyGroup Condition="'$(MaaPjskPackageBuild)' == 'true'">
        <AssemblyName>MaaPJSK</AssemblyName>
        <OutputName>MaaPJSK</OutputName>
    </PropertyGroup>
'@
    [IO.File]::WriteAllText($desktopProject, $desktop.Replace('</Project>', $branding + "`n</Project>"), [Text.UTF8Encoding]::new($false))
    dotnet publish $desktopProject -c Release -r win-x64 --self-contained true -p:MaaPjskPackageBuild=true -o $PublishOutput --nologo -v quiet
    if ($LASTEXITCODE -ne 0) { throw 'MaaPJSK 自包含发行主程序构建失败。' }
    New-Item -ItemType Directory -Force -Path (Join-Path $PublishOutput 'packaging') | Out-Null
    @{
        schema = 1
        mfa_ref = 'v2.12.0'
        mfa_commit = (git -C $MfaSource rev-parse v2.12.0).Trim()
        overlay_sha256 = (Get-FileHash -LiteralPath (Join-Path $projectRoot 'mfa-chart-ui\MaaPjskReleaseUpdate.cs') -Algorithm SHA256).Hash.ToLowerInvariant()
        core_sha256 = (Get-FileHash -LiteralPath (Join-Path $PublishOutput 'libs\MFAAvalonia.Core.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
    } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $PublishOutput 'packaging\overlay-build.json') -Encoding UTF8
}
