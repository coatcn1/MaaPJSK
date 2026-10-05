param([string]$Python)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
if (-not $Python) { $Python = Join-Path (Split-Path -Parent $projectRoot) '.tools\Miniconda3\envs\maabangdream\python.exe' }
$nativeRoot = Join-Path $projectRoot 'native'
$outputRoot = Join-Path $projectRoot 'project_sekai\native'
New-Item -ItemType Directory -Force -Path $outputRoot | Out-Null
$paths = (& $Python -X utf8 -c "import json,sysconfig,pybind11,ziglang,pathlib; print(json.dumps([str(pathlib.Path(ziglang.__file__).parent/'zig.exe'),pybind11.get_include(),sysconfig.get_paths()['include'],str(pathlib.Path(sysconfig.get_config_var('prefix'))/'libs')]))") | ConvertFrom-Json
if ($LASTEXITCODE -ne 0) { throw '固定 Python 环境需要 pybind11 与 ziglang 构建依赖。' }
$sources = @(Get-ChildItem -LiteralPath (Join-Path $nativeRoot 'src') -Filter '*.cpp' | Sort-Object Name | ForEach-Object { $_.FullName })
$output = Join-Path $outputRoot 'maapjsk_native.pyd'
$arguments = @('c++','-target','x86_64-windows-gnu','-O2','-shared','-std=c++17','-DNDEBUG','-Wno-nullability-completeness',"-I$(Join-Path $nativeRoot 'include')","-I$($paths[1])","-I$($paths[2])") + $sources + @('-o',$output,"-L$($paths[3])",'-lpython312','-lws2_32')
& $paths[0] @arguments
if ($LASTEXITCODE -ne 0) { throw 'PJSK Native 编译失败' }
Push-Location -LiteralPath $projectRoot
try { & $Python -X utf8 -c "from project_sekai import native_engine; assert native_engine.available(), native_engine.unavailable_reason(); print('Native '+native_engine.module().version())" }
finally { Pop-Location }
if ($LASTEXITCODE -ne 0) { throw 'PJSK Native 无法在固定 Python 环境导入' }
Write-Host 'PJSK Native 已编译。'
