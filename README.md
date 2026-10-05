# MaaPJSK

面向日服《プロジェクトセカイ カラフルステージ！ feat. 初音ミク》的 MaaFramework / MFAAvalonia 自动演出项目。任务通过 **MFA 中已连接的 MuMu 控制器**运行：提供游戏内 AUTO LIVE 循环，以及按本地 SUS 时序触控的单人、协力谱面演出。从主页选取当前歌曲或随机歌曲，按指定次数运行，并以 Android BACK（模拟器 ESC）推进正常结算。谱面任务默认关闭，每局体力消耗由用户决定，可沿用游戏设置或指定 0–10；支持体力不足时按指定种类和瓶数自动用药，并记录实际判定数字。

当前模板来自本机日服 MuMu 平板环境：1280×720、240 DPI。任务开始前检查截图尺寸、DPI 和前台包名 `com.sega.pjsekai`。自动演出与单人任务每局开始前，未识别到主页会发送 Android BACK（模拟器 ESC），最多 12 次，连续两次确认主页后继续；识别到仍在演奏时停止返回。检测到 TAP TO START 标题页时停止返回并保留已完成次数，避免因无法登录而持续发送 ESC。自动演出与单人任务在连接中断、用户停止或等待超时后中止；协力普通故障进入有界恢复或可取消等待，详见协力说明。正常结算不识别具体奖励按钮，以实际主页为终点。

## 在 MFA 中运行

1. 准备通用版 MFAAvalonia 运行目录与 Python 环境，运行 `python -m pip install -r requirements.txt`。谱面任务另需 `python scripts/setup-song-ocr.py` 准备离线模型。本机启动脚本默认读取相邻工作区的 `.tools/MFAAvalonia` 与 `.tools/Miniconda3/envs/maabangdream/python.exe`。可通过参数指定其他路径。
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

启动脚本将程序部署到 `.local/mfa-generic/`，使用项目自己的 `interface.json`、Agent、Pipeline 与模板，并将 Agent 的 Python 路径写成部署时指定的解释器。MFA 窗口标题应显示 `Project SEKAI 自动化（MaaFramework）`，任务列表提供「🎶 自动演出」「🎼 单人谱面演出」「🤝 协力谱面演出」「▶️ 一键演出」和「🎯 Native 谱面校准」。脚本默认禁止任务随程序启动。需要重新部署时，先停止任务并关闭本项目 MFA。

在 MFA 中选择自己的 MuMu ADB 连接，勾选「🎶 自动演出」，设置：

| 选项 | 作用 |
| --- | --- |
| 选曲方式 | 默认使用选曲页当前歌曲，不搜索特定曲目；随机模式每局点击「决定」左下角的交叉箭头，并确认右侧封面变化后继续。 |
| 演出次数 | 1–999 局；每局从主页重新选曲。 |
| 体力不足时使用道具 | 默认关闭。可选择小饮料（每瓶 +1）或大饮料（每瓶 +10）。AUTO 开启失败时关闭提示、打开回复页；用药后确认 AUTO 开启再继续。 |
| 每次回复饮料数量 | 1–99 瓶，默认 1；使用指定种类，逐瓶确认选择和体力变化。 |

游戏内单局消耗 Live Bonus 的设置由游戏管理；本机目前设为 5。每次 AUTO 因体力不足无法开启时，使用设置的种类和瓶数回复；后续局再次缺体力时仍按相同设置回复，没有任务累计瓶数上限。日志显示每瓶的确认结果。AUTO 体力不足提示、用药后回复完成提示均发送一次 ESC/BACK 关闭并确认准备页，不点击空白处；用药前的二次确认仍点击 OK。回复页已处于道具标签时不重复切换，等待动画后再选药；不识别饮料初始数量、已选数量或库存数字。每瓶先把小、大饮料滑条复位，再点击所选饮料加号一次，等待决定按钮启用；最多重试三次，每次重试先复位滑条，避免重复加药。回复页的“决定”按钮可能因广告入口出现而右移，脚本按模板的实际位置点击。用药后仅重试一次 AUTO，仍失败则停止。自动回复不使用水晶或广告。2026-10-03 本机日志记录了大饮料回复、ESC 关闭提示、重新开启 AUTO 和 30/30 完成；标题页异常处理仍需在实际异常场景验收。

「🎼 单人谱面演出」的 **谱面触控偏移** 和 **每局体力消耗检测** 在 **MFA 设置 → 演奏设置** 中保存。体力默认沿用游戏设置，也可指定 0–10；任务结束后保留用户选择的消耗量。该任务提供 **体力不足时使用道具**、**每次回复饮料数量** 设置，开演前检查实际体力；不足时按指定数量回复，保持 AUTO 关闭。选择 0 时不触发用药；一批饮料用完仍不足时停止并提示，不自行增加瓶数或降低消耗。

