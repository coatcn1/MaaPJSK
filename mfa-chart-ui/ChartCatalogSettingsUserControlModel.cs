using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Avalonia.Controls.Notifications;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using SukiUI.Dialogs;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.ViewModels.UsersControls.Settings;

public static class ChartCatalogMaintenance
{
    // 所有实例共用一个本地谱面库；维护时阻止新任务进入，停止任务仍可照常使用。
    public static bool IsBusy => MaaPjskMaintenance.IsBusy;
    internal static bool TryBegin() => MaaPjskMaintenance.TryBegin();
    internal static void End() => MaaPjskMaintenance.End();
}

public sealed partial class ChartCatalogSettingsUserControlModel : ViewModelBase
{
    private readonly Queue<string> _progressLines = new();
    private Process? _process;
    private CancellationTokenSource? _cancellation;
    private bool _cancelRequested;
    private string? _resultToken;
    private JObject? _runResult;

    [ObservableProperty] private string _catalogText = "本地尚未同步日服谱面";
    [ObservableProperty] private string _statusText = "停止演出任务后，可在这里增量更新谱面。";
    [ObservableProperty] private string _progressText = string.Empty;
    [ObservableProperty] [NotifyPropertyChangedFor(nameof(NotSyncing))] private bool _isSyncing;
    public bool NotSyncing => !IsSyncing;

