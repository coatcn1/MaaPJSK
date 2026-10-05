using Avalonia;
using Avalonia.Headless;
using Avalonia.Threading;
using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.InteropServices;

internal sealed class SleepApplication : Application;

internal static class Program
{
    private const uint Continuous = 0x80000000;
    private const uint Required = 0x80000003;
    private static int _checks;

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint SetThreadExecutionState(uint flags);

    private static void Require(bool condition, string reason)
    {
        if (!condition) throw new InvalidOperationException(reason);
        _checks++;
    }

    private static void Wait(Task task)
    {
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!task.IsCompleted && DateTime.UtcNow < deadline)
        {
            Dispatcher.UIThread.RunJobs();
            Thread.Sleep(5);
        }
        if (!task.IsCompleted) throw new TimeoutException("任务生命周期离屏检查超时");
        task.GetAwaiter().GetResult();
        Dispatcher.UIThread.RunJobs();
    }

    private static uint ReadExecutionState()
    {
        // Windows 返回调用线程的旧状态；读取后立即恢复，避免探针解除任务保护。
        var state = SetThreadExecutionState(Continuous);
        Require(state != 0, "无法读取本线程的实际 Windows 执行状态");
        Require(SetThreadExecutionState(state) != 0, "读取后必须恢复本线程的执行状态");
        return state;
    }

    private static MaaProcessor Queue(MFATask task)
    {
        // 只实例化真实队列和任务动作，不创建 Maa 控制器、资源或游戏输入。
        var processor = (MaaProcessor)RuntimeHelpers.GetUninitializedObject(typeof(MaaProcessor));
        var queue = new ObservableQueue<MFATask>();
        typeof(MaaProcessor).GetField("<TaskQueue>k__BackingField", BindingFlags.NonPublic | BindingFlags.Instance)!
            .SetValue(processor, queue);
        queue.Enqueue(task);
        return processor;
    }

    private static Task Execute(MaaProcessor processor, CancellationToken token = default)
    {
        var method = typeof(MaaProcessor).GetMethod("ExecuteTasks", BindingFlags.NonPublic | BindingFlags.Instance)!;
        return Task.Run(async () => await (Task)method.Invoke(processor, [token])!);
    }

    private static void CheckSavedSettingAndLastTask()
    {
        // 与本次实机完全相同：旧配置开关为 true，全局配置没有这个字段。
        File.WriteAllText(AppPaths.GlobalConfigPath, "{}");
        ConfigurationManager.Current.SetValue(ConfigurationKeys.PreventSleep, true);
        Require(new PerformanceUserControlModel().PreventSleep, "设置页必须显示旧配置中已开启的开关");
        SystemSleepHelper.ApplyPreventSleep();
        Require(ReadExecutionState() == Continuous, "MFA 空闲时不应阻止息屏");
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var finished = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var processor = Queue(new MFATask { Action = async () => { entered.TrySetResult(); await finished.Task; } });
        var execution = Execute(processor);
        try
        {
            Wait(entered.Task);
            Require(processor.TaskQueue.Count == 0, "覆盖最后一个任务已出队、仍在执行的边界");
            Require(ReadExecutionState() == Required, "开关已开启时，真实任务执行期间必须同时阻止息屏和休眠");
        }
        finally
        {
            finished.TrySetResult();
            Wait(execution);
        }
        Require(ReadExecutionState() == Continuous, "任务完成后必须解除防息屏请求");
    }

    private static void CheckFailureAndCancellation()
    {
        SystemSleepHelper.SavePreventSleepSetting(true);
        var failed = Queue(new MFATask { Action = () => Task.FromException(new InvalidOperationException("预期的合成失败")) });
        Wait(Execute(failed));
        Require(ReadExecutionState() == Continuous, "任务异常失败后必须解除请求");
        using var cancellation = new CancellationTokenSource();
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var cancelled = Queue(new MFATask { Action = async () => {
            entered.TrySetResult();
            await Task.Delay(Timeout.Infinite, cancellation.Token);
        } });
        var run = Execute(cancelled, cancellation.Token);
        Wait(entered.Task);
        Require(ReadExecutionState() == Required, "停止前仍在执行的任务必须受到保护");
        cancellation.Cancel();
        Wait(run);
        Require(ReadExecutionState() == Continuous, "用户取消任务后必须解除请求");
        var actionCalled = false;
        var beforeStart = Queue(new MFATask { Action = () => { actionCalled = true; return Task.CompletedTask; } });
        Wait(Execute(beforeStart, new CancellationToken(true)));
        Require(!actionCalled && ReadExecutionState() == Continuous, "已取消的启动不得运行任务或申请保护");
        var empty = Queue(new MFATask { Action = () => throw new InvalidOperationException("空队列不可执行") });
        empty.TaskQueue.Clear();
        Wait(Execute(empty));
        Require(ReadExecutionState() == Continuous, "空队列不得申请保护");
    }

    private static void CheckConcurrentTasksAndSettingChanges()
    {
        SystemSleepHelper.SavePreventSleepSetting(false);
        var model = new PerformanceUserControlModel();
        Require(!model.PreventSleep, "新的全局关闭值必须覆盖旧配置的 true");
        var enteredA = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var enteredB = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var finishA = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var finishB = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var first = Execute(Queue(new MFATask { Action = async () => { enteredA.TrySetResult(); await finishA.Task; } }));
        var second = Execute(Queue(new MFATask { Action = async () => { enteredB.TrySetResult(); await finishB.Task; } }));
        try
        {
            Wait(enteredA.Task);
            Wait(enteredB.Task);
            Require(ReadExecutionState() == Continuous, "开关关闭时，即使有任务执行也不应申请保护");
            model.PreventSleep = true;
            Require(GlobalConfiguration.GetValue(ConfigurationKeys.PreventSleep) == "true", "设置页必须保存到实际执行读取的配置");
            Require(ReadExecutionState() == Required, "运行中开启开关必须立即生效");
            finishA.TrySetResult();
            Wait(first);
            Require(ReadExecutionState() == Required, "一个实例结束时不能释放另一个实例的保护");
            model.PreventSleep = false;
            Require(ReadExecutionState() == Continuous, "运行中关闭开关必须立即解除请求");
            model.PreventSleep = true;
            Require(ReadExecutionState() == Required, "再次开启必须重新建立请求");
        }
        finally
        {
            finishA.TrySetResult();
            finishB.TrySetResult();
            Wait(first);
            Wait(second);
        }
        Require(ReadExecutionState() == Continuous, "所有实例结束后应恢复系统电源策略");
        model.PreventSleep = false;
        model.PreventSleep = true;
        Require(ReadExecutionState() == Continuous, "空闲时切换开关只保存偏好，不能建立保护");
    }

    private static void CheckQueuedCancellationAndDuplicateDisposal()
    {
        using var cancellation = new CancellationTokenSource();
        var queued = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var acquisition = Task.Run(async () => {
            var requested = SystemSleepHelper.BeginTaskExecutionAsync(cancellation.Token);
            queued.TrySetResult();
            return await requested;
        });
        // 暂不推进 UI 调度器，确保取消发生在唤醒请求排队后、建立前。
        Require(queued.Task.Wait(TimeSpan.FromSeconds(5)), "作用域申请应已排到 UI 线程");
        cancellation.Cancel();
        Wait(acquisition);
        acquisition.Result.Dispose();
        Require(ReadExecutionState() == Continuous, "UI 排队期间取消不得建立请求");

        var active = SystemSleepHelper.BeginTaskExecutionAsync();
        Wait(active);
        Require(ReadExecutionState() == Required, "未取消的作用域必须建立请求");
        Wait(Task.Run(() => { active.Result.Dispose(); active.Result.Dispose(); }));
        Require(ReadExecutionState() == Continuous, "跨线程重复 Dispose 只能解除一次，且在原线程完成");
    }

    private static void CheckShutdownAndLateCallbacks()
    {
        var active = SystemSleepHelper.BeginTaskExecutionAsync();
        Wait(active);
        Require(ReadExecutionState() == Required, "退出前存在实际唤醒请求");
        SystemSleepHelper.Shutdown();
        Require(ReadExecutionState() == Continuous, "应用退出时必须解除请求");
        var late = Task.Run(() => SystemSleepHelper.BeginTaskExecutionAsync());
        Wait(late);
        Wait(Task.Run(() => { active.Result.Dispose(); late.Result.Dispose(); SystemSleepHelper.ApplyPreventSleep(); }));
        Require(ReadExecutionState() == Continuous && !SystemSleepHelper.IsPreventingSleep,
            "退出后的迟到作用域和设置回调不得重新申请保护");
    }

    public static int Main()
    {
        if (!OperatingSystem.IsWindows()) throw new PlatformNotSupportedException("本检查须在 Windows 运行");
        AppBuilder.Configure<SleepApplication>().UseHeadless(new AvaloniaHeadlessPlatformOptions()).SetupWithoutStarting();
        try
        {
            CheckSavedSettingAndLastTask();
            CheckFailureAndCancellation();
            CheckConcurrentTasksAndSettingChanges();
            CheckQueuedCancellationAndDuplicateDisposal();
            CheckShutdownAndLateCallbacks();
            Console.WriteLine($"任务防息屏离屏检查通过：{_checks} 项，无 Maa 控制器、无游戏输入。");
            return 0;
        }
        finally
        {
            SetThreadExecutionState(Continuous);
        }
    }
}