### Native 演奏与校准

先用固定 Python 环境安装 `pybind11`、`ziglang`，运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-native.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-chart-settings.ps1
```

重新部署 MFA 后，在「演奏设置」中选择 Native 引擎。启用校准配置时，先运行
「🎯 Native 谱面校准」，选择当前或随机歌曲以及难度。该任务按 **演奏设置中的体力数量、不用药**
完成一局排练和一局正式验证，验证通过后保存当前设备与难度的配置。
校准以实际 FAST 与 LATE（SLOW）平衡为目标，不因 MISS 等其他判定单独拒绝；仍要求 LIVE CLEAR、音数匹配、完整输入与释放，具体容差见 [Native 与校准](docs/native-calibration.md)。
正常任务使用的偏移是 **已验证校准偏移 + 手动触控偏移**；正值延后、负值提前。
每局开演前固定歌曲相位和偏移，设备回执用于补偿未编译的未来窗口。
引擎、设备、游戏版本、控制器链路或流速变化时须重新校准。

Native 参考 MaaBanGDream 的设备队列、逐命令耗时回读和释放确认，适配 PJSK
十二轨 SUS 与滑动坐标；具体来源、保存位置和验证标准见 [Native 与校准说明](docs/native-calibration.md)。

`scripts/launch-mfa.ps1 -StageOnly` 只更新 `.local/mfa-generic/`，不启动窗口；其他项目的 MFA 可以继续运行，但本项目 MFA 必须先关闭。正常启动仍检查控制器互斥。页面失败截图会写到部署目录的 `config/failure-*.png`，Agent 日志和 MFA 日志可用于复核。

MFA「性能设置」中的「任务运行中阻止息屏」开启后，只在实际任务执行期间阻止 Windows 自动息屏和休眠；完成、失败或停止后释放请求，空闲窗口不阻止息屏。原来已勾选的设置继续生效。构建、配置兼容与验证方法见 [MFA 页面及任务防息屏说明](mfa-chart-ui/README.md)。

## 在任意目录启动

直接使用脚本的完整路径，无需安装命令入口或修改 PATH、PowerShell Profile：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "<项目完整路径>\scripts\launch-mfa.ps1"
```

需要排查模拟器连接时，可追加可选参数 `-VerifyAdbEndpoint`，在启动前只读检查最后使用实例保存的 ADB 端点是否处于 `device` 状态；首次使用尚未保存设备时，进入 MFA 后选择模拟器。已保存端点尚不可用时会提示在 MFA 中刷新或重新连接，仍允许打开窗口。启动脚本默认只部署并打开 MFA，演出任务由用户在 MFA 中启动。

脚本按自身目录定位资源，支持 `-RuntimeSource`、`-Python`、`-StageOnly` 和 `-Help`；不依赖当前命令行所在目录。启动前关闭其他 MFA / MaaBanGDream 实例；重新部署前停止任务并关闭本项目窗口。

## 演出进度与通知

MFA 任务日志在开始时显示 `自动演出：已完成 0 / 总数 N`，每局演出、结算并返回主页后更新完成次数。失败或中途停止的局数不计入完成数；错误原因放在任务日志，通知只显示简短状态。

本机部署采用紧凑通知样式，覆盖 MFA 自带的完成、终止等通知以及本项目新增通知。样式宽度范围从 300–400 调为 240–320，并缩小字体、图标和留白。源码仓库提供可复现的构建脚本，不附带修改后的第三方二进制。

