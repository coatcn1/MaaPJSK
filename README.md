# MaaPJSK

面向日服《プロジェクトセカイ カラフルステージ！ feat. 初音ミク》的 MaaFramework / MFAAvalonia 自动演出项目。当前任务通过 **MFA 中已连接的 MuMu 控制器**运行：从主页进入单人 Live，使用选曲页当前歌曲或点击随机选曲按钮，选择 MASTER 难度，打开游戏内 AUTO LIVE，按指定次数循环，并以 Android BACK（模拟器 ESC）推进结算，直到重新确认主页。这里使用游戏提供的 AUTO LIVE，没有实时识别音符或按谱面辅助演奏。

当前模板来自本机日服 MuMu 平板环境：1280×720、240 DPI。任务开始前检查截图尺寸、DPI 和前台包名 `com.sega.pjsekai`。每局开始前，未识别到主页会发送 Android BACK（模拟器 ESC），最多 12 次，连续两次确认主页后继续；识别到仍在演奏时停止返回。连接中断、用户停止任务或等待超时均会中止。结算中不识别具体奖励按钮，只以主页作为结束标志。

## 在 MFA 中运行

1. 准备通用版 MFAAvalonia 运行目录和安装了 `maafw`、`numpy`、`opencv-python` 的 Python 环境。本机启动脚本默认读取相邻工作区的 `.tools/MFAAvalonia` 与 `.tools/Miniconda3/envs/maabangdream/python.exe`。可通过参数指定其他路径。
2. 把本机页面截图放入 `.local/captures/`，依照 `examples/template-manifest.json` 生成 `.local/config.json` 和模板。当前机器已有这些文件；截图和模板不会提交到 Git。
3. 在 MuMu 中打开日服游戏，确认分辨率为 1280×720、DPI 为 240，然后运行：

```powershell
python scripts/build_templates.py
powershell -ExecutionPolicy Bypass -File scripts/launch-mfa.ps1
```

指定其他通用 MFA 与 Python 路径时：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/launch-mfa.ps1 -RuntimeSource "<通用 MFA 目录>" -Python "<python.exe 路径>"
```

启动脚本将程序部署到 `.local/mfa-generic/`，使用项目自己的 `interface.json`、Agent、Pipeline 与模板，并将 Agent 的 Python 路径写成部署时指定的解释器。MFA 窗口标题应显示 `Project SEKAI 自动化（MaaFramework）`，任务列表只有「🎶 自动演出」，不应出现 MaaBanGDream 的演出设置。脚本默认禁止任务随程序启动。需要重新部署时，先在 MFA 停止任务并关闭窗口。

在 MFA 中选择自己的 MuMu ADB 连接，勾选「🎶 自动演出」，设置：

| 选项 | 作用 |
| --- | --- |
| 选曲方式 | 默认使用选曲页当前歌曲，不搜索特定曲目；随机模式每局点击「决定」左下角的交叉箭头，并确认右侧封面变化后继续。 |
| 演出次数 | 1–999 局；每局从主页重新选曲。 |
| 体力不足时使用道具 | 默认关闭。可选择小饮料（每瓶 +1）或大饮料（每瓶 +10）。AUTO 开启失败时关闭提示、打开回复页；用药后确认 AUTO 开启再继续。 |
| 每次回复饮料数量 | 1–99 瓶，默认 1；使用指定种类，逐瓶确认选择和体力变化。 |

游戏内单局消耗 Live Bonus 的设置由游戏管理；本机目前设为 5。每次 AUTO 因体力不足无法开启时，使用设置的种类和瓶数回复；后续局再次缺体力时仍按相同设置回复，没有任务累计瓶数上限。日志显示每瓶的确认结果。AUTO 体力不足提示、用药后回复完成提示均发送一次 ESC/BACK 关闭并确认准备页，不点击空白处；用药前的二次确认仍点击 OK。回复页已处于道具标签时不重复切换，等待动画后再选药；不识别饮料初始数量、已选数量或库存数字。每瓶先把小、大饮料滑条复位，再点击所选饮料加号一次，等待决定按钮启用；最多重试三次，每次重试先复位滑条，避免重复加药。回复页的“决定”按钮可能因广告入口出现而右移，脚本按模板的实际位置点击。用药后仅重试一次 AUTO，仍失败则停止。自动回复不使用水晶或广告；OK 用药已实机确认体力从 0 变为 10；新增的 ESC 提示关闭及完整连续演出流程仍待实机验收。

`scripts/launch-mfa.ps1 -StageOnly` 只更新 `.local/mfa-generic/`，不启动窗口。页面失败截图会写到部署目录的 `config/failure-*.png`，Agent 日志和 MFA 日志可用于复核。

## 在任意目录启动

在项目目录安装一次命令入口（只写入当前用户 PATH，无需管理员权限）：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install-launcher.ps1
```

