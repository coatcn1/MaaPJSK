using MFAAvalonia.ViewModels.UsersControls.Settings;
using MFAAvalonia.Extensions.MaaFW;
using System;
using System.Collections.Generic;
using System.Threading;
using System.Threading.Tasks;
using Avalonia.Threading;

namespace MFAAvalonia.Helper;

public static class MaaPjskFirstLiveSync
{
    private static readonly CancellationTokenSource ShutdownCancellation = new();
    private static bool _completed;
    public static CancellationToken ShutdownToken => ShutdownCancellation.Token;
    public static bool IsPrepared { get { lock (MaaPjskMaintenance.Sync) return _completed; } }
    public static ChartCatalogSettingsUserControlModel Model { get; } = new();
    public static bool NeedsSync(IEnumerable<string?> entries)
    {
        lock (MaaPjskMaintenance.Sync)
            return !_completed && System.Linq.Enumerable.Any(entries, IsLiveEntry);
    }
    public static bool IsLiveEntry(string? entry) => entry is "AutoLive" or "SoloChartLive"
        or "CooperativeChartLive" or "OneShotChartLive" or "SoloChartCalibration";
    public static async Task PrepareAsync(CancellationToken token, MaaProcessor? owner = null)
    {
        // 子进程两条输出与绑定属性沿同一 UI 上下文推进，避免并发改写进度队列。
        var synchronization = await Dispatcher.UIThread.InvokeAsync(() => Model.RunAutomaticSyncAsync(token, owner));
        if (!synchronization)
        {
            token.ThrowIfCancellationRequested();
            throw new InvalidOperationException("首次演出前谱面同步未完成，未启动演出；请再次启动重试。");
        }
        token.ThrowIfCancellationRequested();
        lock (MaaPjskMaintenance.Sync) _completed = true;
    }
    public static void Shutdown()
    {
        ShutdownCancellation.Cancel();
        Model.ShutdownProcess();
    }
}
