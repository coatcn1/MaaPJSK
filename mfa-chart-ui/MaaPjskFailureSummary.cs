using Avalonia.Controls.Notifications;
using MFAAvalonia.ViewModels.Other;
using Newtonsoft.Json.Linq;
using SukiUI.Dialogs;
using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace MFAAvalonia.Helper;

public sealed record MaaPjskFailureNotice(string Content, bool Warning);
public readonly record struct MaaPjskTaskerObservation(bool Initialized, bool Running, bool Stopping);

public sealed class MaaPjskFailureSummary
{
    private readonly object _sync = new();
    private readonly string _root;
    private readonly List<(string Id, string Mode, int Limit)> _runs = [];
    private bool _claimed;
    private bool _taskSubmitted;
    private bool _executionReturned;
    private bool _stopInFlight;
    public string ExecutionId { get; } = Guid.NewGuid().ToString("N");
    private static readonly SemaphoreSlim DialogQueue = new(1, 1);

    public MaaPjskFailureSummary(string? root = null) => _root = root ?? AppContext.BaseDirectory;
    public bool IsClosed { get { lock (_sync) return _claimed; } }
    public bool TaskSubmitted { get { lock (_sync) return _taskSubmitted; } }
    public bool ExecutionReturned { get { lock (_sync) return _executionReturned; } }
    public bool StopInFlight { get { lock (_sync) return _stopInFlight; } }
    public void MarkTaskSubmitted() { lock (_sync) _taskSubmitted = true; }
    public void MarkExecutionReturned() { lock (_sync) _executionReturned = true; }
    public bool TryBeginStop()
    {
        lock (_sync)
        {
            if (_claimed || _stopInFlight) return false;
            _stopInFlight = true;
            return true;
        }
    }
    public void EndStop() { lock (_sync) _stopInFlight = false; }
    private string PathFor(string id) => Path.Combine(_root, "config", "play-failure-summaries", id + ".json");
    private int RetryLimit()
    {
        var path = Path.Combine(_root, "config", "performance-settings.json");
        if (!File.Exists(path)) return 0;
        var value = JObject.Parse(File.ReadAllText(path))["play_failure_retry_count"];
        if (value is null) return 0;
        if (value.Type != JTokenType.Integer || value.Value<long>() is < 0 or > 99)
            throw new InvalidDataException("中途失败重试次数无效，拒绝开始任务。");
        return value.Value<int>();
    }

