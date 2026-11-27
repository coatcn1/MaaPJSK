# 开发与贡献

开始修改前阅读 [AGENTS.md](AGENTS.md)、[CHANGELOG.md](CHANGELOG.md) 和受影响模块的说明，核对当前分支、远端与工作树，保留已有改动。使用方法见[使用指南](docs/usage.md)。

## 运行环境

- Python 使用工作区上级 `.tools/Miniconda3/envs/maabangdream/python.exe`，固定 CPython 3.12 x64，不临时切换系统 Python。
- MaaFramework Python Binding 固定 `maafw==5.10.2`，通用 MFAAvalonia 固定 v2.12.0。
- Native 使用 pybind11 与 Zig；MFA 页面构建需要 .NET 10 SDK。
- 固定依赖组合见 [runtime-compatibility.json](runtime-compatibility.json)。

在仓库根目录准备依赖：

```powershell
$pjskPython = Join-Path (Split-Path -Parent (Get-Location).Path) '.tools/Miniconda3/envs/maabangdream/python.exe'
& $pjskPython -m pip install -r requirements.txt
& $pjskPython scripts/setup-song-ocr.py
```

首次克隆还需要准备本机模板：截图放在 `.local/captures/`，按 [模板清单示例](examples/template-manifest.json) 生成 `.local/config.json` 和模板。公开仓库不附游戏完整截图、识别模板、下载曲库或本机配置。

```powershell
& $pjskPython scripts/build_templates.py
```

## 构建

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-native.ps1 -Python $pjskPython
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-chart-settings.ps1
```

设置页和通知样式从固定 MFA 标签导出到本项目 `.local/` 后应用覆盖，不修改相邻 MaaBanGDream 或其定制 MFA。构建器保留第三方许可证并检查原始库的兼容性哈希；升级固定 MFA 版本须重新确认，不能直接覆盖官方 DLL。

可选紧凑通知样式的构建方式：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-compact-toasts.ps1 -MfaSource "<MFA源码目录>" -RuntimeSource "<通用MFA目录>"
```

构建产物位于 `.local/compact-toasts/`；移走该目录的 `SukiUI.dll` 后重新部署可恢复通用 MFA 样式。页面及任务防息屏说明见 [MFA 页面开发说明](mfa-chart-ui/README.md)。

## 固定启动入口

从任意目录使用同一条命令：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<项目完整路径>\scripts\launch-mfa.ps1"
```

脚本定位自己的仓库，部署到 `.local/mfa-generic/` 后打开 MFA，不自动开始任务。默认读取工作区上级 `.tools/MFAAvalonia` 与固定 Python 环境；其他机器可用 `-RuntimeSource` 和 `-Python` 指定位置。普通使用无需额外参数或专用候选启动器；开发版谱面修复的生效状态与回退方式见 [Native 与校准](docs/native-calibration.md)。

源码和已运行的 Agent 是不同状态。修改后先停止任务并关闭本项目 MFA，再运行原命令；已有窗口不会被重部署。部署保留用户演奏设置、任务次数、消耗、Profile 和曲库，本地曲库共用仓库中的 `resource/charts/`。

仅部署时使用 `-StageOnly`，不会打开窗口；本项目 MFA 必须先关闭，其他项目的 MFA 可以继续运行。普通启动仍检查 MFA 互斥。`-VerifyAdbEndpoint` 仅供诊断保存的连接端点，正常启动不需要；不修改 PATH、PowerShell Profile 或创建额外命令入口。

## 验证

Python 逻辑先跑受影响测试。涉及任务入口、恢复、持久化、Native 或核心生命周期时执行完整回归；Native 改动先构建，MFA 页面另跑离屏检查。

```powershell
& $pjskPython -X utf8 -m unittest discover -s tests -v
dotnet run --project tests/MfaChartSettingsSmoke/MfaChartSettingsSmoke.csproj -c Release -r win-x64 -- $pjskPython '.local/chart-settings/settings-offscreen.png'
git diff --check
```

纯文档修改检查本地链接、标题结构、代码围栏与差异。不要把当前测试数量作为永久门槛。

自动测试、离线回放、部署哈希、连接检查与真实演出验收分别报告。设备测试沿用 [AGENTS.md](AGENTS.md) 的授权、进程互斥、资源消耗、防息屏和触点释放要求；用户要求停止后不自行恢复。独立 CLI `scripts/test_solo_live.py` 与 MFA 互斥，不作为普通启动替代。

`main.py` 仅保留为旧版页面诊断入口。其 `inspect` 与 `auto-live --dry-run` 使用独立 ADB，不连接 MFA 控制器；即使只读操作也须遵守设备操作的授权与委派要求。

## 文档、提交与发行

- README 保持功能概览和使用入口；详细使用写 `docs/`，开发命令写本文件，长期 Agent 约束写 `AGENTS.md`，修改记录写 `CHANGELOG.md`。
- 沿用独立 `codex/<主题>` 分支，小而完整地提交；提交前检查差异、未跟踪文件及敏感信息。
- 不提交本机路径、设备信息、配置、Profile、日志、截图、缓存、下载谱面或编译产物。
- 提交、推送、合并和发行分别遵循用户授权及仓库门禁；Windows 打包和更新见[发行说明](docs/release-package.md)。
