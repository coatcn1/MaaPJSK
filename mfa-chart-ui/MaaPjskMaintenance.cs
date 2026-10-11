using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.ViewModels.Windows;
using MFAAvalonia.ViewModels.Other;
using System;
using System.Linq;
using System.Threading;

namespace MFAAvalonia.Helper;

public static class MaaPjskMaintenance
{
    public static readonly object Sync = new();
    private static bool _maintenance;
    private static int _executing;
    public static bool HasExecuting { get { lock (Sync) return _executing != 0; } }
    public static bool IsBusy { get { lock (Sync) return _maintenance; } }
    public static IDisposable EnterTaskStart()
    {
        Monitor.Enter(Sync);
        return new Scope(() => Monitor.Exit(Sync));
    }
    public static bool TryBegin()
    {
        lock (Sync)
        {
            if (_maintenance || _executing != 0
                || (Instances.IsResolved<RootViewModel>() && Instances.RootViewModel.IsRunning)
                || (Instances.IsResolved<InstanceTabBarViewModel>() && Instances.InstanceTabBarViewModel.Tabs.Any(tab => tab.IsRunning))
                || MaaProcessor.Processors.Any(p => p.TaskQueue.Count > 0 || p.IsConnecting
                    || p.MaaTasker?.IsRunning == true || p.MaaTasker?.IsStopping == true)) return false;
            _maintenance = true;
            return true;
        }
    }
    public static void End() { lock (Sync) _maintenance = false; }
    public static IDisposable BeginExecution()
    {
        lock (Sync)
        {
            if (_maintenance) throw new InvalidOperationException("维护正在进行，不能开始任务。");
            _executing++;
        }
        return new Scope(() => { lock (Sync) _executing--; });
    }
    private sealed class Scope(Action release) : IDisposable
    {
        private Action? _release = release;
        public void Dispose() => Interlocked.Exchange(ref _release, null)?.Invoke();
    }
}
