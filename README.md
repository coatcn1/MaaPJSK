# MaaPJSK

基于 MaaFramework / MFAAvalonia 的日服《プロジェクトセカイ カラフルステージ！ feat. 初音ミク》自动演出工具，支持游戏内 AUTO LIVE 和本地 SUS 谱面演奏。

## ✨ 功能

- 🎶 **自动演出**：当前歌曲或随机选曲，使用游戏内 AUTO LIVE 连续运行。
- 🎼 **单人谱面演出**：当前、随机或完整曲名搜索，按本地谱面执行多点触控。
- 🤝 **协力谱面演出**：自由／资深公房，按实际抽选歌曲确认谱面并记录个人成绩。
- ▶️ **一键演出**：手动选歌与准备后，等待最终开场并演奏一曲。
- 🎯 **Native 校准**：排练与正式验证，保存匹配设备环境的校准配置。
- 🔄 **曲库维护**：MFA 内增量同步谱面、歌曲信息与封面。
- 🔋 **体力与演奏设置**：按用户指定消耗和饮料补足体力，支持偏移设置与自动保存。
- 📺 **广告奖励**：独立选择的有限广告请求与返回流程。
- 🧾 **演出记录**：成绩、失败证据与可选诊断日志，支持清理缓存。

谱面演奏按照本地 SUS 时序派发输入，屏幕识别负责导航、身份确认、首音同步和保护；当前不提供实时音符识别演奏。

## 🚀 快速开始

1. 从 [Releases](https://github.com/coatcn1/MaaPJSK/releases) 下载完整 Windows x64 ZIP，完整解压。
2. 双击 `MaaPJSK.cmd`，等待首次运行环境准备完成。
3. 在 MFA 中连接自己的 MuMu，在「谱面管理」同步曲库。
4. 在「演奏设置」确认引擎、偏移和体力，再选择任务、难度与次数，手动开始。

完整包包含运行环境，无需额外安装 Python 或 .NET。安装与更新见 [Windows 运行包](docs/release-package.md)，任务配置见[使用指南](docs/usage.md)。

## 🖥️ 环境要求

| 项目 | 要求 |
| --- | --- |
| 操作系统 | Windows x64 |
| 游戏 | 日服 Project SEKAI |
| 模拟器基准 | MuMu，横屏 1280 × 720，240 DPI |
| 演奏环境 | 引擎、设备、流速及画质与校准环境保持一致 |

其他设备、区服和分辨率需要另行适配；各模式的适用范围与限制见对应文档。

## 📚 文档

| 文档 | 内容 |
| --- | --- |
| [使用指南](docs/usage.md) | 安装连接、任务选择、体力、日志与缓存 |
| [单人谱面演出](docs/solo-chart-live.md) | 选曲、难度、演奏设置与成绩 |
| [协力谱面演出](docs/cooperative-chart-live.md) | 公房流程、恢复与任务边界 |
| [一键演出](docs/one-shot-chart-live.md) | 手动准备与开场等待 |
| [Native 与校准](docs/native-calibration.md) | 引擎、校准和解析候选 |
| [日服谱面库](docs/sekai-chart-repository.md) | 数据来源、增量维护与本地校验 |
| [广告奖励](docs/ad-rewards.md) | 独立广告任务及返回流程 |
| [演奏诊断](docs/performance-trace.md) | 日志、报告和回放证据 |
| [Windows 运行包](docs/release-package.md) | 安装、更新与数据保留 |
| [v0.9.0 发行说明](docs/release-notes-v0.9.0.md) | 本版变化与已知限制 |
| [更新记录](CHANGELOG.md) | 版本变更与历史记录 |

## 🛠️ 源码开发

环境准备、构建、固定 PowerShell 启动入口与验证步骤见 [CONTRIBUTING.md](CONTRIBUTING.md)；Agent 协作约束见 [AGENTS.md](AGENTS.md)。源码仓库不附带游戏完整截图、识别模板、谱面、运行包或本机配置。

本项目是非官方研究工具。
