param([switch]$PrepareOnly)
$ErrorActionPreference = 'Stop'
$packageRoot = Split-Path -Parent $PSScriptRoot
$marker = Join-Path $packageRoot 'package-manifest.json'
if (-not (Test-Path -LiteralPath $marker)) { throw '缺少 MaaPJSK 发行清单。' }
$manifest = Get-Content -LiteralPath $marker -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.package -ne 'MaaPJSK' -or $manifest.architecture -ne 'win-x64') { throw '发行包身份不匹配。' }
$runtimeRoot = Join-Path $packageRoot 'runtime'
$pythonRoot = Join-Path $runtimeRoot 'python'
$partialRoot = Join-Path $runtimeRoot 'python.partial'
$readyPath = Join-Path $pythonRoot '.maapjsk-ready.json'
$pythonExe = Join-Path $pythonRoot 'python.exe'
$archive = Join-Path $runtimeRoot 'maapjsk-python.zip'
function Assert-RuntimePath([string]$RuntimePath) {
    $absolutePackage = [IO.Path]::GetFullPath($packageRoot).TrimEnd('\')
    $absoluteRuntime = [IO.Path]::GetFullPath($runtimeRoot).TrimEnd('\')
    $absoluteTarget = [IO.Path]::GetFullPath($RuntimePath).TrimEnd('\')
    if (-not $absoluteRuntime.StartsWith($absolutePackage + '\', [StringComparison]::OrdinalIgnoreCase) -or
        -not $absoluteTarget.StartsWith($absoluteRuntime + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '运行环境操作路径不在本包 runtime 内。' }
    $cursor = $absoluteTarget
    while ($cursor -and ($cursor -eq $absolutePackage -or $cursor.StartsWith($absolutePackage + '\', [StringComparison]::OrdinalIgnoreCase))) {
        if (Test-Path -LiteralPath $cursor) {
            if (((Get-Item -LiteralPath $cursor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) { throw '运行环境路径包含重解析点，拒绝解包或删除。' }
        }
        $cursor = Split-Path -Parent $cursor
    }
}
Assert-RuntimePath $pythonRoot
Assert-RuntimePath $partialRoot
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
# 同一目录同时首启时只允许一个解包者；异常退出后文件锁自动释放。
$lock = [IO.File]::Open((Join-Path $runtimeRoot 'bootstrap.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
try {
    if (-not (Test-Path -LiteralPath $readyPath)) {
        if (Test-Path -LiteralPath $pythonRoot) {
            $preparingPath = Join-Path $pythonRoot '.maapjsk-preparing'
            if (-not (Test-Path -LiteralPath $preparingPath) -or ([IO.File]::ReadAllText($preparingPath)).Trim() -ne $manifest.python_archive_sha256) { throw 'Python 目录没有就绪标记，且不是本包中断的解包，请检查目录。' }
            # 只重新处理带本包准备标记的未完成目录，不接管未知运行环境。
            Assert-RuntimePath $pythonRoot
            Remove-Item -LiteralPath $pythonRoot -Recurse -Force
        }
        if (-not (Test-Path -LiteralPath $archive)) { throw '首次启动需要完整包中的 Python ZIP。' }
        $archiveStream = [IO.File]::OpenRead($archive)
        $archiveHasher = [Security.Cryptography.SHA256]::Create()
        try { $archiveHash = ([BitConverter]::ToString($archiveHasher.ComputeHash($archiveStream))).Replace('-', '').ToLowerInvariant() }
        finally { $archiveHasher.Dispose(); $archiveStream.Dispose() }
        if ($archiveHash -ne $manifest.python_archive_sha256) { throw 'Python ZIP 校验失败。' }
        Assert-RuntimePath $partialRoot
        if (Test-Path -LiteralPath $partialRoot) { Remove-Item -LiteralPath $partialRoot -Recurse -Force }
        New-Item -ItemType Directory -Path $partialRoot | Out-Null
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $zip = [IO.Compression.ZipFile]::OpenRead($archive)
        try {
            foreach ($entry in $zip.Entries) {
                $entryName = $entry.FullName.Replace('\', '/')
                if ($entryName.StartsWith('/') -or $entryName.Contains(':') -or ($entryName.Split('/') -contains '..') -or (($entry.ExternalAttributes -shr 16) -band 0xF000) -eq 0xA000) { throw 'Python ZIP 含非法路径。' }
                $destination = [IO.Path]::GetFullPath((Join-Path $partialRoot $entryName))
                if (-not $destination.StartsWith($partialRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Python ZIP 路径越界。' }
                if (-not $entry.Name) { New-Item -ItemType Directory -Force -Path $destination | Out-Null; continue }
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $destination) | Out-Null
                [IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $destination, $false)
            }
        } finally { $zip.Dispose() }
        [IO.File]::WriteAllText((Join-Path $partialRoot '.maapjsk-preparing'), $manifest.python_archive_sha256)
        # 前缀修复在最终目录执行；未完成标记使断电后的下一次启动能重新准备。
        Assert-RuntimePath $partialRoot
        Assert-RuntimePath $pythonRoot
        Move-Item -LiteralPath $partialRoot -Destination $pythonRoot
        $env:PATH = "$pythonRoot;$pythonRoot\Library\bin;$pythonRoot\Scripts;$env:PATH"
        try {
            $unpack = Join-Path $pythonRoot 'Scripts\conda-unpack-script.py'
            if (-not (Test-Path -LiteralPath $unpack)) { $unpack = Join-Path $pythonRoot 'Scripts\conda-unpack' }
            & $pythonExe $unpack
            if ($LASTEXITCODE -ne 0) { throw 'conda-unpack 执行失败。' }
            & $pythonExe -c 'import maa, cv2, numpy, onnxruntime, yaml; import sys; assert sys.version_info[:2] == (3,12)'
            if ($LASTEXITCODE -ne 0) { throw '便携 Python 依赖验证失败。' }
            $readyPartial = $readyPath + '.partial'
            @{schema=1; python_archive_sha256=$manifest.python_archive_sha256} | ConvertTo-Json | Set-Content -LiteralPath $readyPartial -Encoding UTF8
            Move-Item -LiteralPath $readyPartial -Destination $readyPath -Force
            Remove-Item -LiteralPath (Join-Path $pythonRoot '.maapjsk-preparing') -Force
        } catch {
            # 无 ready 的失败解包不能被后续启动误用，目录在本包 runtime 内已固定。
            Assert-RuntimePath $pythonRoot
            Remove-Item -LiteralPath $pythonRoot -Recurse -Force
            throw
        }
    }
    $ready = Get-Content -LiteralPath $readyPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($ready.schema -ne 1 -or $ready.python_archive_sha256 -ne $manifest.python_archive_sha256) { throw 'Python 就绪标记与安装清单不符。' }
    if (-not (Test-Path -LiteralPath $pythonExe)) { throw '就绪 Python 不存在。' }
} finally { $lock.Dispose() }
$env:PATH = "$pythonRoot;$pythonRoot\Library\bin;$env:PATH"
$env:PYTHONUTF8 = '1'
$env:MAAPJSK_TEMPLATE_CONFIG = Join-Path $packageRoot 'config\maapjsk-templates.json'
Push-Location -LiteralPath $packageRoot
try {
    & $pythonExe -X utf8 -m project_sekai.release_package bootstrap --root $packageRoot
    if ($LASTEXITCODE -ne 0) { throw '发行配置初始化失败。' }
} finally { Pop-Location }
if ($PrepareOnly) { return }
Start-Process -FilePath (Join-Path $packageRoot 'MaaPJSK.exe') -WorkingDirectory $packageRoot
