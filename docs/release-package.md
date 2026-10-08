# Windows 便携发行包

v0.8.0 面向 Windows x64、日服 MuMu 横屏 1280×720／240 DPI。完整包 `MaaPJSK-v0.8.0-win-x64.zip` 包含自包含 .NET 10 主程序、CPython 3.12.13 便携运行环境、MaaFramework 5.10.2、Native 1.2.0、离线 OCR 和必要 UI 裁剪模板。依赖版本见 `runtime-compatibility.json`，授权来源见 `THIRD-PARTY-NOTICES.md`。

完整 ZIP 和 `MaaPJSK-v0.8.0-win-x64-update.zip` 都采用扁平根目录，并各附 `.sha256`。第一次使用解压完整包，运行 `MaaPJSK.cmd`。首次以 .NET SHA256 校验并解包 Python ZIP、执行 conda-unpack，成功后写就绪标记；再次启动复用运行环境。支持目录手动改名及中文／空格路径，每次启动重绑定服务路径，安装目录内的曲库随改名重定位，用户外部曲库与清单原路径保持。不要单独直接运行 EXE 绕过首启准备。

首次启动生成本机配置，偏移为 0，不附机器校准 Profile、不自动开始任务，发行整包更新固定使用 GitHub。发行包不含曲库；先在谱面管理页同步，连接自己的 MuMu 后由用户选择任务。发行包内没有设备端点、任务历史、日志、完整截图、用户配置或 `.pdb`。

便携包内资源更新和 MFA 软件更新都由本项目整包更新流程接管。它只接受本项目稳定标签的 win-x64 更新 ZIP 和匹配 SHA256 sidecar；REST 限流时使用 GitHub latest／expanded_assets 读取元数据，不使用 MirrorCDK。没有发行清单的开发目录仍保留固定 MFA 原行为。

任务或谱面同步运行中拒绝更新。下载／逐文件哈希校验在写入前完成，随后正常退出 GUI，助手等待本安装目录的 GUI 和 Agent 全部退出。只合并发行清单管理的文件；旧清单多余文件保守保留，绝不清空 resource。`config`、`profiles`、`debug`、日志、截图、`runtime`、`resource/charts` 及用户校准都保留，OCR 随整包管理。版本清单在其他文件成功后最后写入；失败回退已写文件，助手错误保留在临时更新目录。恢复启动通过 `scripts/start-release.ps1`。

本地更新也必须同时提供 `*-update.zip` 与同名 `.sha256`，可用 MFA 的本地资源更新入口。仅当任务已停止且释放完成才开始更新；校验失败不会退回上游覆盖更新。便携 Python 依赖变化需要新的完整包，不用更新 ZIP 替换用户运行环境。

更新 helper 继承环境／系统 HTTP 代理；MFA GUI 中单独设置的代理尚未传入 helper，尤其 SOCKS5 不属于当前已支持或已验证范围。

2026-10-08 本机新版便携包探测在不含开发环境的 PATH 下通过中文／空格目录测试：首次准备 10.593 秒，再次 0.437 秒，运行环境就绪文件字节及修改时间保持；手动目录改名后准备 0.516 秒，同步服务路径正确重绑定，Native 1.2.0 隔离导入通过。把安装清单合成设为 0.7.9 后，以实际 0.8.0 更新包成功应用 557 个受控文件，偏移 37、消耗 5、合成实例标记／校准 Profile／曲库 SUS、profiles 和 debug 均按字节保留；更新后准备 0.406 秒。此次是合成旧版本标记的更新验收，不表示曾发布 v0.7.9；设备输入为零，不等同真实演出或 GUI 更新交互验收。

同次探测还确认全新、无设备端点的 GUI 正常启动，窗口显示 MFA v2.12.0 与 Project SEKAI v0.8.0；正常关闭在十秒内退出且没有遗留新进程。该项只检查窗口句柄、标题和日志，没有启动任务或发送设备输入，不代表完整 GUI 更新交互已验收。Python 完整回归、发行定向回归及 MFA 离屏检查均通过。

构建入口：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-chart-settings.ps1 -ReleasePackage -PublishOutput .local/release-v0.8.0/mfa-self-contained
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/build-windows-release.ps1 -Version 0.8.0 -MfaPublish .local/release-v0.8.0/mfa-self-contained -PythonArchive .local/release-v0.8.0/maapjsk-python.zip -Output .local/release
python scripts/check_release_package.py .local/release/MaaPJSK-v0.8.0-win-x64
```

正式构建要求干净工作树，`-AllowDirty` 只用于本地探测，不能上传该资产。构建记录仅含版本、提交及哈希，不记录私有绝对路径。模板仅复制四份本机模板 JSON 显式引用的小裁剪，模型使用固定已校验 SHA256；构建器拒绝不符内容。固定 MFA 导出在项目 `.local` 内应用 overlay，不修改邻仓。

自动回归、包内容检查、首次启动／改名测试、更新保留／回退测试与真实演出验收分别报告；打包不扩大任何演出、异常恢复或广告验收范围。
