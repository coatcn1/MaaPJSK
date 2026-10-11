param([Parameter(Mandatory)][string]$SourceRoot, [Parameter(Mandatory)][string]$ProjectRoot)
$ErrorActionPreference = 'Stop'
foreach ($name in @('MaaPjskMaintenance.cs', 'MaaPjskDiagnostics.cs', 'MaaPjskCacheMaintenance.cs')) {
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
    public bool HasProjectExecution => _pjskExecution != null;
    private bool ReserveProjectExecution()
    {
        lock (MaaPjskMaintenance.Sync)
        {
            if (_isClosed || _pjskExecution != null || MaaPjskMaintenance.IsBusy) return false;
            _pjskExecution = MaaPjskMaintenance.BeginExecution();
            return true;
        }
    }
    private void ReleaseProjectExecution() => Interlocked.Exchange(ref _pjskExecution, null)?.Dispose();
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
private void HandleStopResult(MFATask.MFATaskStatus status, MaaJobStatus success, bool onlyStart, Action? action = null, bool isUpdateRelated = false)
    {
        ReleaseProjectExecution();
'@
Replace-Once $processor 'private void HandleStopException\(Exception ex\)\s*\{' @'
private void HandleStopException(Exception ex)
    {
        ReleaseProjectExecution();
'@
Replace-Once $processor '(LoggerHelper.Info\("命令队列已停止，已忽略本次请求。"\);)' '$1 ReleaseProjectExecution();'
Replace-Once $processor 'public async Task StartTask\(List<DragItemViewModel>\? tasks, bool onlyStart = false, bool checkUpdate = false\)\s*\{' @'
public async Task StartTask(List<DragItemViewModel>? tasks, bool onlyStart = false, bool checkUpdate = false)
    {
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
            Status = MFATask.MFATaskStatus.FAILED;
            LoggerHelper.Error(error);
        }
        finally
        {
            // 初始化失败或任务取消同样经过原停止与释放流程，租约直到停止回调结束才释放。
            Stop(Status, true, onlyStart);
        }
    }
'@
Replace-Once $processor '(await Task.Run\(async \(\) => await ExecuteTasks\(token\)\);\s*\})\s*catch \(Exception error\)' '$1 catch (OperationCanceledException) { Status = MFATask.MFATaskStatus.STOPPED; } catch (Exception error)'
Replace-Once $processor '(if \(!ShouldProcessStop\(finished\)\)\s*\{\s*ToastHelper.Warn\(LangKeys.NoTaskToStop.ToLocalization\(\)\);\s*TaskQueue.Clear\(\);)' '$1 ReleaseProjectExecution();'
Replace-Once $processor 'EnqueueCommand\(\(\) => StartInternal\(null, onlyStart, checkUpdate\)\);' 'try { EnqueueCommand(() => StartInternal(null, onlyStart, checkUpdate)); } catch { ReleaseProjectExecution(); throw; }'
Replace-Once $processor 'EnqueueCommand\(\(\) => StartInternal\(dragItemViewModels, onlyStart, checkUpdate, ignoreCheckedState\)\);' 'try { EnqueueCommand(() => StartInternal(dragItemViewModels, onlyStart, checkUpdate, ignoreCheckedState)); } catch { ReleaseProjectExecution(); throw; }'
$tabs = Join-Path $SourceRoot 'MFAAvalonia\ViewModels\Other\InstanceTabBarViewModel.cs'
Replace-Once $tabs '(private async Task CloseInstance\(InstanceTabViewModel\? tab\)\s*\{\s*if \(tab == null\) return;)' @'
$1
        if (tab.Processor.HasProjectExecution)
        {
            ToastHelper.Warn("实例仍在执行", "请先停止任务并等待释放完成，再关闭实例。");
            return;
        }
'@
$manager = Join-Path $SourceRoot 'MFAAvalonia\Extensions\MaaFW\MaaProcessorManager.cs'
Replace-Once $manager '(public bool RemoveInstance\(string instanceId\)\s*\{)' '$1 using var projectGate = MaaPjskMaintenance.EnterTaskStart();'
Replace-Once $manager '(public bool RemoveInstance\(string instanceId\)[\s\S]*?if \(_instances.TryGetValue\(instanceId, out var processor\)\)\s*\{)' '$1 if (processor.HasProjectExecution) return false;'