需要 .NET SDK、Git、通用 MFA **v2.12.0** 运行目录，以及包含 `v2.12.0` 标签的 [MFAAvalonia 源码](https://github.com/MaaXYZ/MFAAvalonia)。关闭 MFA 后运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-compact-toasts.ps1 -MfaSource "<MFA源码目录>" -RuntimeSource "<通用MFA目录>"
powershell -NoProfile -ExecutionPolicy Bypass -File "<项目完整路径>\scripts\launch-mfa.ps1" -RuntimeSource "<通用MFA目录>" -Python "<python.exe路径>"
```

脚本从固定标签导出 SukiUI，仅修改通知样式，生成到 `.local/compact-toasts/`；部署前检查原始 UI 库的 SHA256 是否与构建时一致。升级 MFA 后须重新确认兼容性和构建。删除 `.local/compact-toasts/SukiUI.dll` 后重新部署，可恢复通用 MFA 的默认通知样式。MFA/SukiUI 的源码和许可证保留在原项目中，构建不修改本地 MFA 仓库。

## 旧版独立 ADB 入口

`main.py` 仍保留为页面模板检查和单机诊断入口；正常使用应从 MFA 启动。它需要显式指定 ADB 程序和设备地址，不使用 MFA 控制器。`inspect` 仅读取当前页面，`auto-live --dry-run` 只确认主页并显示下一步，不发送点击：

```powershell
python main.py --adb "<adb.exe 路径>" --serial "<模拟器 ADB 地址>" inspect
python main.py --adb "<adb.exe 路径>" --serial "<模拟器 ADB 地址>" auto-live --dry-run
```

## 谱面辅助演奏的参考

已接入日服在线谱面库：**MFA 设置 → 谱面管理 → 增量更新谱面**。同步已公开歌曲的六种实际难度、日文标题等元数据及 PNG 封面，未变文件通过本地校验后直接复用，只处理新增、变化、缺失或损坏资源。近三个月的资源每七天复查，也可点击 **检查近期资源**；超过三个月的完整未变资源不再联网复查。支持取消、续传和错误记录。界面构建方式、命令行、数据布局与验证见 [日服谱面库说明](docs/sekai-chart-repository.md)。本机 2026-10-04 增量更新后取得 3748 张谱面、723 张封面，复用 4465 个资源，仅新增下载 1.23 MiB；另两首歌曲的 12 个资源在镜像返回 404。

「🎼 单人谱面演出」已接入准备页与最终封面的歌曲确认、六种难度配置、本地 SUS 解析、多点触控调度及结算判定报告。使用方法、统计定义、资源与验收边界见 [单人谱面演出说明](docs/solo-chart-live.md)。源码仓库不包含或分发游戏谱面。

「🤝 协力谱面演出」为默认关闭的公房任务，支持自由／资深房间、当前歌曲／随机（おまかせ）、五种已采样难度和指定次数。按实际抽选歌曲确认谱面，记录个人判定并回游戏主页；普通导航、身份、缺谱、首音、输入及结算故障保存证据后可取消等待恢复，不消耗演出次数；仅真实演出死亡先释放触点，再用 HOME 返回模拟器主页并结束整批。释放未知时不追加页面输入；公房演奏、最终封面、加载和未知页不盲退房。体力和手动偏移沿用演奏设置，暂不复用单人校准配置。本机资深公房随机 EXPERT 已完成连续二十局流程，其中十八局形成完整自动成绩、两局成绩漏读后警告继续；异常和其他模式仍须独立验收，见 [协力谱面演出说明](docs/cooperative-chart-live.md)。

「▶️ 一键演出」默认关闭，只等待最终开场封面与标题，最长五分钟；识别后按任务选择的难度演奏一曲，完整输入并确认生命栏消失或 LIVE CLEAR 后停止，保留当前页面。手动选曲和准备时须使游戏难度与任务设置一致；任务不操作体力、不使用单人校准配置、不推进结算。使用与验收边界见 [一键演出说明](docs/one-shot-chart-live.md)。

## 验证与限制

```powershell
python -m unittest discover -s tests -v
```

Python 自动测试覆盖演出、回复、返回、谱面库、SUS 时间与触点生命周期、歌曲确认、Native 回执、生命保护及判定采集。MFA 谱面设置页另通过离屏验证，检查按钮与中文进度、子进程退出、取消、任务启动保护和本地索引保护。连接自检已在本机对 MuMu 完成：MaaFramework 资源、Agent、控制器、只读设备预检和嵌套日志节点均通过。启动脚本已从其他目录调用验证；紧凑 UI 库已编译并在 MFA 窗口确认显示。选曲页的随机按钮已实机点击并确认会切换歌曲；随机模式从主页到准备页的导航也已实机验证。2026-10-03 日志记录了用药后 ESC 关闭提示与 30/30 完成；新的标题页保护仍待实际异常场景验收。换设备、区服、分辨率或游戏 UI 后须重新采样模板。

早期逐条触控版本通过「エメラルド」NORMAL Lv.13 连续两局 0 体力验收，本机流速 8.00、任务触控偏移 +30 ms：分别 381 PERFECT，以及 376 PERFECT / 5 GREAT，两局 GOOD/BAD/MISS 为 0，完成日志 2/2，JSON 与 CSV 均已记录。APPEND Lv.30 及返回 NORMAL 的准备页选择已确认。Native 的 MASTER、5 体力测试和生命保护验收见 [Native 与校准说明](docs/native-calibration.md)。偏移应按本机链路校准；这些成绩不代表其他曲目、难度或设备上的准确率。

补齐开场持续检测及 MuMu extras／MaaTouch 部署后的逐条触控版本，NORMAL 零体力完整流程通过 1/1，实际 243 PERFECT / 33 GREAT / 105 GOOD / 0 BAD / 0 MISS。该版本通过第一音同步和固定手动偏移演奏；Native 和校准任务的当前验收单独记录于 [Native 与校准说明](docs/native-calibration.md)。旧链路的偏移不能直接作为新链路的校准值。

本项目是非官方研究工具。公开仓库仅包含源码、模板裁剪清单和说明，不附带游戏截图、识别模板、谱面、MFA 运行包或本机配置；首次克隆须自行准备依赖和截图。
