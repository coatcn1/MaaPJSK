using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Threading;
using MFAAvalonia.Configuration;

namespace MFAAvalonia.Helper;

// 任务作用域参考定制 MFA 的 a39dcd87；本项目通过固定上游导出覆盖部署，来源见本目录说明。
public static class SystemSleepHelper
{
    [Flags]
    private enum ExecutionState : uint
    {
        Continuous = 0x80000000,
        DisplayRequired = 0x00000002,
        SystemRequired = 0x00000001
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern ExecutionState SetThreadExecutionState(ExecutionState flags);

    private static readonly HashSet<TaskExecutionScope> ActiveTasks = new();
    private static bool _shuttingDown;
    public static bool IsPreventingSleep { get; private set; }

    public static bool GetPreventSleepSetting()
    {
        // 新旧界面读取同一偏好；已有配置的勾选值在全局项缺失时仍然生效。
        var global = GlobalConfiguration.GetValue(ConfigurationKeys.PreventSleep);
        return bool.TryParse(global, out var enabled)
            ? enabled
            : ConfigurationManager.Current.GetValue(ConfigurationKeys.PreventSleep, false);
    }

    public static void SavePreventSleepSetting(bool enabled)
    {
        GlobalConfiguration.SetValue(ConfigurationKeys.PreventSleep, enabled ? "true" : "false");
        ApplyPreventSleep();
    }

    public static async Task<IDisposable> BeginTaskExecutionAsync(CancellationToken token = default)
    {
        var scope = new TaskExecutionScope();
        if (!OperatingSystem.IsWindows()) return scope;
        // 请求必须先在固定线程建立；空队列、已取消任务和退出后的迟到回调不能建立保护。
        await DispatcherHelper.RunOnMainThreadAsync(() =>
        {
            if (_shuttingDown || token.IsCancellationRequested) return;
            ActiveTasks.Add(scope);
            ApplyPreventSleep();
        });
        return scope;
    }

    private static void EndTaskExecution(TaskExecutionScope scope)
    {
        if (!OperatingSystem.IsWindows()) return;
        if (!Dispatcher.UIThread.CheckAccess())
        {
            Dispatcher.UIThread.Post(() => EndTaskExecution(scope));
            return;
        }
        // 多个实例各持有自己的作用域，结束一项不能解除其他执行中任务的保护。
        if (ActiveTasks.Remove(scope)) ApplyPreventSleep();
    }

    public static void ApplyPreventSleep()
    {
        if (!OperatingSystem.IsWindows()) return;
        if (!Dispatcher.UIThread.CheckAccess())
        {
            // 执行时重新读取最新状态，避免跨线程排队的旧开关值重新申请保护。
            Dispatcher.UIThread.Post(ApplyPreventSleep);
            return;
        }
        try
        {
            SetPreventSleepState(!_shuttingDown && ActiveTasks.Count > 0 && GetPreventSleepSetting());
        }
        catch (Exception error)
        {
            LoggerHelper.Error("应用任务防息屏设置失败。", error);
        }
    }

    public static void Shutdown()
    {
        if (!OperatingSystem.IsWindows()) return;
        if (!Dispatcher.UIThread.CheckAccess())
        {
            Dispatcher.UIThread.Post(Shutdown);
            return;
        }
        _shuttingDown = true;
        ActiveTasks.Clear();
        SetPreventSleepState(false);
    }

    private static void SetPreventSleepState(bool enabled)
    {
        // Windows 请求属于调用线程，建立与解除都只在 UI 线程进行。
        if (enabled == IsPreventingSleep) return;
        try
        {
            var flags = ExecutionState.Continuous;
            if (enabled) flags |= ExecutionState.SystemRequired | ExecutionState.DisplayRequired;
            if (SetThreadExecutionState(flags) == 0)
                throw new Win32Exception(Marshal.GetLastWin32Error());
            IsPreventingSleep = enabled;
            LoggerHelper.Info(enabled ? "已启用任务运行中防息屏和防休眠。" : "已释放任务防息屏和防休眠请求。");
        }
        catch (Exception error)
        {
            // 请求失败不能伪报已经启用，也不修改用户保存的开关偏好。
            LoggerHelper.Error("设置 Windows 任务执行状态失败。", error);
        }
    }

    private sealed class TaskExecutionScope : IDisposable
    {
        private int _disposed;
        public void Dispose()
        {
            // 完成、取消、异常及重复清理都只解除一次。
            if (Interlocked.Exchange(ref _disposed, 1) == 0) EndTaskExecution(this);
        }
    }
}
