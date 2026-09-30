param([switch]$Uninstall)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$binDirectory = Join-Path $env:LOCALAPPDATA 'MaaPJSK\bin'
$commandPath = Join-Path $binDirectory 'maapjsk.cmd'
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
$entries = @($userPath -split ';' | Where-Object { $_ -and $_.TrimEnd('\') -ine $binDirectory.TrimEnd('\') })
if ($Uninstall) {
    # 只移除本脚本生成的单个入口文件，保留项目和运行目录。
    if (Test-Path -LiteralPath $commandPath) { Remove-Item -LiteralPath $commandPath }
} else {
    New-Item -ItemType Directory -Force -Path $binDirectory | Out-Null
    $content = "@echo off`r`nchcp 65001 >nul`r`ncall `"$(Join-Path $projectRoot 'MaaPJSK.cmd')`" %*`r`nexit /b %errorlevel%`r`n"
    [IO.File]::WriteAllText($commandPath, $content, [Text.UTF8Encoding]::new($false))
    $entries += $binDirectory
}
[Environment]::SetEnvironmentVariable('Path', ($entries -join ';'), 'User')
if ($Uninstall) {
    Write-Host '已移除 maapjsk 命令入口。'
} else {
    Write-Host "已安装：$commandPath"
    Write-Host '新开命令行窗口后，在任意目录输入 maapjsk 即可启动。'
}
