using Avalonia;
using Avalonia.Controls;
using Avalonia.Headless;
using Avalonia.Themes.Fluent;
using Avalonia.Threading;
using Avalonia.VisualTree;
using MFAAvalonia;
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
print('同步测试进度：中文字符与参数传递正常', flush=True)
if mode == 'slow':
    time.sleep(30)
if mode == 'partial':
    print('测试镜像缺失', file=sys.stderr, flush=True)
    sys.exit(2)
(root / 'manifest.json').write_text(json.dumps({'schema_version':1,'game':'project-sekai','server':'jp','generated_at':'2026-10-03T05:00:00+00:00','summary':{'songs_with_charts':2,'charts':3,'jackets':2,'recoverable_errors':0,'stale':0}}), encoding='utf-8')
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
        Require(buttons.Length == 3 && buttons.All(button => button.Command is not null), "同步、取消和刷新按钮必须真实绑定命令");
        Require(!buttons.Single(button => Equals(button.Content, "取消同步")).IsEnabled, "空闲时取消按钮应禁用");
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText == "同步完成。" && model.ProgressText.Contains("中文字符"), "同步子进程必须正确处理退出码和 UTF8 进度");
        Require(model.CatalogText.Contains("2 首 / 3 张谱面 / 2 个封面"), "更新后必须刷新本地数量");

        var runningTab = (InstanceTabViewModel)RuntimeHelpers.GetUninitializedObject(typeof(InstanceTabViewModel));
        typeof(InstanceTabViewModel).GetField("_isRunning", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(runningTab, true);
        tabs.Add(runningTab);
        Wait(model.SyncCommand.ExecuteAsync(null));
        Require(model.StatusText.Contains("先停止所有演出任务"), "演出任务运行时不应启动同步");
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
        var taskQueue = (TaskQueueViewModel)RuntimeHelpers.GetUninitializedObject(typeof(TaskQueueViewModel));
        taskQueue.StartTask();
        Require(ChartCatalogMaintenance.IsBusy, "维护保护必须在初始化 Maa 控制器之前拒绝开始任务");
        model.CancelCommand.Execute(null);
        Wait(slowSync);
        Require(model.StatusText.Contains("已取消") && !ChartCatalogMaintenance.IsBusy && model.NotSyncing,
            "取消后必须结束子进程并解除维护保护");
        Require(previousManifest.SequenceEqual(File.ReadAllBytes(manifestPath)), "取消不得替换原有 manifest");
        Require(buttons.Single(button => Equals(button.Content, "同步 / 更新全部日服谱面")).IsEnabled
                && buttons.Single(button => Equals(button.Content, "刷新本地状态")).IsEnabled
                && !buttons.Single(button => Equals(button.Content, "取消同步")).IsEnabled,
            "取消后必须重新启用同步及刷新按钮");

        AvaloniaHeadlessPlatform.ForceRenderTimerTick();
        using var bitmap = window.CaptureRenderedFrame() ?? throw new InvalidOperationException("离屏设置页未渲染");
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(args[1]))!);
        bitmap.Save(args[1]);
        window.Close();
        var performanceControl = new PerformanceSettingsUserControl();
        var performanceWindow = new Window { Width = 960, Height = 680, Content = performanceControl, Padding = new Thickness(20) };
        performanceWindow.Show();
        Dispatcher.UIThread.RunJobs();
        var performance = (PerformanceSettingsUserControlModel)performanceControl.DataContext!;
        Require(performanceControl.GetVisualDescendants().OfType<ComboBox>().Count() == 2,
            "演奏设置必须提供引擎与每局体力选项");
        performance.EngineIndex = 1; performance.TouchOffsetMs = -23; performance.BonusIndex = 6;
        performance.UseCalibrationProfile = true;
        performance.SaveCommand.Execute(null);
        var savedPerformance = Path.Combine(configDirectory, "performance-settings.json");
        var saved = JObject.Parse(File.ReadAllText(savedPerformance));
        Require(saved.Value<string>("engine") == "native" && saved.Value<int>("touch_offset_ms") == -23
            && saved.Value<int>("bonus_consumption") == 5, "设置必须真实保存为 Agent 可读取的配置");
        var reloaded = new PerformanceSettingsUserControlModel();
        reloaded.RefreshCommand.Execute(null);
        Require(reloaded.TouchOffsetMs == -23 && reloaded.BonusIndex == 6 && reloaded.EngineIndex == 1,
            "重新打开设置页必须还原保存值");
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
