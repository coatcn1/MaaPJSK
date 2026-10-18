using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Threading;
using MaaFramework.Binding;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper;
using MFAAvalonia.Helper.ValueType;
using MFAAvalonia.ViewModels.Other;
using MFAAvalonia.ViewModels.UsersControls.Settings;
using Newtonsoft.Json.Linq;
using SukiUI.Dialogs;
using System.Reflection;
using System.Runtime.CompilerServices;

internal static class FailureRetrySmoke
{
    public static void Run(Action<bool, string> require, Action<Task> wait)
    {
        var savedPath = Path.Combine(AppContext.BaseDirectory, "config", "performance-settings.json");
        var original = File.ReadAllBytes(savedPath);
        try
        {
            var model = new PerformanceSettingsUserControlModel();
            model.RefreshCommand.Execute(null);
            foreach (var count in new[] { 0, 99 })
            {
                model.PlayFailureRetryCount = count;
                model.SaveCommand.Execute(null);
                var loaded = new PerformanceSettingsUserControlModel();
                loaded.RefreshCommand.Execute(null);
                require(loaded.PlayFailureRetryCount == count, "中途失败重试 0/99 应真实保存并重新读取");
            }
            var valid = JObject.Parse(File.ReadAllText(savedPath));
            foreach (var invalid in new JToken[] { new JValue(true), new JValue(1.5), new JValue("1"), JValue.CreateNull(), new JValue(-1), new JValue(100) })
            {
                var value = (JObject)valid.DeepClone();
                value["play_failure_retry_count"] = invalid;
                File.WriteAllText(savedPath, value.ToString());
                var invalidModel = new PerformanceSettingsUserControlModel();
                invalidModel.RefreshCommand.Execute(null);
                require(invalidModel.StatusText.Contains("读取演奏设置失败"), "非法重试类型或范围不能被静默转换");
            }
            valid.Remove("play_failure_retry_count");
            File.WriteAllText(savedPath, valid.ToString());
            var old = new PerformanceSettingsUserControlModel();
            old.RefreshCommand.Execute(null);
            require(old.PlayFailureRetryCount == 0, "旧 schema1 缺重试字段应保持关闭");
        }
        finally { File.WriteAllBytes(savedPath, original); }

        var root = Path.Combine(Path.GetTempPath(), "maapjsk-failure-summary-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(Path.Combine(root, "config"));
        File.WriteAllText(Path.Combine(root, "config", "performance-settings.json"), "{\"play_failure_retry_count\":1}");
        try
        {
            var pipeline = MaaToken.FromDictionary(new Dictionary<string, JToken>
            {
                ["SoloChartLiveRun"] = JObject.Parse("{\"custom_action_param\":{\"preserved\":7,\"overwritten\":1},\"next\":[\"A\"]}"),
                ["OtherNode"] = JObject.Parse("{\"next\":[\"B\"]}")
            });
            pipeline.Merge(new Dictionary<string, JToken>
            {
                ["SoloChartLiveRun"] = JObject.Parse("{\"custom_action_param\":{\"preserved\":9}}"),
                ["OtherNode"] = JObject.Parse("{\"timeout\":123}")
            });
            var layers = JArray.Parse(pipeline.ToString());
            var arraySession = new MaaPjskFailureSummary(root);
            var prepared = JArray.Parse(arraySession.Prepare("SoloChartLive", pipeline.ToString())!);
            require(prepared.Count == layers.Count + 1 && layers.Select((layer, index) => JToken.DeepEquals(layer, prepared[index])).All(equal => equal),
                "真实 MaaToken 数组必须逐层保留原覆盖顺序，不能展开或改写已有层");
            var finalLayer = (JObject)prepared.Last!;
            var custom = finalLayer["SoloChartLiveRun"]!["custom_action_param"]!;
            require(finalLayer.Count == 1 && ((JObject)finalLayer["SoloChartLiveRun"]!).Count == 1
                    && custom.Value<int>("preserved") == 9 && custom["overwritten"] is null
                    && custom.Value<string>("execution_id") == arraySession.ExecutionId,
                "最终只追加 Run 参数身份，并保留最后生效的旧参数字段");
            arraySession.Claim("STOPPED");
            string Seed(MaaPjskFailureSummary session, bool finalized = true, int failures = 2, string status = "stopped")
            {
                var param = JObject.Parse(session.Prepare("SoloChartLive", "{\"SoloChartLiveRun\":{\"custom_action_param\":{\"preserved\":7}},\"OtherNode\":{\"next\":[]}}")!);
                var data = (JObject)param["SoloChartLiveRun"]!["custom_action_param"]!;
                require(data.Value<int>("preserved") == 7 && param["OtherNode"] is not null,
                    "身份注入必须保留既有节点与 custom_action_param 字段");
                var id = data.Value<string>("summary_id")!;
                var path = Path.Combine(root, "config", "play-failure-summaries", id + ".json");
                var value = JObject.Parse(File.ReadAllText(path));
                value["agent_attached"] = true;
                value["finalized"] = finalized;
                value["failure_count"] = failures;
                value["retries_used"] = failures == 0 ? 0 : 1;
                value["status"] = status;
                File.WriteAllText(path, value.ToString());
                return id;
            }
            var first = new MaaPjskFailureSummary(root);
            var one = Seed(first);
            var two = Seed(first);
            require(one != two, "同一 closure 重复 AppendTask 必须每次生成新的 summary 身份");
            require(first.Prepare("SoloChartLiveLog", "{}") == "{}", "嵌套日志不能覆盖真实演出身份");
            foreach (var entry in new[] { "SoloChartCalibration", "OneShotChartLive", "AutoLive" })
                require(first.Prepare(entry, "{}") == "{}", "校准、一键和 AUTO 不接入死亡重试汇总");
            var notices = first.Claim("STOPPED");
            require(notices.Count == 2 && notices.All(notice => notice.Content.Contains("累计失败：2 次")), "两个真实 run 应各保留累计失败");
            require(!File.Exists(Path.Combine(root, "config", "play-failure-summaries", one + ".json"))
                    && !File.Exists(Path.Combine(root, "config", "play-failure-summaries", two + ".json")),
                "终态消费应精确清理自己的临时状态");
            require(first.Claim("FAILED").Count == 0, "终态重入只能消费一次");
            var next = new MaaPjskFailureSummary(root);
            Seed(next);
            first.Claim("SUCCEEDED");
            require(next.Claim("STOPPED").Count == 1, "旧终态重入不能消费新执行");
            var incomplete = new MaaPjskFailureSummary(root);
            Seed(incomplete, finalized: false, failures: 1);
            var warning = incomplete.Claim("STOPPED").Single();
            require(warning.Warning && warning.Content.Contains("统计尚未完整保存"), "未 finalized 必须显示最后确认值与不完整警告");
            var corrupt = new MaaPjskFailureSummary(root);
            var corruptId = Seed(corrupt);
            File.WriteAllText(Path.Combine(root, "config", "play-failure-summaries", corruptId + ".json"), "{}");
            require(corrupt.Claim("FAILED").Single().Content.Contains("次数未知"), "无效状态不能被当作失败零次");
            var zero = new MaaPjskFailureSummary(root);
            Seed(zero, failures: 0);
            require(zero.Claim("SUCCEEDED").Count == 0, "没有真实失败时不弹汇总");
            foreach (var (status, text) in new[] { ("succeeded", "成功结束"), ("failed", "异常终止"), ("stopped", "用户停止") })
            {
                var terminal = new MaaPjskFailureSummary(root);
                Seed(terminal, status: status);
                require(terminal.Claim(status).Single().Content.Contains(text), "成功、异常和停止必须显示实际终态");
            }

            // 已有弹窗先保留；统计通知等待其关闭后显示一次，停止或禁日志不能吞掉它。
            var manager = Instances.DialogManager;
            var existing = manager.CreateDialog().WithTitle("已有提示").WithContent("保持原提示")
                .WithActionButton("确定", _ => { }, true);
            existing.TryShow();
            var shown = new List<ISukiDialog>();
            SukiDialogManagerEventHandler handler = (_, args) => shown.Add(args.Dialog);
            manager.OnDialogShown += handler;
            try
            {
                var pending = MaaPjskFailureSummary.ShowAsync(notices);
                Dispatcher.UIThread.RunJobs();
                require(!pending.IsCompleted && shown.Count == 0, "已有提示时汇总必须排队");
                existing.Dialog.ActionButtons.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
                wait(pending);
                require(shown.Count == 1, "关闭已有提示后应只显示一次汇总");
                shown.Single().ActionButtons.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            }
            finally { manager.OnDialogShown -= handler; }

            // 直接调用真实生成 Processor 的迟到终态钩子，核对它不会释放新启动租约。
            var processor = (MaaProcessor)RuntimeHelpers.GetUninitializedObject(typeof(MaaProcessor));
            var instanceId = typeof(MaaProcessor).GetField("<InstanceId>k__BackingField", BindingFlags.Instance | BindingFlags.NonPublic)!;
            instanceId.SetValue(processor, "failure-retry-offscreen");
            var field = typeof(MaaProcessor).GetField("_pjskFailureSummary", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var execution = typeof(MaaProcessor).GetField("_pjskExecution", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var hook = typeof(MaaProcessor).GetMethod("HandleStopResult", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var release = typeof(MaaProcessor).GetMethod("ReleaseProjectExecution", BindingFlags.Instance | BindingFlags.NonPublic)!;
            foreach (var failedStop in new[] { MaaJobStatus.Failed, MaaJobStatus.Invalid })
            {
                var unfinished = new MaaPjskFailureSummary(root);
                var unfinishedId = Seed(unfinished, finalized: false, failures: 1);
                field.SetValue(processor, unfinished);
                execution.SetValue(processor, MaaPjskMaintenance.BeginExecution());
                try
                {
                    hook.Invoke(processor, [MFATask.MFATaskStatus.STOPPED, failedStop, false, null, false, unfinished]);
                    require(processor.HasProjectExecution && File.Exists(Path.Combine(root, "config", "play-failure-summaries", unfinishedId + ".json")),
                        "停止 Failed/Invalid 尚无真实终止证明，必须保留租约和原统计，不能当作终态消费");
                }
                finally { release.Invoke(processor, null); }
            }
            var newSession = new MaaPjskFailureSummary(root);
            var newId = Seed(newSession);
            field.SetValue(processor, newSession);
            hook.Invoke(processor, [MFATask.MFATaskStatus.STOPPED, MaaJobStatus.Succeeded, false, null, false, first]);
            require(ReferenceEquals(field.GetValue(processor), newSession)
                    && File.Exists(Path.Combine(root, "config", "play-failure-summaries", newId + ".json"))
                    && newSession.Claim("STOPPED").Count == 1,
                "旧执行迟到真实终态不能清空或消费新任务统计");

            var active = new MaaPjskFailureSummary(root);
            var activeId = Seed(active, finalized: false, failures: 1);
            var activePath = Path.Combine(root, "config", "play-failure-summaries", activeId + ".json");
            active.MarkTaskSubmitted();
            field.SetValue(processor, active);
            execution.SetValue(processor, MaaPjskMaintenance.BeginExecution());
            var stopException = typeof(MaaProcessor).GetMethod("HandleStopException", BindingFlags.Instance | BindingFlags.NonPublic)!;
            stopException.Invoke(processor, [new InvalidOperationException("合成停止异常"), active]);
            var reserve = typeof(MaaProcessor).GetMethod("ReserveProjectExecution", BindingFlags.Instance | BindingFlags.NonPublic)!;
            require(processor.HasProjectExecution && File.Exists(activePath) && !active.IsClosed
                    && !(bool)reserve.Invoke(processor, null)!,
                "停止异常且未确认终止必须保留文件和租约，拒绝新启动而不是提前弹窗");
            var cancelled = new CancellationTokenSource();
            cancelled.Cancel();
            typeof(MaaProcessor).GetProperty("CancellationTokenSource")!.SetValue(processor, cancelled);
            var shouldStop = typeof(MaaProcessor).GetMethod("ShouldProcessStop", BindingFlags.Instance | BindingFlags.NonPublic)!;
            require((bool)shouldStop.Invoke(processor, [false])!, "已有执行即使取消令牌已取消也必须允许再次停止");
            require(active.TryBeginStop() && !active.TryBeginStop(), "仅正在进行的停止请求可阻止并发重复停止");
            active.EndStop();
            require(active.TryBeginStop(), "失败请求退出后必须允许新停止请求");
            active.EndStop();

            var observation = typeof(MaaProcessor).GetField("_pjskTaskerObservation", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var confirm = typeof(MaaProcessor).GetMethod("ConfirmProjectStopAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var aborted = 0;
            MaaJobStatus Confirm(MaaPjskTaskerObservation observed, MaaJobStatus response)
            {
                observation.SetValue(processor, (Func<MaaPjskTaskerObservation>)(() => observed));
                var attempt = (Task<MaaJobStatus>)confirm.Invoke(processor, [active,
                    (Func<MaaJobStatus>)(() => { aborted++; return response; }), 2, 0])!;
                wait(attempt);
                return attempt.Result;
            }
            active.MarkExecutionReturned();
            require(Confirm(default, MaaJobStatus.Succeeded) == MaaJobStatus.Invalid && aborted == 0,
                "无效或不存在的执行器不能反射成已终止证明，也不能冒充成功回执");
            require(Confirm(new(true, true, true), MaaJobStatus.Succeeded) == MaaJobStatus.Invalid && aborted == 0,
                "执行器仍在停止中时必须等待，不可直接释放或重复并发 Stop");
            foreach (var response in new[] { MaaJobStatus.Failed, MaaJobStatus.Invalid })
            {
                var result = Confirm(new(true, true, false), response);
                hook.Invoke(processor, [MFATask.MFATaskStatus.FAILED, result, false, null, false, active]);
                require(processor.HasProjectExecution && !active.IsClosed && File.Exists(activePath),
                    "重复停止失败仍须保留最新统计；请求 FAILED 不得跳过真实终止门槛");
            }
            require(aborted == 4, "每次有界停止均真正查询回执，不能沿用默认成功");

            // 异常发生在 await 之后，真实生成的异步停止核心仍须捕获，不能成为 async void。
            active.TryBeginStop();
            var enteredStop = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var completeStop = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var core = typeof(MaaProcessor).GetMethod("ExecuteStopCore", BindingFlags.Instance | BindingFlags.NonPublic)!;
            require(core.GetParameters()[1].ParameterType == typeof(Func<Task>), "真实停止核心必须等待异步委托");
            core.Invoke(processor, [true, (Func<Task>)(async () => {
                enteredStop.TrySetResult();
                await completeStop.Task;
                throw new InvalidOperationException("await 后合成停止异常");
            }), active]);
            wait(enteredStop.Task);
            require(processor.HasProjectExecution && active.StopInFlight, "异步停止尚未完成时不能提前释放租约");
            completeStop.TrySetResult();
            wait(Task.Run(async () => {
                var deadline = DateTime.UtcNow.AddSeconds(5);
                while (active.StopInFlight && DateTime.UtcNow < deadline) await Task.Delay(5);
                if (active.StopInFlight) throw new TimeoutException("异步停止异常未完成收尾");
            }));
            require(processor.HasProjectExecution && !active.IsClosed && File.Exists(activePath),
                "await 后停止异常已捕获但未有终止证明，仍须保留作用域与统计");

            var latest = JObject.Parse(File.ReadAllText(activePath));
            latest["failure_count"] = 2;
            latest["finalized"] = true;
            File.WriteAllText(activePath, latest.ToString());
            typeof(MaaProcessor).GetField("_stopCompletionMessageHandled", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(processor, 1);
            shown.Clear();
            manager.OnDialogShown += handler;
            try
            {
                var success = Confirm(new(true, true, false), MaaJobStatus.Succeeded);
                hook.Invoke(processor, [MFATask.MFATaskStatus.STOPPED, success, false, null, false, active]);
                Dispatcher.UIThread.RunJobs();
                require(!processor.HasProjectExecution && active.IsClosed && !File.Exists(activePath)
                        && shown.Count == 1 && shown[0].Content?.ToString()?.Contains("累计失败：2 次") == true,
                    "后续真实成功回执才释放并展示最新两次失败，不能消费先前旧值");
                hook.Invoke(processor, [MFATask.MFATaskStatus.STOPPED, success, false, null, false, active]);
                Dispatcher.UIThread.RunJobs();
                require(shown.Count == 1, "同一已确认终态重复回调不能重复弹窗");
                shown[0].ActionButtons.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            }
            finally { manager.OnDialogShown -= handler; }
            require((bool)reserve.Invoke(processor, null)!, "旧终态完成后允许新任务预留自己的作用域");
            var reserved = field.GetValue(processor);
            stopException.Invoke(processor, [new InvalidOperationException("迟到旧异常"), active]);
            require(processor.HasProjectExecution && ReferenceEquals(field.GetValue(processor), reserved),
                "迟到旧异常不能释放新 Reserve 的租约或消费其统计");
            typeof(MaaProcessor).GetMethod("ReleaseProjectExecution", BindingFlags.Instance | BindingFlags.NonPublic)!.Invoke(processor, null);

            // 已返回的 worker 加真实已初始化空闲状态，是停止异常之外的独立正向终止证明。
            var idle = new MaaPjskFailureSummary(root);
            idle.MarkTaskSubmitted();
            idle.MarkExecutionReturned();
            field.SetValue(processor, idle);
            execution.SetValue(processor, MaaPjskMaintenance.BeginExecution());
            typeof(MaaProcessor).GetField("_pjskPending", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(processor, false);
            observation.SetValue(processor, (Func<MaaPjskTaskerObservation>)(() => new(true, false, false)));
            stopException.Invoke(processor, [new InvalidOperationException("已有真实空闲证明"), idle]);
            require(!processor.HasProjectExecution && idle.IsClosed, "独立正向终止证明允许释放并恰好完结原执行");

            // 未初始化的真实队列令连接任务构造失败，覆盖进入 ExecuteTasks 之前的 worker finally。
            var startup = (MaaProcessor)RuntimeHelpers.GetUninitializedObject(typeof(MaaProcessor));
            instanceId.SetValue(startup, "failure-retry-startup-offscreen");
            require((bool)reserve.Invoke(startup, null)!, "初始化异常回归先预留真实执行身份");
            var startupSummary = (MaaPjskFailureSummary)field.GetValue(startup)!;
            wait(startup.StartTask([]));
            require(!startup.HasProjectExecution && startupSummary.ExecutionReturned && !startupSummary.TaskSubmitted
                    && startupSummary.IsClosed && !(bool)typeof(MaaProcessor).GetField("_pjskPending", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(startup)!,
                "提交前初始化异常必须清掉 pending，并以 worker 返回和从未提交的正向证据安全完结");
            File.WriteAllText(Path.Combine(root, "config", "performance-settings.json"), "{\"play_failure_retry_count\":0}");
            var disabled = new MaaPjskFailureSummary(root);
            var disabledId = Seed(disabled);
            require(disabled.Claim("FAILED").Count == 0, "默认关闭时保持旧退出与通知行为");
            require(!File.Exists(Path.Combine(root, "config", "play-failure-summaries", disabledId + ".json")),
                "默认关闭也必须回收临时状态，不能积累运行记录");
        }
        finally { Directory.Delete(root, recursive: true); }
    }
}
