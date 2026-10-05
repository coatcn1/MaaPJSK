# MFA 本项目扩展

`scripts/build-chart-settings.ps1` 将通用 MFA 固定 `v2.12.0` 导出到项目忽略目录，再应用设置页、任务状态和任务防息屏覆盖。构建与部署均保留上游许可；相邻定制 MFA 的源码和运行目录不参与修改。

## 任务防息屏

性能设置中的「任务运行中阻止息屏」沿用 `PreventSleep` 配置键和关闭默认值。设置页与任务使用同一偏好，全局项缺失时读取已有配置的值；更改开关后保存为全局项，保留旧配置以兼容原设置。

实际任务队列进入 `ExecuteTasks` 后才建立作用域，直到任务动作返回才释放。因此最后一个任务已出队但仍在执行时，保护仍然有效。多个实例的作用域互不干扰；任务完成、失败、取消、应用退出均清理，空队列、已取消的启动和退出后的迟到回调不建立请求。

Windows 的 `SetThreadExecutionState` 始终在 Avalonia UI 线程调用，运行期间申请 `ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_DISPLAY_REQUIRED`，结束时解除请求。检查返回值，申请失败不报告启用；不修改系统电源计划。接口语义见 [Microsoft 文档](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setthreadexecutionstate)。

任务作用域方案参考 MaaBanGDream 定制 MFA 的 [a39dcd87](https://github.com/coatcn1/MFAAvalonia/commit/a39dcd87ba2e5098ee23072e9a015c5c36f8c8d1)，同属 MFAAvalonia 的 GPL-3.0 代码。本项目另外将保护接入实际执行循环，并在 UI 线程建作用域前复查取消状态。

## 验证

`tests/MfaSleepSmoke` 在离屏平台执行真实 `MaaProcessor.ExecuteTasks`，以合成任务动作检查实际 Windows 执行状态，不连接模拟器或派发游戏输入。测试覆盖保存设置兼容、最后任务出队、完成／失败／停止、多个实例、运行时开关和退出回调。

```powershell
dotnet run --project tests/MfaSleepSmoke/MfaSleepSmoke.csproj -c Release -r win-x64
```

该检查证明任务与系统唤醒请求的生命周期；它不替代长时间游戏演奏验收。
