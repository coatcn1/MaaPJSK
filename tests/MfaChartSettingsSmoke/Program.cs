using Avalonia;
using Avalonia.Controls;
using Avalonia.Controls.Notifications;
using Avalonia.Interactivity;
using Avalonia.Headless;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MaaFramework.Binding;
using MFAAvalonia;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.Helper;
using MFAAvalonia.ViewModels.Other;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using MFAAvalonia.Views.UserControls.Settings;
using Microsoft.Extensions.DependencyInjection;
using Newtonsoft.Json.Linq;
using SukiUI;
using SukiUI.Controls;
using SukiUI.Dialogs;
using SukiUI.Toasts;
using System.Collections.ObjectModel;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;

public sealed class SmokeApplication : Application
{
    public override void Initialize()
    {
        Styles.Add(new FluentTheme());
        Styles.Add(new SukiTheme());
    }
}

internal static partial class Program
{
    private static int _checks;
    [DllImport("MaaFramework", CallingConvention = CallingConvention.Cdecl)]
    private static extern nint MaaTaskerGetResource(nint handle);

    private static int LoggingProbe(string mode, string directory)
    {
        Directory.CreateDirectory(directory);
        Environment.CurrentDirectory = directory;
        bool disabled = mode == "disabled";
        typeof(MaaPjskDiagnostics).GetField("_disabled", BindingFlags.Static | BindingFlags.NonPublic)!.SetValue(null, disabled);
        typeof(AppPaths).GetField("_logsDirectory", BindingFlags.Static | BindingFlags.NonPublic)!.SetValue(null, Path.Combine(directory, "logs"));
        typeof(AppPaths).GetField("_initialized", BindingFlags.Static | BindingFlags.NonPublic)!.SetValue(null, true);
        // 初次静态初始化先设置全局日志，再初始化 Toolkit；不连接或控制设备。
        var global = MaaProcessor.Global;
        _ = MaaProcessor.Toolkit;
        if (!disabled) global.SetOption_LogDir(Path.Combine(directory, "debug"));
        LoggerHelper.Info("startup buffered fixture");
        LoggerHelper.InitializeLogger();
        LoggerHelper.Error("startup error fixture");
        MaaTaskerGetResource(0);
        LoggerHelper.DisposeLogger();
        var files = Directory.GetFiles(directory, "*", SearchOption.AllDirectories);
        Require(disabled ? files.Length == 0 : files.Any(path => path.EndsWith("maafw.log")) && files.Any(path => path.EndsWith(".log")),
            "禁日志启动须零文件，启用对照须生成实际框架和 GUI 日志。");
        Console.WriteLine($"GUI startup logging probe {mode}: {files.Length} files");
        return 0;
    }

    private static void Require(bool condition, string message)
    {
        if (!condition)
            throw new InvalidOperationException(message);
        _checks++;
    }

    private static void Wait(Task operation, int seconds = 10)
    {
        var deadline = DateTime.UtcNow.AddSeconds(seconds);
        while (!operation.IsCompleted && DateTime.UtcNow < deadline)
        {
            Dispatcher.UIThread.RunJobs();
            Thread.Sleep(10);
        }
        if (!operation.IsCompleted)
            throw new TimeoutException("离屏设置页测试超时");
        operation.GetAwaiter().GetResult();
        Dispatcher.UIThread.RunJobs();
    }

    private static void CheckFailedPerformanceTask()
    {
        // 本局日志已确认框架返回 Failed；检验这一状态穿过实际队列包装后不会被吞掉。
        string[] entries = ["AutoLive", "SoloChartLive", "CooperativeChartLive", "OneShotChartLive", "SoloChartCalibration", "AdRewards"];
        foreach (var entry in entries)
        {
            var queued = new MFATask { Name = entry, Type = MFATask.MFATaskType.MAAFW,
                Action = () => { MaaPjskTaskStatus.Check(entry, MaaJobStatus.Failed, true); return Task.CompletedTask; } };
            var result = queued.Run(CancellationToken.None);
            Wait(result);
            Require(result.Result == MFATask.MFATaskStatus.FAILED,
                $"{entry} 已失败时，开启出错继续也不能把队列任务包装成完成");
        }
        var succeeded = new MFATask { Name = "CooperativeChartLive", Type = MFATask.MFATaskType.MAAFW,
            Action = () => { MaaPjskTaskStatus.Check("CooperativeChartLive", MaaJobStatus.Succeeded, true); return Task.CompletedTask; } };
        var successResult = succeeded.Run(CancellationToken.None);
        Wait(successResult);
        Require(successResult.Result == MFATask.MFATaskStatus.SUCCEEDED, "完整成功的演出任务仍须传递成功状态");
        var cancelled = new MFATask { Type = MFATask.MFATaskType.MAAFW,
            Action = () => Task.FromCanceled(new CancellationToken(true)) };
        var cancelResult = cancelled.Run(CancellationToken.None);
        Wait(cancelResult);
        Require(cancelResult.Result == MFATask.MFATaskStatus.STOPPED, "用户取消必须继续显示停止");
        var continued = new MFATask { Action = () => {
            MaaPjskTaskStatus.Check("OtherResourceTask", MaaJobStatus.Failed, true); return Task.CompletedTask; } };
        var continuedResult = continued.Run(CancellationToken.None);
        Wait(continuedResult);
        Require(continuedResult.Result == MFATask.MFATaskStatus.SUCCEEDED, "其他资源任务保留原来的出错继续策略");
        var other = new MFATask { Action = () => {
            MaaPjskTaskStatus.Check("OtherResourceTask", MaaJobStatus.Failed, false); return Task.CompletedTask; } };
        var otherResult = other.Run(CancellationToken.None);
        Wait(otherResult);
        Require(otherResult.Result == MFATask.MFATaskStatus.FAILED, "关闭出错继续时仍须传递失败");
    }