    private static async Task<JObject> LoadConfigAsync()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "config", "chart-sync.json");
        if (!File.Exists(path))
            throw new InvalidDataException("缺少谱面同步配置，请重新运行 MaaPJSK 启动脚本部署。");
        return JObject.Parse(await File.ReadAllTextAsync(path));
    }

    private static string Required(JObject config, string name) =>
        config.Value<string>(name) is { Length: > 0 } value
            ? value : throw new InvalidDataException($"谱面同步配置缺少 {name}");

    [RelayCommand]
    private async Task RefreshAsync()
    {
        try
        {
            var config = await LoadConfigAsync();
            var manifestPath = Required(config, "manifest_path");
            if (!File.Exists(manifestPath))
            {
                CatalogText = "本地尚未同步日服谱面";
                return;
            }
            var manifest = JObject.Parse(await File.ReadAllTextAsync(manifestPath));
            var summary = manifest["summary"] as JObject
                          ?? throw new InvalidDataException("谱面库缺少统计信息");
            var errors = (summary.Value<int?>("recoverable_errors") ?? 0) + (summary.Value<int?>("fatal_errors") ?? 0);
            var generatedAt = manifest.Value<string>("generated_at") ?? "未知时间";
            if (DateTimeOffset.TryParse(generatedAt, out var timestamp))
                generatedAt = timestamp.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss");
            CatalogText = $"{summary.Value<int?>("songs_with_charts") ?? 0} 首 / "
                          + $"{summary.Value<int?>("charts") ?? 0} 张谱面 / {summary.Value<int?>("jackets") ?? 0} 个封面 / "
                          + $"{errors} 个错误 / {summary.Value<int?>("stale") ?? 0} 个资源未确认最新 · 更新于 {generatedAt}";
            if (summary.ContainsKey("reused"))
                CatalogText += Environment.NewLine + $"上次完整索引：新增 {summary.Value<int?>("added") ?? 0} / "
                               + $"更新 {summary.Value<int?>("updated") ?? 0} / 本地复用 {summary.Value<int?>("reused") ?? 0} / "
                               + $"联网未变 {summary.Value<int?>("unchanged") ?? 0} / "
                               + $"下载 {(summary.Value<long?>("downloaded_bytes") ?? 0) / (1024d * 1024d):F2} MiB";
        }
        catch (Exception error)
        {
            CatalogText = $"读取本地谱面状态失败：{error.Message}";
        }
    }

    private void AppendProgress(string line)
    {
        if (string.IsNullOrWhiteSpace(line))
            return;
        _progressLines.Enqueue(line.Length <= 1000 ? line : line[..1000]);
        while (_progressLines.Count > 120)
            _progressLines.Dequeue();
        ProgressText = string.Join(Environment.NewLine, _progressLines);
    }

    private async Task PumpAsync(StreamReader stream, CancellationToken token)
    {
        while (await stream.ReadLineAsync(token) is { } line)
        {
            if (line.StartsWith("MAAPJSK_SYNC_RESULT=", StringComparison.Ordinal))
            {
                try
                {
                    var result = JObject.Parse(line["MAAPJSK_SYNC_RESULT=".Length..]);
                    if (result.Value<string>("token") == _resultToken)
                        _runResult = result;
                }
                catch (Exception) { }
            }
            else
                AppendProgress(line);
        }
    }

    [RelayCommand]
    private Task SyncAsync() => RunSyncAsync(checkRecent: false);

    [RelayCommand]
    private Task CheckRecentAsync() => RunSyncAsync(checkRecent: true);

    private async Task RunSyncAsync(bool checkRecent)
    {
        if (Instances.InstanceTabBarViewModel.Tabs.Any(tab => tab.IsRunning))
        {
            StatusText = "请先停止所有演出任务，再同步谱面。";
            return;
        }
        if (!ChartCatalogMaintenance.TryBegin())
        {
            StatusText = "另一个谱面同步操作正在进行。";
            return;
        }
        IsSyncing = true;
        _cancelRequested = false;
        _progressLines.Clear();
        ProgressText = string.Empty;
        StatusText = checkRecent ? "正在联网检查近三个月的资源…" : "正在增量更新日服谱面…";
        _resultToken = Guid.NewGuid().ToString("N");
        _runResult = null;
        var completionType = NotificationType.Error;
        var completionTitle = "谱面同步失败";
        string? completionStatistics = null;
        var completionStatus = string.Empty;
        try
        {
            var config = await LoadConfigAsync();
            var startInfo = new ProcessStartInfo(Required(config, "child_exec"))
            {
                UseShellExecute = false, CreateNoWindow = true,
                RedirectStandardOutput = true, RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
                WorkingDirectory = Required(config, "working_directory")
            };
            startInfo.ArgumentList.Add(Required(config, "script_path"));
            startInfo.ArgumentList.Add("--output-root");
            startInfo.ArgumentList.Add(Required(config, "output_root"));
            startInfo.ArgumentList.Add("--result-token");
            startInfo.ArgumentList.Add(_resultToken);
            if (checkRecent)
                startInfo.ArgumentList.Add("--check-recent");
            startInfo.Environment["PYTHONUTF8"] = "1";
            startInfo.Environment["PYTHONIOENCODING"] = "utf-8";
            _cancellation = new CancellationTokenSource(TimeSpan.FromMinutes(45));
            _process = Process.Start(startInfo) ?? throw new InvalidOperationException("无法启动谱面同步器");
            // 同时读取两条流，避免大量错误堵塞同步器；进度保持在 UI 线程更新。
            await Task.WhenAll(PumpAsync(_process.StandardOutput, _cancellation.Token),
                               PumpAsync(_process.StandardError, _cancellation.Token));
            await _process.WaitForExitAsync(_cancellation.Token);
            var exitCode = _process.ExitCode;
            StatusText = exitCode switch
            {
                0 => checkRecent ? "近期资源检查完成。" : "增量更新完成。",
                2 => "同步已结束；部分资源更新失败，可再次同步重试，错误详情见下方。",
                130 => "已取消；下次同步会复用已校验的下载。",
                _ => $"同步失败（退出码 {_process.ExitCode}），原有谱面库可继续使用；详情见下方。"
            };
            if (exitCode is 0 or 2 && _runResult?.Value<int?>("exit_code") == exitCode
                && _runResult["summary"] is JObject summary)
            {
                completionType = exitCode == 0 ? NotificationType.Success : NotificationType.Warning;
                completionTitle = exitCode == 0 ? "谱面同步完成" : "谱面同步部分失败";
                completionStatistics = $"本次资源（谱面与封面）：新增 {summary.Value<int?>("added") ?? 0} / "
                    + $"更新 {summary.Value<int?>("updated") ?? 0} / 本地复用 {summary.Value<int?>("reused") ?? 0} / "
                    + $"联网未变 {summary.Value<int?>("unchanged") ?? 0} / "
                    + $"失败 {summary.Value<int?>("recoverable_errors") ?? 0} / "
                    + $"未确认最新 {summary.Value<int?>("stale") ?? 0} / "
                    + $"下载 {(summary.Value<long?>("downloaded_bytes") ?? 0) / (1024d * 1024d):F2} MiB";
            }
            else if (exitCode is 0 or 2)
                StatusText = "同步器未返回可核对的本次统计，无法确认本次更新结果；详情见下方。";
            else if (exitCode == 130)
            {
                completionType = NotificationType.Information;
                completionTitle = "谱面同步已取消";
            }
        }
        catch (OperationCanceledException)
        {
            StopProcess();
            StatusText = _cancelRequested ? "已取消；下次同步会复用已校验的下载。" : "同步超过 45 分钟，已停止；可再次同步续传。";
            AppendProgress(StatusText);
            completionType = _cancelRequested ? NotificationType.Information : NotificationType.Error;
            completionTitle = _cancelRequested ? "谱面同步已取消" : "谱面同步超时";
        }
        catch (Exception error)
        {
            StopProcess();
            StatusText = $"谱面同步失败：{error.Message}";
            AppendProgress(StatusText);
        }
        finally
        {
            completionStatus = StatusText;
            // 先确认同步进程退出，再解除任务启动保护；强制取消不会替换完整 manifest。
            StopProcess();
            if (_process is not null)
                await _process.WaitForExitAsync();
            _process?.Dispose();
            _process = null;
            _cancellation?.Dispose();
            _cancellation = null;
            ChartCatalogMaintenance.End();
            IsSyncing = false;
            await RefreshAsync();
        }
        // 完成通知属于本次进程结果；先清理进程、解锁并刷新，不用旧索引冒充本次成功。
        var manager = Instances.DialogManager;
        var completion = manager.CreateDialog().OfType(completionType).WithTitle(completionTitle)
            .WithContent(completionStatus + Environment.NewLine
                + (completionStatistics ?? "本次新增、更新、复用及失败数量未取得；上次完整索引统计见页面。"))
            .WithActionButton("确定", _ => { }, true, "Basic");
        // 已有弹窗不能吞掉完成结果；等其关闭后显示同一个完成弹窗，不重复创建或打断现有提示。
        while (true)
        {
            var dismissed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            SukiDialogManagerEventHandler onDismissed = (_, _) => dismissed.TrySetResult();
            manager.OnDialogDismissed += onDismissed;
            try
            {
                if (completion.TryShow())
                    break;
                await dismissed.Task;
            }
            finally
            {
                manager.OnDialogDismissed -= onDismissed;
            }
        }
    }

    private void StopProcess()
    {
        if (_process is not null && !_process.HasExited)
            _process.Kill(entireProcessTree: true);
    }

    [RelayCommand]
    private void Cancel()
    {
        _cancelRequested = true;
        _cancellation?.Cancel();
    }
}
