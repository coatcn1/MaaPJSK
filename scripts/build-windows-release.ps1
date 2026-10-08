param(
    [string]$Version = '0.8.0',
    [Parameter(Mandatory=$true)][string]$PythonArchive,
    [Parameter(Mandatory=$true)][string]$MfaPublish,
    [string]$Python,
    [string]$Output,
    [switch]$AllowDirty
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path (Split-Path -Parent $projectRoot) '.tools\Miniconda3\envs\maabangdream\python.exe' }
if (-not $Output) { $Output = Join-Path $projectRoot '.local\release' }
$arguments = @('-X','utf8','-m','project_sekai.release_package','build','--root',$projectRoot,'--mfa',$MfaPublish,'--python-archive',$PythonArchive,'--output',$Output,'--version',$Version)
if ($AllowDirty) { $arguments += '--allow-dirty' }
Push-Location -LiteralPath $projectRoot
try { & $Python @arguments; if ($LASTEXITCODE -ne 0) { throw '发行包构建失败。' } }
finally { Pop-Location }
