using Avalonia.Threading;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using Newtonsoft.Json.Linq;
using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

public static class MaaPjskReleaseUpdate
{
    private static int _busy;
    private static int _executing;
    public static bool IsBusy => Volatile.Read(ref _busy) != 0;
    public static bool IsPortable => File.Exists(Path.Combine(AppContext.BaseDirectory, "package-manifest.json"));

    // 队列最后一项已经出队，实际任务／清理仍可能执行；保持整个执行作用域。
    public static IDisposable BeginExecution()
    {
        Interlocked.Increment(ref _executing);
        return new ExecutionScope();
    }
    private sealed class ExecutionScope : IDisposable
    {
        private int _disposed;
        public void Dispose()
        {
            if (Interlocked.Exchange(ref _disposed, 1) == 0)
                Interlocked.Decrement(ref _executing);
        }
    }

    public static bool IsIdle => Volatile.Read(ref _executing) == 0
        && !ChartCatalogMaintenance.IsBusy
        && !Instances.RootViewModel.IsRunning
        && !MaaProcessor.Processors.Any(p => p.TaskQueue.Count > 0 || p.IsConnecting
            || p.MaaTasker?.IsRunning == true || p.MaaTasker?.IsStopping == true);

    private static string Python => Path.Combine(AppContext.BaseDirectory, "runtime", "python", "python.exe");

    private static async Task<JObject> RunAsync(params string[] arguments)
    {
        var marker = JObject.Parse(await File.ReadAllTextAsync(Path.Combine(AppContext.BaseDirectory, "package-manifest.json")));
        if (marker.Value<string>("package") != "MaaPJSK" || marker.Value<string>("architecture") != "win-x64")
            throw new InvalidDataException("发行包身份无效，请使用 MaaPJSK 完整包。");
        var start = new ProcessStartInfo(Python) {
            WorkingDirectory = AppContext.BaseDirectory,
            UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true
        };
        start.ArgumentList.Add("-X"); start.ArgumentList.Add("utf8");
        start.ArgumentList.Add(Path.Combine(AppContext.BaseDirectory, "scripts", "update-release.py"));
        foreach (var argument in arguments) start.ArgumentList.Add(argument);
        using var process = Process.Start(start) ?? throw new IOException("无法启动发行更新检查。");
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        if (process.ExitCode != 0) throw new IOException(await error);
        return JObject.Parse(await output);
    }

    public static async Task CheckAsync()
    {
        try {
            var release = await RunAsync("check");
            var current = JObject.Parse(await File.ReadAllTextAsync(Path.Combine(AppContext.BaseDirectory, "package-manifest.json")));
            var latest = release.Value<string>("version") ?? "";
            var installed = current.Value<string>("version") ?? "";
            if (Version.Parse(latest) > Version.Parse(installed))
                ToastHelper.Info("MaaPJSK 更新", $"发现 v{latest}，请停止任务后使用资源更新按钮更新整个发行包。");
            else ToastHelper.Info("MaaPJSK 更新", $"当前 v{installed} 已是最新稳定版本。");
        } catch (Exception error) {
            ToastHelper.Error("MaaPJSK 更新检查失败", error.Message);
        }
    }

    public static async Task UpdateAsync(string? localPackagePath = null)
    {
        if (Interlocked.CompareExchange(ref _busy, 1, 0) != 0) return;
        var handedOff = false;
        try {
            if (!await Dispatcher.UIThread.InvokeAsync(() => IsIdle))
                throw new InvalidOperationException("任务或谱面同步仍在执行，请停止并等待释放完成后再更新。");
            var args = new[] { "prepare", "--root", AppContext.BaseDirectory };
            if (!string.IsNullOrWhiteSpace(localPackagePath))
                args = args.Concat(new[] { "--archive", localPackagePath, "--sidecar", localPackagePath + ".sha256" }).ToArray();
            ToastHelper.Info("MaaPJSK 更新", "正在校验完整发行更新包，用户配置与谱面库将保留。");
            var prepared = await RunAsync(args);
            var plan = prepared.Value<string>("plan") ?? throw new InvalidDataException("更新计划缺失。");
            // 下载期间阻止新任务；退出前再次确认，不能为更新强停正在演出的任务。
            await Dispatcher.UIThread.InvokeAsync(() => {
                if (!IsIdle) throw new InvalidOperationException("仍有任务执行，已取消更新退出。");
                var start = new ProcessStartInfo("powershell.exe") {
                    UseShellExecute = false, CreateNoWindow = true,
                    WorkingDirectory = Path.GetDirectoryName(plan)!
                };
                foreach (var argument in new[] { "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    Path.Combine(Path.GetDirectoryName(plan)!, "apply-release-update.ps1"),
                    "-Plan", plan, "-ParentId", Environment.ProcessId.ToString() }) start.ArgumentList.Add(argument);
                _ = Process.Start(start) ?? throw new IOException("无法启动退出后的更新助手。");
                handedOff = true;
                Instances.ShutdownApplication();
            });
        } catch (Exception error) {
            ToastHelper.Error("MaaPJSK 更新失败", error.Message);
        } finally { if (!handedOff) Interlocked.Exchange(ref _busy, 0); }
    }
}
