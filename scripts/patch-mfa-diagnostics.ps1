param([Parameter(Mandatory)][string]$SourceRoot, [Parameter(Mandatory)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
foreach ($name in @('MaaPjskMaintenance.cs', 'MaaPjskDiagnostics.cs', 'MaaPjskCacheMaintenance.cs', 'MaaPjskFirstLiveSync.cs', 'MaaPjskFailureSummary.cs')) {
    Copy-Item -LiteralPath (Join-Path $ProjectRoot "mfa-chart-ui\$name") -Destination (Join-Path $SourceRoot 'MFAAvalonia\Helper') -Force
}
function Replace-Once([string]$path, [string]$pattern, [string]$replacement) {
    $source = [IO.File]::ReadAllText($path)
    if ([regex]::Matches($source, $pattern).Count -ne 1) { throw "固定 MFA 诊断入口不匹配：$pattern" }
    [IO.File]::WriteAllText($path, [regex]::Replace($source, $pattern, $replacement), [Text.UTF8Encoding]::new($false))
}
$logger = Join-Path $SourceRoot 'MFAAvalonia\Helper\LoggerHelper.cs'
Replace-Once $logger 'public static void InitializeLogger\(\)\s*\{' @'
public static void InitializeLogger()
    {
        lock (MaaPjskDiagnostics.LoggerSync)
        {
        if (MaaPjskDiagnostics.Disabled) { _logCache.Clear(); return; }
'@
Replace-Once $logger 'FlushCache\(\);\s*\}' 'FlushCache(); } }'
Replace-Once $logger 'public static void DisposeLogger\(\)\s*\{' 'public static void DisposeLogger() { lock (MaaPjskDiagnostics.LoggerSync) {'
Replace-Once $logger '_logger = null;\s*\}' '_logger = null; } }'
Replace-Once $logger 'private static void Write\(LogEventLevel level, object\? message, Exception\? exception = null\)\s*\{' @'
private static void Write(LogEventLevel level, object? message, Exception? exception = null)
    {
        lock (MaaPjskDiagnostics.LoggerSync)
        {
        if (MaaPjskDiagnostics.Disabled) { _logCache.Clear(); return; }
'@
Replace-Once $logger '_logger.Write\(level, exception, formattedMessage\);\s*\}' '_logger.Write(level, exception, formattedMessage); } }'
$processor = Join-Path $SourceRoot 'MFAAvalonia\Extensions\MaaFW\MaaProcessor.cs'
Replace-Once $processor 'tasker.Global.SetOption_SaveDraw\(ConfigurationManager.Maa.GetValue\(ConfigurationKeys.SaveDraw, false\)\);\s*tasker.Global.SetOption\(GlobalOption.SaveOnError, ConfigurationManager.Maa.GetValue\(ConfigurationKeys.SaveOnError, true\)\);' 'MaaPjskDiagnostics.ApplyFramework();'
$about = Join-Path $SourceRoot 'MFAAvalonia\Views\UserControls\Settings\AboutUserControl.axaml.cs'
Replace-Once $about 'private void ClearCache_Click\(object\? sender, RoutedEventArgs e\)\s*\{' @'
private void ClearCache_Click(object? sender, RoutedEventArgs e)
    {
        if (!MaaPjskMaintenance.TryBegin())
        {
            ToastHelper.Warn("暂不能清理", "任务、谱面同步或更新仍在执行，请等待停止与释放完成。");
            return;
        }
        try { ClearProjectCache(); }
        finally { MaaPjskMaintenance.End(); }
    }
    private void ClearProjectCache()
    {
'@
Replace-Once $about 'ClearDirectory\(debugDirectory\);\s*ClearDirectory\(logsDirectory\);\s*Directory.CreateDirectory\(debugDirectory\);\s*Directory.CreateDirectory\(logsDirectory\);\s*ToastHelper.Success\(LangKeys.ClearCacheSuccess.ToLocalization\(\)\);' @'
var result = MaaPjskCacheMaintenance.Clear(baseDirectory);
            var summary = $"已清理 {result.Files} 个文件，释放 {result.Bytes / 1048576.0:F2} MiB；保留成绩 CSV、配置与谱面。";
            if (result.Errors.Count == 0) ToastHelper.Success(summary);
            else ToastHelper.Warn("部分缓存未清理", summary + $" 失败 {result.Errors.Count} 项：" + string.Join("；", result.Errors.Take(3)));
'@

$gameSettings = Join-Path $SourceRoot 'MFAAvalonia\ViewModels\UsersControls\Settings\GameSettingsUserControlModel.cs'
Replace-Once $gameSettings 'MaaProcessor.Global.SetOption_SaveDraw\(value\);' 'MaaProcessor.Global.SetOption_SaveDraw(!MaaPjskDiagnostics.Disabled && value);'
Replace-Once $gameSettings 'MaaProcessor.Global.SetOption\(GlobalOption.SaveOnError, value\);' 'MaaProcessor.Global.SetOption(GlobalOption.SaveOnError, !MaaPjskDiagnostics.Disabled && value);'

Replace-Once $processor 'public static MaaToolkit Toolkit \{ get; \} = new\(true\);\s*public static MaaGlobal Global \{ get; \} = new\(\);' @'
public static MaaGlobal Global { get; } = MaaPjskDiagnostics.CreateGlobal();
    public static MaaToolkit Toolkit { get; } = new(!MaaPjskDiagnostics.Disabled);
    private IDisposable? _pjskExecution;
    private MaaPjskFailureSummary? _pjskFailureSummary;
    private Func<MaaPjskTaskerObservation>? _pjskTaskerObservation = null;
    private volatile bool _pjskPreparing;
    private volatile bool _pjskPending;
    private int _pjskStarting;
    public bool HasProjectExecution => _pjskExecution != null;
    private bool ReserveProjectExecution()
    {
        lock (MaaPjskMaintenance.Sync)
        {
            if (_isClosed || _pjskExecution != null || MaaPjskMaintenance.IsBusy
                || (!MaaPjskFirstLiveSync.IsPrepared && MaaPjskMaintenance.HasExecuting))
            {
                ToastHelper.Warn("任务暂不能启动", "另一个启动、谱面同步或停止清理仍在进行，请等待完成后再次点击开始。");
                return false;
            }
            _pjskExecution = MaaPjskMaintenance.BeginExecution();
            _pjskFailureSummary = new MaaPjskFailureSummary();
            CancellationTokenSource = new CancellationTokenSource();
            _pjskPending = true;
            return true;
        }
    }
    private void ReleaseProjectExecution()
    {
        lock (MaaPjskMaintenance.Sync)
        {
            _pjskPending = false;
            Interlocked.Exchange(ref _pjskStarting, 0);
            Interlocked.Exchange(ref _pjskExecution, null)?.Dispose();
        }
    }
    private MaaPjskTaskerObservation ObserveProjectTasker()
    {
        try
        {
            if (_pjskTaskerObservation != null) return _pjskTaskerObservation();
            var tasker = MaaTasker;
            if (tasker == null || !tasker.IsInitialized) return default;
            return new(true, tasker.IsRunning, tasker.IsStopping);
        }
        catch { return default; }
    }
    private bool ProjectExecutionTerminated(MaaPjskFailureSummary? summary)
    {
        if (summary == null || !summary.ExecutionReturned || _pjskPending || _pjskPreparing) return false;
        // 没有提交过任务且 worker 已返回，或真实已初始化执行器明确空闲，才能作为独立终止证明。
        if (!summary.TaskSubmitted) return true;
        var observed = ObserveProjectTasker();
        return observed.Initialized && !observed.Running && !observed.Stopping;
    }
    private void KeepProjectStopPending(MaaPjskFailureSummary? summary)
    {
        if (!ReferenceEquals(_pjskFailureSummary, summary)) return;
        summary?.EndStop();
        Status = MFATask.MFATaskStatus.NOT_STARTED;
        ToastHelper.Warn("停止尚未确认", "任务尚未完全停止，正在等待输入释放；请再次点击停止，完成前无法开始新任务。");
        DispatcherHelper.PostOnMainThread(() =>
        {
            if (ViewModel != null) { ViewModel.ToggleEnable = true; ViewModel.IsRunning = HasProjectExecution; }
            if (HasProjectExecution) { Instances.RootViewModel.Idle = false; Instances.RootViewModel.IsRunning = true; }
        });
    }
    private async Task<MaaJobStatus> ConfirmProjectStopAsync(MaaPjskFailureSummary? summary,
        Func<MaaJobStatus> abort, int attempts, int delayMilliseconds)
    {
        var result = MaaJobStatus.Invalid;
        for (var index = 0; index < attempts; index++)
        {
            if (!ReferenceEquals(_pjskFailureSummary, summary)) return MaaJobStatus.Invalid;
            if (ProjectExecutionTerminated(summary)) return MaaJobStatus.Succeeded;
            var observed = ObserveProjectTasker();
            if (observed.Initialized && !observed.Stopping)
            {
                result = abort();
                if (result == MaaJobStatus.Succeeded) return result;
            }
            if (ProjectExecutionTerminated(summary)) return MaaJobStatus.Succeeded;
            if (index + 1 < attempts) await Task.Delay(delayMilliseconds);
        }
        return result;
    }
    private Task StopProjectExecution(MFATask.MFATaskStatus status, bool finished, bool onlyStart,
        Action? action, MaaPjskFailureSummary? summary)
    {
        lock (MaaPjskMaintenance.Sync)
        {
            if (!ReferenceEquals(_pjskFailureSummary, summary) || (summary?.IsClosed == true && !HasProjectExecution))
                return Task.CompletedTask;
            return StopInternal(status, finished, onlyStart, action);
        }
    }
'@
Replace-Once $processor 'public void Start\(bool onlyStart = false, bool checkUpdate = false\)\s*\{' @'
public void Start(bool onlyStart = false, bool checkUpdate = false)
    {
        if (!ReserveProjectExecution()) return;
'@
Replace-Once $processor 'public void Start\(List<DragItemViewModel> dragItemViewModels, bool onlyStart = false, bool checkUpdate = false, bool ignoreCheckedState = false\)\s*\{' @'
public void Start(List<DragItemViewModel> dragItemViewModels, bool onlyStart = false, bool checkUpdate = false, bool ignoreCheckedState = false)
    {
        if (!ReserveProjectExecution()) return;
'@
Replace-Once $processor 'private Task StartInternal\(List<DragItemViewModel>\? dragItemViewModels, bool onlyStart, bool checkUpdate, bool ignoreCheckedState = false\)\s*\{' @'
private Task StartInternal(List<DragItemViewModel>? dragItemViewModels, bool onlyStart, bool checkUpdate, bool ignoreCheckedState = false)
    {
        var transferred = false;
        try
        {
'@
Replace-Once $processor '_ = StartTask\(tasks, onlyStart, checkUpdate\);' '_ = StartTask(tasks, onlyStart, checkUpdate); transferred = true;'
Replace-Once $processor '(return Task.CompletedTask;\s*\})(\s*private List<DragItemViewModel> FilterExecutableTasks)' '$1 finally { if (!transferred) ReleaseProjectExecution(); } } $2'
Replace-Once $processor 'private void HandleStopResult\(MFATask.MFATaskStatus status, MaaJobStatus success, bool onlyStart, Action\? action = null, bool isUpdateRelated = false\)\s*\{' @'
private void HandleStopResult(MFATask.MFATaskStatus status, MaaJobStatus success, bool onlyStart, Action? action = null, bool isUpdateRelated = false, MaaPjskFailureSummary? failureSummary = null)
    {
        lock (MaaPjskMaintenance.Sync)
        {
            // 原停止闭包持有原执行身份，迟到回调不能释放或消费新任务。
            if (!ReferenceEquals(_pjskFailureSummary, failureSummary))
            {
                return;
            }
            if (failureSummary?.IsClosed == true && !HasProjectExecution) return;
            if (success != MaaJobStatus.Succeeded && !ProjectExecutionTerminated(failureSummary))
            {
                KeepProjectStopPending(failureSummary);
                return;
            }
            success = MaaJobStatus.Succeeded;
            failureSummary?.EndStop();
            ReleaseProjectExecution();
        }
        failureSummary?.Complete(status.ToString());
'@
Replace-Once $processor 'private void HandleStopException\(Exception ex\)\s*\{' @'
private void HandleStopException(Exception ex, MaaPjskFailureSummary? failureSummary = null)
    {
        lock (MaaPjskMaintenance.Sync)
        {
            if (!ReferenceEquals(_pjskFailureSummary, failureSummary))
            {
                return;
            }
            if (failureSummary?.IsClosed == true && !HasProjectExecution) return;
            if (!ProjectExecutionTerminated(failureSummary))
            {
                LoggerHelper.Error($"停止操作失败但未确认终止：{ex.Message}");
                KeepProjectStopPending(failureSummary);
                return;
            }
            failureSummary?.EndStop();
            ReleaseProjectExecution();
        }
        failureSummary?.Complete("failed");
'@
Replace-Once $processor 'LoggerHelper.Info\("命令队列已停止，已忽略本次请求。"\);\s*return;' 'throw new InvalidOperationException("命令队列已停止，不能确认本次执行终止。");'
Replace-Once $processor 'public async Task StartTask\(List<DragItemViewModel>\? tasks, bool onlyStart = false, bool checkUpdate = false\)\s*\{' @'
public async Task StartTask(List<DragItemViewModel>? tasks, bool onlyStart = false, bool checkUpdate = false)
    {
        lock (MaaPjskMaintenance.Sync)
        {
            if (Interlocked.CompareExchange(ref _pjskStarting, 1, 0) != 0) return;
            if (_pjskExecution == null && !ReserveProjectExecution()) { _pjskStarting = 0; return; }
        }
        var executionSummary = _pjskFailureSummary;
        try
        {
'@
Replace-Once $processor 'await TaskManager.RunTaskAsync\(async \(\) =>\s*\{\s*await ExecuteTasks\(token\);\s*Stop\(Status, true, onlyStart\);\s*\}, token: token, name: "启动任务"\);' @'
await Task.Run(async () => await ExecuteTasks(token));
'@
Replace-Once $processor '(await Task.Run\(async \(\) => await ExecuteTasks\(token\)\);)\s*\}' @'
$1
        }
        catch (Exception error)
        {
            if (ReferenceEquals(_pjskFailureSummary, executionSummary)) Status = MFATask.MFATaskStatus.FAILED;
            LoggerHelper.Error(error);
        }
        finally
        {
            // 初始化失败或任务取消同样经过原停止与释放流程，租约直到停止回调结束才释放。
            lock (MaaPjskMaintenance.Sync)
                if (ReferenceEquals(_pjskFailureSummary, executionSummary)) { _pjskPending = false; _pjskPreparing = false; }
            executionSummary?.MarkExecutionReturned();
            var completedStatus = Status;
            try { EnqueueCommand(() => StopProjectExecution(completedStatus, true, onlyStart, null, executionSummary)); }
            catch (Exception stopError) { HandleStopException(stopError, executionSummary); }
        }
    }
'@
Replace-Once $processor '(await Task.Run\(async \(\) => await ExecuteTasks\(token\)\);\s*\})\s*catch \(Exception error\)' '$1 catch (OperationCanceledException) { if (ReferenceEquals(_pjskFailureSummary, executionSummary)) Status = MFATask.MFATaskStatus.STOPPED; } catch (Exception error)'
Replace-Once $processor 'CancellationTokenSource = new CancellationTokenSource\(\);(\s*_startTime = DateTime.Now;)' '$1'
Replace-Once $processor '(await Task.Run\(async \(\) => await ExecuteTasks\(token\)\);)' 'lock (MaaPjskMaintenance.Sync) _pjskPending = false; $1'
Replace-Once $processor '(var taskAndParams = tasks.Select\(\(task, index\) => CreateNodeAndParam\(task, index \+ 1\)\).ToList\(\);)' @'
$1
            if (MaaPjskFirstLiveSync.NeedsSync(tasks.Select(task => task.InterfaceItem?.Entry)))
            {
                _pjskPreparing = true;
                TaskQueue.Enqueue(CreateMFATask("首次演出前更新谱面", async () =>
                {
                    DispatcherHelper.PostOnMainThread(() =>
                    {
                        ViewModel?.SetCurrentTaskName("首次演出前更新谱面");
                        AddLog("正在更新日服谱面，完成后继续演出；可点击停止取消。", (IBrush?)null);
                    });
                    try { await MaaPjskFirstLiveSync.PrepareAsync(token, this); }
                    finally { _pjskPreparing = false; }
                    token.ThrowIfCancellationRequested();
                }));
            }
'@
Replace-Once $processor 'EnqueueCommand\(\(\) => StopInternal\(status, finished, onlyStart, action\)\);' @'
var summary = _pjskFailureSummary;
        try { EnqueueCommand(() => StopProjectExecution(status, finished, onlyStart, action, summary)); }
        catch (Exception error) { HandleStopException(error, summary); }
'@
Replace-Once $processor 'private Task StopInternal\(MFATask.MFATaskStatus status, bool finished, bool onlyStart, Action\? action\)[\s\S]*?(?=    private void CancelOperations)' @'
private Task StopInternal(MFATask.MFATaskStatus status, bool finished, bool onlyStart, Action? action)
    {
        var failureSummary = _pjskFailureSummary;
        // 同步子进程退出前保留租约，由原 worker 返回后统一确认终态。
        lock (MaaPjskMaintenance.Sync)
        {
            if ((_pjskPending || _pjskPreparing) && !finished)
            {
                CancellationTokenSource?.Cancel();
                return Task.CompletedTask;
            }
        }
        using var logScope = BeginInstanceLogScope("StopTask", "Worker");
        ResetActionFailedCount();
        ClearTaskbarProgress();
        _taskQueueTotal = 0;
        lock (_stopLock)
        {
            if (failureSummary?.IsClosed == true && !HasProjectExecution) return Task.CompletedTask;
            if (Status == MFATask.MFATaskStatus.STOPPING && (failureSummary == null || failureSummary.StopInFlight))
                return Task.CompletedTask;
            if (failureSummary != null && !failureSummary.TryBeginStop()) return Task.CompletedTask;
            Status = MFATask.MFATaskStatus.STOPPING;
            ViewModel?.SetAdbRecoverySelectionLock(false);
            DispatcherHelper.PostOnMainThread(() => { if (ViewModel != null) ViewModel.ToggleEnable = false; });
            try
            {
                var isUpdateRelated = TaskQueue.Any(task => task.IsUpdateRelated);
                ViewModel?.SetCurrentTaskName(string.Empty);
                if (!ShouldProcessStop(finished))
                {
                    failureSummary?.EndStop();
                    ToastHelper.Warn(LangKeys.NoTaskToStop.ToLocalization());
                    return Task.CompletedTask;
                }
                CancelOperations(status == MFATask.MFATaskStatus.STOPPED && !_agentStarted && _agentContexts.Count > 0);
                TaskQueue.Clear();
                ExecuteStopCore(finished, async () =>
                {
                    // 请求状态不能冒充终止证明；必须等停止回执或独立的真实空闲证明。
                    var stopResult = await ConfirmProjectStopAsync(failureSummary, AbortCurrentTasker, 10, 500);
                    HandleStopResult(status, stopResult, onlyStart, action, isUpdateRelated, failureSummary);
                }, failureSummary);
            }
            catch (Exception ex) { HandleStopException(ex, failureSummary); }
        }
        return Task.CompletedTask;
    }


'@
Replace-Once $processor 'return CancellationTokenSource\?\.IsCancellationRequested == false\s*\|\| finished;' 'return HasProjectExecution || CancellationTokenSource?.IsCancellationRequested == false || finished;'
Replace-Once $processor 'private void ExecuteStopCore\(bool finished, Action stopAction\)[\s\S]*?(?=    private MaaJobStatus AbortCurrentTasker)' @'
private void ExecuteStopCore(bool finished, Func<Task> stopAction, MaaPjskFailureSummary? failureSummary)
    {
        TaskManager.RunTaskAsync(async () =>
        {
            try
            {
                if (!finished) DispatcherHelper.PostOnMainThread(() => AddLogByKey(LangKeys.Stopping, (IBrush?)null));
                await stopAction();
            }
            catch (Exception ex) { HandleStopException(ex, failureSummary); }
            finally
            {
                failureSummary?.EndStop();
                DispatcherHelper.PostOnMainThread(() =>
                {
                    if (ViewModel != null) { ViewModel.ToggleEnable = true; ViewModel.IsRunning = HasProjectExecution; }
                    Instances.RootViewModel.Idle = !MaaPjskMaintenance.HasExecuting;
                    if (HasProjectExecution) Instances.RootViewModel.IsRunning = true;
                });
            }
        }, null, "停止maafw任务");
    }

'@
Replace-Once $processor '(private MaaJobStatus AbortCurrentTasker\(\)\s*\{\s*if \(MaaTasker == null\)\s*return )MaaJobStatus.Succeeded;' '$1MaaJobStatus.Invalid;'
Replace-Once $processor '(foreach \(var task in taskAndParams\)\s*\{)' '$1 var failureSummary = _pjskFailureSummary;'
Replace-Once $processor 'TryRunTasksAsync\(MaaTasker, task.Entry, task.Param, token\);' 'TryRunTasksAsync(MaaTasker, task.Entry, task.Param, token, failureSummary);'
Replace-Once $processor 'async private Task TryRunTasksAsync\(MaaTasker\? maa, string\? task, string\? param, CancellationToken token\)' 'async private Task TryRunTasksAsync(MaaTasker? maa, string? task, string? param, CancellationToken token, MaaPjskFailureSummary? failureSummary)'
Replace-Once $processor '(var job = maa.AppendTask\(task, param \?\? "\{\}"\);)' @'
token.ThrowIfCancellationRequested();
        if (task is "SoloChartLive" or "CooperativeChartLive")
        {
            if (failureSummary is null) throw new InvalidOperationException("谱面任务缺少本次执行身份，拒绝开始。");
            param = failureSummary.Prepare(task, param);
        }
        token.ThrowIfCancellationRequested();
        failureSummary?.MarkTaskSubmitted();
        $1
'@
Replace-Once $processor 'EnqueueCommand\(\(\) => StartInternal\(null, onlyStart, checkUpdate\)\);' 'try { EnqueueCommand(() => StartInternal(null, onlyStart, checkUpdate)); } catch { ReleaseProjectExecution(); throw; }'
Replace-Once $processor 'EnqueueCommand\(\(\) => StartInternal\(dragItemViewModels, onlyStart, checkUpdate, ignoreCheckedState\)\);' 'try { EnqueueCommand(() => StartInternal(dragItemViewModels, onlyStart, checkUpdate, ignoreCheckedState)); } catch { ReleaseProjectExecution(); throw; }'
$tabs = Join-Path $SourceRoot 'MFAAvalonia\ViewModels\Other\InstanceTabBarViewModel.cs'
Replace-Once $tabs '(private async Task CloseInstance\(InstanceTabViewModel\? tab\)\s*\{\s*if \(tab == null\) return;)' @'
$1
        if (tab.Processor.HasProjectExecution)
        {
            tab.Processor.Stop(MFAAvalonia.Helper.ValueType.MFATask.MFATaskStatus.STOPPED);
            ToastHelper.Warn("实例仍在执行", "已请求停止，请等待释放完成后再关闭实例。");
            return;
        }
'@
$manager = Join-Path $SourceRoot 'MFAAvalonia\Extensions\MaaFW\MaaProcessorManager.cs'
Replace-Once $manager '(public bool RemoveInstance\(string instanceId\)\s*\{)' '$1 using var projectGate = MaaPjskMaintenance.EnterTaskStart();'
Replace-Once $manager '(public bool RemoveInstance\(string instanceId\)[\s\S]*?if \(_instances.TryGetValue\(instanceId, out var processor\)\)\s*\{)' '$1 if (processor.HasProjectExecution) return false;'
