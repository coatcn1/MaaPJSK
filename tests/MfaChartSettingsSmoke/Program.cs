using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MaaFramework.Binding;
using MFAAvalonia;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Other;
using MFAAvalonia.ViewModels.Pages;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using MFAAvalonia.Views.UserControls.Settings;
using Microsoft.Extensions.DependencyInjection;
using Newtonsoft.Json.Linq;
using SukiUI;
using SukiUI.Toasts;
using System.Collections.ObjectModel;
using System.Reflection;
using System.Runtime.CompilerServices;

public sealed class SmokeApplication : Application
{
    public override void Initialize()
    {
        Styles.Add(new FluentTheme());
        Styles.Add(new SukiTheme());
    }
}

internal static class Program
{
    private static int _checks;

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
        if (args.Length != 2)
            throw new ArgumentException("参数：python.exe 和离屏截图输出路径");
        AppBuilder.Configure<SmokeApplication>().UseSkia()
            .UseHeadless(new AvaloniaHeadlessPlatformOptions { UseHeadlessDrawing = false })
            .SetupWithoutStarting();

        // 测试中的实例集合不创建 Maa 控制器，所有窗口和输入都在离屏平台的内存里。
        var tabs = new ObservableCollection<InstanceTabViewModel>();
        var tabBar = (InstanceTabBarViewModel)RuntimeHelpers.GetUninitializedObject(typeof(InstanceTabBarViewModel));
        typeof(InstanceTabBarViewModel).GetField("<Tabs>k__BackingField", BindingFlags.Instance | BindingFlags.NonPublic)!
            .SetValue(tabBar, tabs);
        typeof(App).GetProperty(nameof(App.Services))!.SetValue(null,
            new ServiceCollection().AddSingleton(tabBar).AddSingleton<ISukiToastManager, SukiToastManager>().BuildServiceProvider());

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
    sys.exit(2)
(root / 'manifest.json').write_text(json.dumps({'schema_version':1,'game':'project-sekai','server':'jp','generated_at':'2026-10-03T05:00:00+00:00','summary':{'songs_with_charts':2,'charts':3,'jackets':2,'recoverable_errors':0,'stale':0,'added':1,'updated':0,'reused':4,'unchanged':0,'downloaded_bytes':1048576}}), encoding='utf-8')
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
        var window = new Window { Width = 960, Height = 650, Content = control, Padding = new Thickness(20) };
        window.Show();
        Dispatcher.UIThread.RunJobs();
        var model = (ChartCatalogSettingsUserControlModel)control.DataContext!;
        var buttons = control.GetVisualDescendants().OfType<Button>().ToArray();
        Require(buttons.Length == 4 && buttons.All(button => button.Command is not null), "增量更新、近期检查、取消和刷新按钮必须真实绑定命令");
        Require(!buttons.Single(button => Equals(button.Content, "取消同步")).IsEnabled, "空闲时取消按钮应禁用");
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText == "增量更新完成。" && model.ProgressText.Contains("中文字符"), "同步子进程必须正确处理退出码和 UTF8 进度");
        Require(!File.ReadAllText(Path.Combine(fixture, "arguments.json")).Contains("--check-recent"), "默认按钮必须执行增量更新");
        Require(model.CatalogText.Contains("2 首 / 3 张谱面 / 2 个封面"), "更新后必须刷新本地数量");
        Require(model.CatalogText.Contains("本地复用 4") && model.CatalogText.Contains("1.00 MiB"), "更新后必须显示复用及实际下载量");
        Wait(model.CheckRecentCommand.ExecuteAsync(null));
        Require(model.StatusText == "近期资源检查完成。"
                && File.ReadAllText(Path.Combine(fixture, "arguments.json")).Contains("--check-recent"),
            "近期检查按钮必须将模式传给同步子进程");

        var runningTab = (InstanceTabViewModel)RuntimeHelpers.GetUninitializedObject(typeof(InstanceTabViewModel));
        typeof(InstanceTabViewModel).GetField("_isRunning", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(runningTab, true);
        tabs.Add(runningTab);
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("先停止所有演出任务"), "演出任务运行时不应启动同步");
        Wait(model.CheckRecentCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("先停止所有演出任务"), "演出任务运行时不应启动近期检查");
        tabs.Clear();

        File.WriteAllText(modePath, "partial");
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("部分资源更新失败") && model.ProgressText.Contains("测试镜像缺失"), "部分错误须显示详情并保留本地库");

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
        Require(buttons.Single(button => Equals(button.Content, "增量更新谱面")).IsEnabled
                && buttons.Single(button => Equals(button.Content, "检查近期资源")).IsEnabled
                && buttons.Single(button => Equals(button.Content, "刷新本地状态")).IsEnabled
                && !buttons.Single(button => Equals(button.Content, "取消同步")).IsEnabled,
            "取消后必须重新启用同步及刷新按钮");

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
        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
        using var performanceBitmap = performanceWindow.CaptureRenderedFrame() ?? throw new InvalidOperationException("演奏设置未渲染");
        performanceBitmap.Save(Path.Combine(Path.GetDirectoryName(Path.GetFullPath(args[1]))!, "performance-settings.png"));
        performanceWindow.Close();
        Console.WriteLine($"MFA 谱面管理离屏验证通过：{_checks} 项，无桌面输入、无 Maa 控制器。");
        return 0;
    }
}
