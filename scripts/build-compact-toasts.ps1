param(
    [string]$MfaSource,
    [string]$RuntimeSource,
    [string]$SourceRef = 'v2.12.0'
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$workspaceRoot = Split-Path -Parent $projectRoot
if (-not $MfaSource) { $MfaSource = Join-Path $workspaceRoot 'MFAAvalonia' }
if (-not $RuntimeSource) { $RuntimeSource = Join-Path $workspaceRoot '.tools\MFAAvalonia' }
$stockLibrary = Join-Path $RuntimeSource 'libs\SukiUI.dll'
if (-not (Test-Path -LiteralPath $stockLibrary)) { throw "缺少通用 MFA 的 SukiUI.dll：$stockLibrary" }
$outputRoot = Join-Path $projectRoot '.local\compact-toasts'
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$archive = Join-Path $outputRoot 'source.zip'
# 仅从固定上游版本导出 UI 库，不修改本地 MFA 仓库或复制它的任务与设置。
git -C $MfaSource archive --format=zip "--output=$archive" $SourceRef SukiUI
if ($LASTEXITCODE -ne 0) { throw '无法导出 MFA v2.12.0 的 SukiUI 源码' }
$sourceRoot = Join-Path $outputRoot 'source'
Expand-Archive -LiteralPath $archive -DestinationPath $sourceRoot -Force
$themePath = Join-Path $sourceRoot 'SukiUI\Controls\SukiToast.axaml'
$theme = [IO.File]::ReadAllText($themePath)
$replacements = [ordered]@{
    'MinWidth="300"' = 'MinWidth="240"'
    'MaxWidth="400"' = 'MaxWidth="320"'
    'Margin="40,5,30,10"' = 'Margin="32,4,16,6"'
    'Margin="20,22,20,8"' = 'Margin="12,14,12,4"'
    'Margin="0,-7,0,-4"' = 'Margin="0,0,0,0"'
    'Margin="0,10,0,0"' = 'Margin="0,0,0,0"'
    'Margin="12,10,0,0"' = 'Margin="8,4,0,0"'
    '<Setter Property="FontSize" Value="14" />' = '<Setter Property="FontSize" Value="12" />'
    '<Setter Property="FontSize" Value="16" />' = '<Setter Property="FontSize" Value="14" />'
    'FontSize="17"' = 'FontSize="14"'
    'Width="35"' = 'Width="28"'
    'Height="35"' = 'Height="28"'
    'CornerRadius="35"' = 'CornerRadius="28"'
    'Margin="22,0,0,3"' = 'Margin="16,0,0,3"'
}
foreach ($entry in $replacements.GetEnumerator()) {
    if (-not $theme.Contains($entry.Key)) { throw "上游通知模板不匹配：$($entry.Key)" }
    $theme = $theme.Replace($entry.Key, $entry.Value)
}
[IO.File]::WriteAllText($themePath, $theme, [Text.UTF8Encoding]::new($false))
$buildDirectory = Join-Path $outputRoot 'build'
dotnet build (Join-Path $sourceRoot 'SukiUI\SukiUI.csproj') -c Release -o $buildDirectory --nologo -v quiet
if ($LASTEXITCODE -ne 0) { throw '紧凑通知样式编译失败' }
Copy-Item -LiteralPath (Join-Path $buildDirectory 'SukiUI.dll') -Destination $outputRoot -Force
@{
    source_ref = $SourceRef
    stock_sha256 = (Get-FileHash -LiteralPath $stockLibrary -Algorithm SHA256).Hash
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $outputRoot 'compatibility.json') -Encoding UTF8
Write-Host '紧凑通知 UI 库已生成，下次部署 MFA 时自动应用。'