新开 PowerShell 或 CMD 窗口后，在任意目录输入：

```powershell
maapjsk
maapjsk -Help
```

`maapjsk` 支持上述 `-RuntimeSource`、`-Python`、`-StageOnly` 参数。项目根目录的 `MaaPJSK.cmd` 也可通过完整路径调用；两种入口均按脚本所在目录定位项目。程序已打开时直接提示已运行。移动项目后重新运行安装脚本更新入口；卸载入口使用 `scripts/install-launcher.ps1 -Uninstall`。

## 演出进度与通知

MFA 任务日志在开始时显示 `自动演出：已完成 0 / 总数 N`，每局演出、结算并返回主页后更新完成次数。失败或中途停止的局数不计入完成数；错误原因放在任务日志，通知只显示简短状态。

本机部署采用紧凑通知样式，覆盖 MFA 自带的完成、终止等通知以及本项目新增通知。样式宽度范围从 300–400 调为 240–320，并缩小字体、图标和留白。源码仓库提供可复现的构建脚本，不附带修改后的第三方二进制。

需要 .NET SDK、Git、通用 MFA **v2.12.0** 运行目录，以及包含 `v2.12.0` 标签的 [MFAAvalonia 源码](https://github.com/MaaXYZ/MFAAvalonia)。关闭 MFA 后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-compact-toasts.ps1 -MfaSource "<MFA源码目录>" -RuntimeSource "<通用MFA目录>"
maapjsk -RuntimeSource "<通用MFA目录>" -Python "<python.exe路径>"
```

脚本从固定标签导出 SukiUI，仅修改通知样式，生成到 `.local/compact-toasts/`；部署前检查原始 UI 库的 SHA256 是否与构建时一致。升级 MFA 后须重新确认兼容性和构建。删除 `.local/compact-toasts/SukiUI.dll` 后重新部署，可恢复通用 MFA 的默认通知样式。MFA/SukiUI 的源码和许可证保留在原项目中，构建不修改本地 MFA 仓库。

## 旧版独立 ADB 入口

`main.py` 仍保留为页面模板检查和单机诊断入口；正常使用应从 MFA 启动。它需要显式指定 ADB 程序和设备地址，不使用 MFA 控制器。`inspect` 仅读取当前页面，`auto-live --dry-run` 只确认主页并显示下一步，不发送点击：

```powershell
python main.py --adb "<adb.exe 路径>" --serial "<模拟器 ADB 地址>" inspect
python main.py --adb "<adb.exe 路径>" --serial "<模拟器 ADB 地址>" auto-live --dry-run
```

## 谱面辅助演奏的参考

后续若制作按谱面预定时序输入的功能，可参考 [AutoSekai](https://github.com/dogwong/AutoSekai) 的 SUS 谱面解析与 HID 触控调度、[MikuMikuWorldForProsekaR](https://github.com/Choccodrize/MikuMikuWorldForProsekaR) 的 SUS 编辑/查看能力，以及 [pjsekai-scores](https://github.com/Sekai-World/pjsekai-scores) 的 SUS 读取方式。目前仓库没有实现这一功能，也不包含或分发游戏谱面。

## 验证与限制

```powershell
python -m unittest discover -s tests -v
```

37 项自动测试覆盖选曲、配置传递、AUTO 失败后的提示关闭、指定数量、后续局再次缺体力时回复、两种决定按钮布局、加号首次无响应的重试、无饮料数量模板仍能完成回复、重试前复位滑条不叠加选药、控制器点击失败、OK 二次确认、延迟体力更新、ESC 通知关闭顺序、回复保护、计数及 ESC 兜底；实际失败截图用于离线回放。连接自检已在本机对 MuMu 完成：MaaFramework 资源、Agent、控制器、只读设备预检和嵌套日志节点均通过。启动脚本已从其他目录调用验证；紧凑 UI 库已编译并在 MFA 窗口确认显示。选曲页的随机按钮已实机点击并确认会切换歌曲；随机模式从主页到准备页的导航也已实机验证。用药的 OK 已实机确认体力从 0 变为 10；新增 ESC 提示关闭和完整连续演出流程仍需在游戏中验收。换设备、区服、分辨率或游戏 UI 后须重新采样模板。

本项目是非官方研究工具。公开仓库仅包含源码、模板裁剪清单和说明，不附带游戏截图、识别模板、谱面、MFA 运行包或本机配置；首次克隆须自行准备依赖和截图。