    public string? Prepare(string entry, string? parameters)
    {
        var mode = entry switch { "SoloChartLive" => "solo", "CooperativeChartLive" => "cooperative", _ => null };
        if (mode is null) return parameters;
        lock (_sync)
        {
            if (_claimed) throw new InvalidOperationException("本次执行已结束，不能再开始谱面演出。");
            var id = Guid.NewGuid().ToString("N");
            var limit = RetryLimit();
            var value = new JObject
            {
                ["schema_version"] = 1, ["execution_id"] = ExecutionId, ["summary_id"] = id,
                ["mode"] = mode, ["agent_attached"] = false, ["finalized"] = false,
                ["failure_count"] = 0, ["retries_used"] = 0, ["retry_limit"] = limit, ["status"] = "pending"
            };
            var param = JToken.Parse(parameters ?? "{}");
            if (param is not JObject && param is not JArray)
                throw new InvalidDataException("任务管线覆盖参数必须是对象或对象数组。");
            var node = mode == "solo" ? "SoloChartLiveRun" : "CooperativeChartLiveRun";
            JObject custom = new();
            var layers = param is JArray array ? array.ToArray() : [param];
            foreach (var layer in layers)
            {
                if (layer is not JObject item) throw new InvalidDataException("任务管线覆盖层必须是对象。");
                if (item[node] is JObject oldRun && oldRun.TryGetValue("custom_action_param", out var oldCustom))
                {
                    if (oldCustom is not JObject) throw new InvalidDataException("谱面任务参数必须是对象。");
                    // 框架按层覆盖整个参数对象，只沿用最后明确提供的对象，不能复活已被覆盖的字段。
                    custom = (JObject)oldCustom.DeepClone();
                }
            }
            custom["execution_id"] = ExecutionId;
            custom["summary_id"] = id;
            if (param is JArray layered)
                layered.Add(new JObject { [node] = new JObject { ["custom_action_param"] = custom } });
            else
            {
                var run = param[node] as JObject ?? new JObject();
                run["custom_action_param"] = custom;
                param[node] = run;
            }
            // 每次真实 AppendTask 都分配新身份，重复队列项不能覆盖前一次统计。
            var path = PathFor(id);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var temporary = path + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temporary, value.ToString(), new UTF8Encoding(false));
                File.Move(temporary, path);
            }
            finally { if (File.Exists(temporary)) File.Delete(temporary); }
            _runs.Add((id, mode, limit));
            return param.ToString();
        }
    }

    private static int Integer(JObject value, string key)
    {
        var token = value[key];
        if (token?.Type != JTokenType.Integer || token.Value<long>() is < 0 or > int.MaxValue)
            throw new InvalidDataException("任务统计字段无效");
        return token.Value<int>();
    }

    public IReadOnlyList<MaaPjskFailureNotice> Claim(string terminalStatus)
    {
        lock (_sync)
        {
            if (_claimed) return [];
            _claimed = true;
            var notices = new List<MaaPjskFailureNotice>();
            foreach (var (id, mode, configuredLimit) in _runs)
            {
                var label = mode == "solo" ? "单人谱面演出" : "协力谱面演出";
                try
                {
                    if (configuredLimit == 0) continue;
                    var value = JObject.Parse(File.ReadAllText(PathFor(id)));
                    if (value.Value<int?>("schema_version") != 1 || value.Value<string>("execution_id") != ExecutionId
                        || value.Value<string>("summary_id") != id || value.Value<string>("mode") != mode
                        || value["finalized"]?.Type != JTokenType.Boolean || value["agent_attached"]?.Type != JTokenType.Boolean)
                        throw new InvalidDataException("任务统计身份无效");
                    var failures = Integer(value, "failure_count");
                    var used = Integer(value, "retries_used");
                    var limit = Integer(value, "retry_limit");
                    if (limit != configuredLimit || used > limit || used > failures) throw new InvalidDataException("任务重试计数无效");
                    var final = value.Value<bool>("finalized");
                    if (failures == 0 && final) continue;
                    var outcome = final ? value.Value<string>("status") : terminalStatus;
                    var status = outcome?.ToLowerInvariant() switch
                    {
                        "succeeded" => "任务成功结束", "stopped" => "用户停止", _ => "任务异常终止"
                    };
                    var text = $"{label}：{status}。\n{(final ? "累计失败" : "最后确认累计失败")}：{failures} 次。\n"
                        + $"已使用重试预算 {used} / {limit}。";
                    if (value["performed_rounds"]?.Type == JTokenType.Integer && value["target_rounds"]?.Type == JTokenType.Integer)
                        text += $" 已完成演出 {Integer(value, "performed_rounds")} / {Integer(value, "target_rounds")}。";
                    if (!final) text += "\n统计尚未完整保存，实际失败次数可能更多；不能将最后确认值当作完整统计。";
                    notices.Add(new(text, !final));
                }
                catch
                {
                    notices.Add(new($"{label}已结束，但本次失败统计无法读取。累计失败次数未知，不能按零次处理。", true));
                }
                finally
                {
                    // 只回收本执行实际登记的文件；旧回调不会枚举或删除其他实例、新任务的状态。
                    try { File.Delete(PathFor(id)); }
                    catch { notices.Add(new($"{label}的临时统计状态清理失败；已取得的失败次数仍按上方记录显示。", true)); }
                }
            }
            return notices;
        }
    }

    public void Complete(string terminalStatus)
    {
        var notices = Claim(terminalStatus);
        if (notices.Count == 0) return;
        DispatcherHelper.PostOnMainThread(() => { _ = ShowAsync(notices); });
    }

    public static async Task ShowAsync(IReadOnlyList<MaaPjskFailureNotice> notices)
    {
        var token = MaaPjskFirstLiveSync.ShutdownToken;
        try
        {
            await DialogQueue.WaitAsync(token);
            try
            {
                var manager = Instances.DialogManager;
                var dialog = manager.CreateDialog().WithTitle("演出失败次数汇总")
                    .OfType(notices.Any(notice => notice.Warning) ? NotificationType.Warning : NotificationType.Information)
                    .WithContent(string.Join(Environment.NewLine + Environment.NewLine, notices.Select(notice => notice.Content)))
                    .WithActionButton("确定", _ => { }, true, "Basic");
                while (true)
                {
                    var dismissed = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
                    SukiDialogManagerEventHandler handler = (_, _) => dismissed.TrySetResult();
                    manager.OnDialogDismissed += handler;
                    try
                    {
                        if (dialog.TryShow()) return;
                        await dismissed.Task.WaitAsync(token);
                    }
                    finally { manager.OnDialogDismissed -= handler; }
                }
            }
            finally { DialogQueue.Release(); }
        }
        catch (OperationCanceledException) { }
    }
}