    public static int Main(string[] args)
    {
        if (args.Length == 1 && args[0] == "--first-sync-state")
            return MaaPjskFirstLiveSync.NeedsSync(["SoloChartLive"]) ? 0 : 1;
        if (args.Length == 3 && args[0] == "--logging-probe") return LoggingProbe(args[1], Path.GetFullPath(args[2]));
        var firstSyncProbe = args.Length == 4 && args[0] == "--first-sync-probe";
        if (args.Length != 2 && !firstSyncProbe)
            throw new ArgumentException("参数：python.exe 和离屏截图输出路径");
        AppBuilder.Configure<SmokeApplication>().UseSkia()
            .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false })
            .SetupWithoutStarting();

        // 队列末项已经出队时，更新仍必须等待真实执行作用域结束；重叠作用域独立释放。
        var firstExecution = MaaPjskReleaseUpdate.BeginExecution();
        var secondExecution = MaaPjskReleaseUpdate.BeginExecution();
        Require(!MaaPjskReleaseUpdate.IsIdle, "实际任务尚在执行时不能更新");
        firstExecution.Dispose();
        firstExecution.Dispose();
        Require(!MaaPjskReleaseUpdate.IsIdle, "重复释放一个作用域不能释放另一任务的更新门槛");
        secondExecution.Dispose();

        // 测试中的实例集合不创建 Maa 控制器，所有窗口和输入都在离屏平台的内存里。
        var tabs = new ObservableCollection<InstanceTabViewModel>();
        var tabBar = (InstanceTabBarViewModel)RuntimeHelpers.GetUninitializedObject(typeof(InstanceTabBarViewModel));
        typeof(InstanceTabBarViewModel).GetField("<Tabs>k__BackingField", BindingFlags.Instance | BindingFlags.NonPublic)!
            .SetValue(tabBar, tabs);
        var smokeRoot = (MFAAvalonia.ViewModels.Windows.RootViewModel)RuntimeHelpers.GetUninitializedObject(typeof(MFAAvalonia.ViewModels.Windows.RootViewModel));
        typeof(MFAAvalonia.ViewModels.Windows.RootViewModel).GetField("_idle", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(smokeRoot, true);
        typeof(App).GetProperty(nameof(App.Services))!.SetValue(null,
            new ServiceCollection().AddSingleton(tabBar).AddSingleton(smokeRoot).AddSingleton<ISukiToastManager, SukiToastManager>()
                .AddSingleton<ISukiDialogManager, SukiDialogManager>().BuildServiceProvider());
        if (firstSyncProbe) return FirstSyncProbe(args[1], args[2], args[3]);

        var reservation = (MaaProcessor)RuntimeHelpers.GetUninitializedObject(typeof(MaaProcessor));
        var reserve = typeof(MaaProcessor).GetMethod("ReserveProjectExecution", BindingFlags.Instance | BindingFlags.NonPublic)!;
        var release = typeof(MaaProcessor).GetMethod("ReleaseProjectExecution", BindingFlags.Instance | BindingFlags.NonPublic)!;
        Require((bool)reserve.Invoke(reservation, null)!, "尚未排入 worker 的启动请求也须预留任务生命周期");
        Require(!MaaPjskMaintenance.TryBegin(), "pending 启动窗口不能开始缓存或谱面维护");
        release.Invoke(reservation, null);
        Require(MaaPjskMaintenance.TryBegin(), "完整释放后才能维护");
        try { Require(!(bool)reserve.Invoke(reservation, null)!, "维护期间 worker 启动须拒绝且不初始化任务"); }
        finally { MaaPjskMaintenance.End(); }

        var cacheFixture = Path.Combine(AppContext.BaseDirectory, "cache-fixture-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(cacheFixture, "debug", "runs"));
        Directory.CreateDirectory(Path.Combine(cacheFixture, "logs"));
        Directory.CreateDirectory(Path.Combine(cacheFixture, "config"));
        File.WriteAllText(Path.Combine(cacheFixture, "debug", "runs", "image.png"), "fixture image");
        File.WriteAllText(Path.Combine(cacheFixture, "debug", "results.csv"), "preserved score");
        File.WriteAllText(Path.Combine(cacheFixture, "logs", "runtime.log"), "fixture log");
        File.WriteAllText(Path.Combine(cacheFixture, "config", "profile.json"), "preserved config");
        File.WriteAllText(Path.Combine(cacheFixture, "config", "failure-timeout.png"), "old failure");
        var cacheResult = MaaPjskCacheMaintenance.Clear(cacheFixture);
        Require(cacheResult.Files == 3 && cacheResult.Bytes > 0 && cacheResult.Errors.Count == 0,
            "清理须返回实际文件数与空间");
        Require(File.Exists(Path.Combine(cacheFixture, "debug", "results.csv"))
                && File.Exists(Path.Combine(cacheFixture, "config", "profile.json")), "成绩 CSV 和配置须保留");
        Directory.CreateDirectory(Path.Combine(cacheFixture, "logs"));
        var lockedPath = Path.Combine(cacheFixture, "logs", "locked.log");
        using (var locked = File.Open(lockedPath, FileMode.Create, FileAccess.ReadWrite, FileShare.None))
        {
            var partial = MaaPjskCacheMaintenance.Clear(cacheFixture);
            Require(partial.Errors.Count > 0 && File.Exists(lockedPath), "占用文件须报告部分失败并保留");
        }
        var outside = cacheFixture + "-outside";
        Directory.CreateDirectory(outside);
        File.WriteAllText(Path.Combine(outside, "protected.txt"), "outside fixture");
        var link = Path.Combine(cacheFixture, "logs", "escape");
        using (var junction = System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo("cmd.exe")
            { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true,
                ArgumentList = { "/c", "mklink", "/J", link, outside } })!)
        {
            junction.WaitForExit();
            Require(junction.ExitCode == 0, "重解析点 fixture 必须成功创建");
        }
        var unsafeResult = MaaPjskCacheMaintenance.Clear(cacheFixture);
        Require(unsafeResult.Errors.Count > 0 && File.Exists(Path.Combine(outside, "protected.txt")), "重解析点须拒绝且不得越界删除");
        Directory.Delete(link);
        Directory.Delete(outside, true);
        File.WriteAllText(Path.Combine(cacheFixture, "config", "failure-timeout.png"), "button failure fixture");
        var rootField = typeof(Instances).GetField("_rootViewModel", BindingFlags.Static | BindingFlags.NonPublic)!;
        var previousRoot = rootField.GetValue(null);
        var fakeRoot = (MFAAvalonia.ViewModels.Windows.RootViewModel)RuntimeHelpers.GetUninitializedObject(typeof(MFAAvalonia.ViewModels.Windows.RootViewModel));
        typeof(MFAAvalonia.ViewModels.Windows.RootViewModel).GetField("_idle", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(fakeRoot, true);
        rootField.SetValue(null, fakeRoot);
        var previousDataRoot = AppPaths.DataRoot;
        typeof(AppPaths).GetProperty(nameof(AppPaths.DataRoot))!.SetValue(null, cacheFixture);
        try
        {
            Application.Current!.Resources["MdXamlPlugin"] = new Markdown.Avalonia.MdAvPlugins();
            var about = new AboutUserControl();
            var clear = typeof(AboutUserControl).GetMethod("ClearCache_Click", BindingFlags.Instance | BindingFlags.NonPublic)!;
            Require(MaaPjskMaintenance.TryBegin(), "缓存按钮 fixture 可开始维护");
            try { clear.Invoke(about, [null, new RoutedEventArgs()]); }
            finally { MaaPjskMaintenance.End(); }
            Require(File.Exists(Path.Combine(cacheFixture, "config", "failure-timeout.png")), "维护期间实际缓存按钮必须拒绝");
            clear.Invoke(about, [null, new RoutedEventArgs()]);
            Require(!File.Exists(Path.Combine(cacheFixture, "config", "failure-timeout.png")), "实际关于我们缓存按钮须清理故障 PNG");
            Require(File.Exists(Path.Combine(cacheFixture, "config", "profile.json")), "缓存按钮不能删除配置");
        }
        finally
        {
            typeof(AppPaths).GetProperty(nameof(AppPaths.DataRoot))!.SetValue(null, previousDataRoot);
            rootField.SetValue(null, previousRoot);
        }
        Directory.Delete(cacheFixture, true);

        var fixture = Path.Combine(AppContext.BaseDirectory, "fixture");
        Directory.CreateDirectory(fixture);
        var manifestPath = Path.Combine(fixture, "manifest.json");
        var scriptPath = Path.Combine(fixture, "echo-sync.py");
        var modePath = Path.Combine(fixture, "mode.txt");
        File.WriteAllText(scriptPath, """
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
mode = (root / 'mode.txt').read_text()
(root / 'arguments.json').write_text(json.dumps(sys.argv[1:]), encoding='utf-8')
print('同步测试进度：中文字符与参数传递正常', flush=True)
if mode == 'slow':
    time.sleep(30)
if mode == 'partial':
    print('测试镜像缺失', file=sys.stderr, flush=True)
if mode == 'fatal':
    print('测试致命错误', file=sys.stderr, flush=True)
    sys.exit(1)
if mode == 'cancelled':
    sys.exit(130)
(root / 'manifest.json').write_text(json.dumps({'schema_version':1,'game':'project-sekai','server':'jp','generated_at':'2026-10-03T05:00:00+00:00','summary':{'songs_with_charts':2,'charts':3,'jackets':2,'recoverable_errors':0,'stale':0,'added':1,'updated':0,'reused':4,'unchanged':0,'downloaded_bytes':1048576}}), encoding='utf-8')
token = sys.argv[sys.argv.index('--result-token') + 1]
summary = {'added':7,'updated':3,'reused':41,'unchanged':2,'recoverable_errors':1 if mode == 'partial' else 0,'stale':1 if mode == 'partial' else 0,'downloaded_bytes':2097152}
if mode != 'noresult':
    print('MAAPJSK_SYNC_RESULT=' + json.dumps({'token':'wrong' if mode == 'wrongtoken' else token,'exit_code':2 if mode == 'partial' else 0,'summary':summary}), flush=True)
sys.exit(2 if mode == 'partial' else 0)
""");
        var configDirectory = Path.Combine(AppContext.BaseDirectory, "config");
        Directory.CreateDirectory(configDirectory);
        File.WriteAllText(Path.Combine(configDirectory, "chart-sync.json"), new JObject
        {
            ["child_exec"] = args[0], ["script_path"] = scriptPath, ["working_directory"] = fixture,
            ["output_root"] = fixture, ["manifest_path"] = manifestPath
        }.ToString());
        File.WriteAllText(modePath, "success");
        File.Delete(manifestPath);

        var control = new ChartCatalogSettingsUserControl();
        var manager = Instances.DialogManager;
        var dialogs = new List<ISukiDialog>();
        manager.OnDialogShown += (_, e) =>
        {
            Require(!ChartCatalogMaintenance.IsBusy, "完成弹窗必须在解除维护保护后显示");
            Require(typeof(ChartCatalogSettingsUserControlModel).GetField("_process", BindingFlags.Instance | BindingFlags.NonPublic)!
                .GetValue(control.DataContext) is null, "完成弹窗前子进程必须已退出并清理");
            dialogs.Add(e.Dialog);
        };
        var host = new SukiDialogHost { Manager = manager };
        var grid = new Grid();
        grid.Children.Add(control);
        grid.Children.Add(host);
        var window = new Window { Width = 960, Height = 650, Content = grid, Padding = new Thickness(20) };
        window.Show();
        Dispatcher.UIThread.RunJobs();
        var model = (ChartCatalogSettingsUserControlModel)control.DataContext!;
        var buttons = control.GetVisualDescendants().OfType<Button>().ToArray();
        Require(buttons.Length == 4 && buttons.All(button => button.Command is not null), "增量更新、近期检查、取消和刷新按钮必须真实绑定命令");
        Require(!buttons.Single(button => Equals(button.Content, "取消同步")).IsEnabled, "空闲时取消按钮应禁用");
        void CheckCompletion(string title, NotificationType type, string expected, bool capture = false)
        {
            Require(dialogs.Count == 1, "每次已启动同步只能显示一次完成弹窗");
            var dialog = dialogs.Single();
            Require(dialog.Title == title && dialog.Content?.ToString()?.Contains(expected) == true,
                "弹窗标题及内容必须来自本次运行结果");
            var expectedDialog = manager.CreateDialog().OfType(type).Dialog;
            Require(Equals(dialog.IconColor, expectedDialog.IconColor), "完成弹窗必须使用正确的成功、警告、错误或信息类型");
            AvaloniaHeadlessPlatform.ForceRenderTimerTick();
            Dispatcher.UIThread.RunJobs();
            Require(window.GetVisualDescendants().OfType<TextBlock>().Any(text => text.Text == title),
                "真实Suki弹窗必须显示标题");
            var closeButton = dialog.ActionButtons.OfType<Button>().Single(button => Equals(button.Content, "确定"));
            Require(window.GetVisualDescendants().Contains(closeButton), "确认关闭按钮必须在真实弹窗里渲染");
            Require(closeButton.IsEnabled && closeButton.IsVisible, "确定按钮必须可见且可操作");
            if (capture)
            {
                // 固定 Suki 主题有入场动画，待最终透明度再保存可读的视觉证据。
                var renderDeadline = DateTime.UtcNow.AddMilliseconds(650);
                while (DateTime.UtcNow < renderDeadline)
                {
                    Dispatcher.UIThread.RunJobs();
                    AvaloniaHeadlessPlatform.ForceRenderTimerTick();
                    Thread.Sleep(10);
                }
                using var frame = window.CaptureRenderedFrame()!;
                Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1]))!);
                frame.Save(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[1]))!, "chart-sync-completion.png"));
            }
            var dismissed = false;
            dialog.OnDismissed += _ => dismissed = true;
            closeButton.RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Dispatcher.UIThread.RunJobs();
            Require(dismissed, "确定按钮必须实际关闭弹窗");
            dialogs.Clear();
        }
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText == "增量更新完成。" && model.ProgressText.Contains("中文字符"), "同步子进程必须正确处理退出码和 UTF8 进度");
        Require(!File.ReadAllText(Path.Combine(fixture, "arguments.json")).Contains("--check-recent"), "默认按钮必须执行增量更新");
        Require(model.CatalogText.Contains("2 首 / 3 张谱面 / 2 个封面"), "更新后必须刷新本地数量");
        Require(model.CatalogText.Contains("本地复用 4") && model.CatalogText.Contains("1.00 MiB"), "更新后必须显示复用及实际下载量");
        CheckCompletion("谱面同步完成", NotificationType.Success, "新增 7 / 更新 3 / 本地复用 41", capture: true);
        Wait(model.CheckRecentCommand.ExecuteAsync(null));
        Require(model.StatusText == "近期资源检查完成。"
                && File.ReadAllText(Path.Combine(fixture, "arguments.json")).Contains("--check-recent"),
            "近期检查按钮必须将模式传给同步子进程");
        CheckCompletion("谱面同步完成", NotificationType.Success, "近期资源检查完成。");

        // 完成时若有其他提示，不能吞掉本次弹窗；关闭已有提示后仅补显示一次。
        var existingDialog = manager.CreateDialog().WithTitle("已有提示").WithContent("等待读取")
            .WithActionButton("确定", _ => { }, true);
        existingDialog.TryShow();
        dialogs.Clear();
        var queuedSync = model.SyncCommand.ExecuteAsync(null);
        var queueDeadline = DateTime.UtcNow.AddSeconds(10);
        while ((model.IsSyncing || model.StatusText != "增量更新完成。") && DateTime.UtcNow < queueDeadline)
        {
            Dispatcher.UIThread.RunJobs();
            Thread.Sleep(10);
        }
        Require(!queuedSync.IsCompleted && dialogs.Count == 0 && !ChartCatalogMaintenance.IsBusy,
            "已有提示应保留，完成通知等待关闭且维护保护已释放");
        existingDialog.Dialog.ActionButtons.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
        Wait(queuedSync);
        CheckCompletion("谱面同步完成", NotificationType.Success, "新增 7 / 更新 3 / 本地复用 41");

        var runningTab = (InstanceTabViewModel)RuntimeHelpers.GetUninitializedObject(typeof(InstanceTabViewModel));
        typeof(InstanceTabViewModel).GetField("_isRunning", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(runningTab, true);
        tabs.Add(runningTab);
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("先停止所有演出任务"), "演出任务运行时不应启动同步");
        Wait(model.CheckRecentCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("先停止所有演出任务"), "演出任务运行时不应启动近期检查");
        Require(dialogs.Count == 0, "未启动同步不能显示完成弹窗");
        tabs.Clear();

        File.WriteAllText(modePath, "partial");
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("部分资源更新失败") && model.ProgressText.Contains("测试镜像缺失"), "部分错误须显示详情并保留本地库");
        CheckCompletion("谱面同步部分失败", NotificationType.Warning, "失败 1 / 未确认最新 1");
        File.WriteAllText(modePath, "fatal");
        Wait(model.SyncCommand.ExecuteAsync(null));
        CheckCompletion("谱面同步失败", NotificationType.Error, "本次新增、更新、复用及失败数量未取得");
        foreach (var invalidMode in new[] { "noresult", "wrongtoken" })
        {
            File.WriteAllText(modePath, invalidMode);
            Wait(model.SyncCommand.ExecuteAsync(null));
            Require(!model.StatusText.Contains("增量更新完成"), "缺失或其他运行统计不能冒充本次成功");
            CheckCompletion("谱面同步失败", NotificationType.Error, "未返回可核对的本次统计");
        }
        File.WriteAllText(modePath, "cancelled");
        Wait(model.SyncCommand.ExecuteAsync(null));
        CheckCompletion("谱面同步已取消", NotificationType.Information, "已取消");

        File.WriteAllText(modePath, "slow");
        var previousManifest = File.ReadAllBytes(manifestPath);
        var slowSync = model.SyncCommand.ExecuteAsync(null);
        var progressDeadline = DateTime.UtcNow.AddSeconds(5);
        while (!model.ProgressText.Contains("中文字符") && DateTime.UtcNow < progressDeadline)
        {
            Dispatcher.UIThread.RunJobs();
            Thread.Sleep(10);
        }
        Require(model.IsSyncing && ChartCatalogMaintenance.IsBusy, "同步期间必须设置维护保护");
        Require(!buttons.Single(button => Equals(button.Content, "增量更新谱面")).IsEnabled
                && !buttons.Single(button => Equals(button.Content, "检查近期资源")).IsEnabled,
            "维护期间两个更新入口都必须禁用");
        var taskQueue = (TaskQueueViewModel)RuntimeHelpers.GetUninitializedObject(typeof(TaskQueueViewModel));
        taskQueue.StartTask();
        Require(ChartCatalogMaintenance.IsBusy, "维护保护必须在初始化 Maa 控制器之前拒绝开始任务");
        model.CancelCommand.Execute(null);
        Wait(slowSync);
        Require(model.StatusText.Contains("已取消") && !ChartCatalogMaintenance.IsBusy && model.NotSyncing,
            "取消后必须结束子进程并解除维护保护");
        Require(previousManifest.SequenceEqual(File.ReadAllBytes(manifestPath)), "取消不得替换原有 manifest");
        CheckCompletion("谱面同步已取消", NotificationType.Information, "已取消");
        Require(buttons.Single(button => Equals(button.Content, "增量更新谱面")).IsEnabled
                && buttons.Single(button => Equals(button.Content, "检查近期资源")).IsEnabled
                && buttons.Single(button => Equals(button.Content, "刷新本地状态")).IsEnabled
                && !buttons.Single(button => Equals(button.Content, "取消同步")).IsEnabled,
            "取消后必须重新启用同步及刷新按钮");

        CheckFirstLiveSync(args[0], fixture, modePath, dialogs, manager);
        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
        Dispatcher.UIThread.RunJobs();
        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
        using var bitmap = window.CaptureRenderedFrame() ?? throw new InvalidOperationException("离屏设置页未渲染");
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1]))!);
        bitmap.Save(args[1]);
        window.Close();
        CheckFailedPerformanceTask();
        var performanceControl = new PerformanceSettingsUserControl();
        var performanceWindow = new Window { Width = 960, Height = 680, Content = performanceControl, Padding = new Thickness(20) };
        performanceWindow.Show();
        Dispatcher.UIThread.RunJobs();
        var performance = (PerformanceSettingsUserControlModel)performanceControl.DataContext!;
        Require(performanceControl.GetVisualDescendants().OfType<TextBlock>().Any(block =>
                block.Text?.Contains("以实际 FAST 与 LATE（SLOW）接近平衡为目标") == true
                && block.Text.Contains("不因 MISS")), "校准帮助必须显示平衡目标而非旧 MASTER 精度门槛");
        var profileTestRoot = Path.Combine(configDirectory, "calibration-profiles");
        Directory.CreateDirectory(profileTestRoot);
        var profileTestPath = Path.Combine(profileTestRoot, "feedback-smoke.json");
        var previousProfileTest = File.Exists(profileTestPath) ? File.ReadAllBytes(profileTestPath) : null;
        try
        {
            foreach (var feedback in new[] { "{\"fast\":null}", "{\"fast\":true,\"late\":\"2\"}" })
            {
                File.WriteAllText(profileTestPath, "{\"accepted\":true,\"difficulty\":\"master\",\"offset_ms\":0,\"validation\":" + feedback + "}");
                performance.RefreshCommand.Execute(null);
                Require(performance.CalibrationText.Contains("FAST 未记录 / LATE 未记录"),
                    "缺失、null 或非整数反馈须显示未记录，不得填零或显示空白");
            }
        }
        finally
        {
            if (previousProfileTest is null) File.Delete(profileTestPath);
            else File.WriteAllBytes(profileTestPath, previousProfileTest);
            performance.RefreshCommand.Execute(null);
        }
        Require(performanceControl.GetVisualDescendants().OfType<ComboBox>().Count() == 2,
            "演奏设置必须提供引擎与每局体力选项");
        Require(!new PerformanceSettingsUserControlModel().CooperativeGameTimingFeedback,
            "协力 FAST / LATE 试验微调必须默认关闭");
        var feedbackCheckBox = performanceControl.GetVisualDescendants().OfType<CheckBox>().Single(checkBox =>
            Equals(checkBox.Content, "协力演奏中按 FAST / LATE 微调（试验）"));
        feedbackCheckBox.IsChecked = true;
        Dispatcher.UIThread.RunJobs();
        Require(performance.CooperativeGameTimingFeedback, "试验微调复选框必须双向绑定设置值");
        performance.EngineIndex = 1; performance.TouchOffsetMs = -23; performance.BonusIndex = 6;
        performance.UseCalibrationProfile = true;
        performance.SaveCommand.Execute(null);
        var savedPerformance = Path.Combine(configDirectory, "performance-settings.json");
        var saved = JObject.Parse(File.ReadAllText(savedPerformance));
        Require(saved.Value<string>("engine") == "native" && saved.Value<int>("touch_offset_ms") == -23
            && saved.Value<int>("bonus_consumption") == 5
            && saved.Value<bool>("cooperative_game_timing_feedback"), "设置必须真实保存为 Agent 可读取的配置");
        var reloaded = new PerformanceSettingsUserControlModel();
        reloaded.RefreshCommand.Execute(null);
        Require(reloaded.TouchOffsetMs == -23 && reloaded.BonusIndex == 6 && reloaded.EngineIndex == 1
                && reloaded.CooperativeGameTimingFeedback,
            "重新打开设置页必须还原保存值");
        var oldPerformance = (JObject)saved.DeepClone();
        oldPerformance.Remove("cooperative_game_timing_feedback");
        File.WriteAllText(savedPerformance, oldPerformance.ToString());
        reloaded.RefreshCommand.Execute(null);
        Require(!reloaded.CooperativeGameTimingFeedback && reloaded.TouchOffsetMs == -23 && reloaded.BonusIndex == 6,
            "旧配置缺少试验开关时须恢复为关闭并保留手动偏移和体力");
        foreach (var invalidFeedback in new JToken[] { new JValue("true"), new JValue(1), JValue.CreateNull() })
        {
            oldPerformance["cooperative_game_timing_feedback"] = invalidFeedback;
            File.WriteAllText(savedPerformance, oldPerformance.ToString());
            reloaded.RefreshCommand.Execute(null);
            Require(reloaded.StatusText.Contains("开关必须为布尔值"), "设置页必须拒绝非布尔试验开关");
        }
        performance.CooperativeGameTimingFeedback = false;
        performance.SaveCommand.Execute(null);
        var disabledPerformance = JObject.Parse(File.ReadAllText(savedPerformance));
        Require(!disabledPerformance.Value<bool>("cooperative_game_timing_feedback")
                && disabledPerformance.Value<int>("touch_offset_ms") == -23,
            "关闭试验微调也须持久化，不能改写手动偏移");
        performance.CooperativeGameTimingFeedback = true;
        performance.SaveCommand.Execute(null);
        var loggingCheckBox = performanceControl.GetVisualDescendants().OfType<CheckBox>().Single(checkBox =>
            Equals(checkBox.Content, "不写入任何日志"));
        loggingCheckBox.IsChecked = true;
        Dispatcher.UIThread.RunJobs();
        Require(performance.DisableLogging, "不写日志复选框须双向绑定");
        performance.SaveCommand.Execute(null);
        Require(JObject.Parse(File.ReadAllText(savedPerformance)).Value<bool>("disable_logging") && MaaPjskDiagnostics.Disabled,
            "禁日志设置须持久化并动态应用到 GUI");
        var loggingReloaded = new PerformanceSettingsUserControlModel();
        loggingReloaded.RefreshCommand.Execute(null);
        Require(loggingReloaded.DisableLogging, "禁日志开关重新打开须恢复");
        Parallel.For(0, 40, index => LoggerHelper.Info("disabled backlog fixture " + index));
        var cache = (System.Collections.ICollection)typeof(LoggerHelper).GetField("_logCache", BindingFlags.Static | BindingFlags.NonPublic)!.GetValue(null)!;
        Require(cache.Count == 0, "禁日志期间不可积累重新开启后补写的 backlog");
        var savedWhileDisabled = File.ReadAllBytes(savedPerformance);
        Require(MaaPjskMaintenance.TryBegin(), "设置互斥 fixture 进入维护");
        try { performance.DisableLogging = false; performance.SaveCommand.Execute(null); }
        finally { MaaPjskMaintenance.End(); }
        Require(savedWhileDisabled.SequenceEqual(File.ReadAllBytes(savedPerformance)), "维护时禁止保存新开关");
        performance.DisableLogging = false;
        performance.SaveCommand.Execute(null);
        Require(!MaaPjskDiagnostics.Disabled, "重新允许日志须立即应用");
        var savedBefore = File.ReadAllBytes(savedPerformance);
        tabs.Add(runningTab);
        performance.TouchOffsetMs = 10; performance.SaveCommand.Execute(null);
        Require(savedBefore.SequenceEqual(File.ReadAllBytes(savedPerformance)) && performance.StatusText.Contains("先停止"),
            "运行中的任务禁止修改全局设置");
        tabs.Clear();
        performance.TouchOffsetMs = -23;
        performance.BonusIndex = 12; performance.SaveCommand.Execute(null);
        Require(savedBefore.SequenceEqual(File.ReadAllBytes(savedPerformance)), "越界体力数量不能写入配置");
        performance.BonusIndex = 6;
        performance.SaveCommand.Execute(null);
        var beforeRefresh = File.ReadAllBytes(savedPerformance);
        performance.RefreshCommand.Execute(null);
        Dispatcher.UIThread.RunJobs();
        Require(beforeRefresh.SequenceEqual(File.ReadAllBytes(savedPerformance)), "初始化读取不能触发保存");
        performanceControl.GetVisualDescendants().OfType<TextBox>().First().Text = "-";
        Dispatcher.UIThread.RunJobs();
        Require(performance.TouchOffsetText == "-", "数值文本编辑必须立即同步，不能等失焦提交");
        Require(!PerformanceSettingsUserControlModel.FlushBeforeTaskStart()
            && beforeRefresh.SequenceEqual(File.ReadAllBytes(savedPerformance)), "未输入完成的数值必须阻止启动且保留配置");
        performance.TouchOffsetText = "-24";
        Require(PerformanceSettingsUserControlModel.FlushBeforeTaskStart()
            && JObject.Parse(File.ReadAllText(savedPerformance)).Value<int>("touch_offset_ms") == -24,
            "立即启动必须保存尚未经过防抖的文本编辑");
        var extended = JObject.Parse(File.ReadAllText(savedPerformance));
        extended["future_setting"] = "preserved";
        File.WriteAllText(savedPerformance, extended.ToString());
        performance.RetryCountText = "3";
        using (var debounceLoop = new CancellationTokenSource(1200))
            Dispatcher.UIThread.MainLoop(debounceLoop.Token);
        Require(JObject.Parse(File.ReadAllText(savedPerformance)).Value<int>("play_failure_retry_count") == 3
            && JObject.Parse(File.ReadAllText(savedPerformance)).Value<string>("future_setting") == "preserved",
            "防抖自动保存必须真实写入并保留扩展字段");
        Require(MaaPjskMaintenance.TryBegin(), "自动保存占用测试进入维护");
        try
        {
            performance.TouchOffsetText = "-25";
            Require(!performance.FlushPending() && performance.StatusText.Contains("未保存"), "维护占用保留未保存状态");
        }
        finally { MaaPjskMaintenance.End(); }
        Require(PerformanceSettingsUserControlModel.FlushBeforeTaskStart(), "维护结束后启动边界应重试待保存值");
        var preserved = File.ReadAllBytes(savedPerformance);
        File.Delete(savedPerformance);
        Directory.CreateDirectory(savedPerformance);
        try
        {
            performance.TouchOffsetText = "-26";
            Require(!performance.FlushPending() && performance.StatusText.Contains("失败"), "写入失败必须可见且阻止启动");
        }
        finally { Directory.Delete(savedPerformance); File.WriteAllBytes(savedPerformance, preserved); }
        Require(performance.FlushPending(), "修复写入条件后应可重试保存");
        performance.TouchOffsetText = "-";
        performance.Deactivate();
        GC.Collect();
        var reopenedPerformance = new PerformanceSettingsUserControl();
        Require(ReferenceEquals(reopenedPerformance.DataContext, performance)
            && ((PerformanceSettingsUserControlModel)reopenedPerformance.DataContext!).TouchOffsetText == "-",
            "重新打开设置页须显示同一未保存编辑，不保留不可修复的隐藏旧实例");
        Require(!PerformanceSettingsUserControlModel.FlushBeforeTaskStart(), "切页后未保存的无效编辑仍须阻止启动");
        performance.TouchOffsetText = "-23";
        Require(PerformanceSettingsUserControlModel.FlushBeforeTaskStart(), "切页后的有效待保存值仍能由启动边界保存");
        performance.Activate();
        performance.Activate();
        performance.Deactivate();
        performance.TouchOffsetText = "-";
        Require(!PerformanceSettingsUserControlModel.FlushBeforeTaskStart(), "另一窗口卸载后共享编辑器仍必须参与启动保存门禁");
        performance.TouchOffsetText = "-23";
        performance.FlushPending();
        performance.Activate();
        var validBeforeLoadFailure = File.ReadAllBytes(savedPerformance);
        File.WriteAllText(savedPerformance, "{broken");
        performance.RefreshCommand.Execute(null);
        performance.SaveCommand.Execute(null);
        Require(File.ReadAllText(savedPerformance) == "{broken" && performance.StatusText.Contains("读取失败"),
            "读取失败后的手动保存不能覆盖损坏配置");
        File.WriteAllBytes(savedPerformance, validBeforeLoadFailure);
        performance.RefreshCommand.Execute(null);
        performance.TouchOffsetText = "-23";
        performance.RetryCountText = "0";
        performance.FlushPending();
        FailureRetrySmoke.Run(Require, operation => Wait(operation));
        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
        using var performanceBitmap = performanceWindow.CaptureRenderedFrame() ?? throw new InvalidOperationException("演奏设置未渲染");
        performanceBitmap.Save(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[1]))!, "performance-settings.png"));
        performanceWindow.Close();
        Console.WriteLine($"MFA 谱面管理离屏验证通过：{_checks} 项，无桌面输入、无 Maa 控制器。");
        return 0;
    }
}
