using Avalonia.Controls;
using Avalonia.Interactivity;
using Avalonia.Threading;
using MFAAvalonia.Helper;
using SukiUI.Dialogs;
using System.Diagnostics;
using System.Reflection;
using System.Runtime.CompilerServices;
using MFAAvalonia.Extensions.MaaFW;
using MFAAvalonia.Helper.ValueType;

internal static partial class Program
{
    private static int FirstSyncProbe(string python, string script, string outputRoot)
    {
        var root = Path.GetFullPath(outputRoot);
        Directory.CreateDirectory(Path.Combine(AppContext.BaseDirectory, "config"));
        File.WriteAllText(Path.Combine(AppContext.BaseDirectory, "config", "chart-sync.json"), new Newtonsoft.Json.Linq.JObject
        {
            ["child_exec"] = Path.GetFullPath(python), ["script_path"] = Path.GetFullPath(script),
            ["working_directory"] = Path.GetDirectoryName(Path.GetFullPath(script)),
            ["output_root"] = root, ["manifest_path"] = Path.Combine(root, "manifest.json")
        }.ToString());
        MaaPjskFirstLiveSync.Model.PropertyChanged += (_, e) =>
        {
            if (e.PropertyName == "StatusText") Console.WriteLine(MaaPjskFirstLiveSync.Model.StatusText);
        };
        using var execution = MaaPjskMaintenance.BeginExecution();
        Wait(MaaPjskFirstLiveSync.PrepareAsync(CancellationToken.None), 46 * 60);
        Require(MaaPjskFirstLiveSync.IsPrepared && !MaaPjskMaintenance.IsBusy, "正式同步协调器必须确认结果后清理");
        Console.WriteLine("FIRST_SYNC_PROBE_COMPLETE: " + MaaPjskFirstLiveSync.Model.CatalogText);
        return 0;
    }

    private static void CheckFirstLiveSync(string python, string fixture, string modePath,
        List<ISukiDialog> dialogs, ISukiDialogManager manager)
    {
        string[] entries = ["AutoLive", "SoloChartLive", "CooperativeChartLive", "OneShotChartLive", "SoloChartCalibration"];
        Require(entries.All(entry => MaaPjskFirstLiveSync.NeedsSync([entry])), "五个演出入口均须首次同步");
        Require(!MaaPjskFirstLiveSync.NeedsSync(["AdRewards", "Connection", "CheckUpdate", null])
            && !MaaPjskFirstLiveSync.NeedsSync([]), "广告、连接、更新和空队列不触发首次同步");
        Require(!MaaPjskFirstLiveSync.IsPrepared, "手动完整同步和近期检查不替代首次自动同步");
        var script = Path.Combine(fixture, "automatic-sync.py");
        File.WriteAllText(script, """
import pathlib,sys,json,time
root=pathlib.Path(sys.argv[sys.argv.index('--output-root')+1])
mode=(root/'mode.txt').read_text()
with (root/'automatic-count.txt').open('a') as out: out.write('sync\n')
print('automatic fixture ready',flush=True)
if mode=='slow': time.sleep(30)
if mode=='fatal': sys.exit(1)
summary={key:0 for key in ['indexed_songs','stored_songs','songs_with_charts','charts','jackets','skipped_unpublished','added','updated','reused','unchanged','downloaded','downloaded_bytes','stale','recoverable_errors','fatal_errors']}
summary.update(indexed_songs=1,stored_songs=1,songs_with_charts=1,charts=1,jackets=1,reused=2,recoverable_errors=1 if mode=='partial' else 0)
manifest={'schema_version':1,'game':'project-sekai','server':'jp','scope':{'song_ids':None,'difficulties':['easy','normal','hard','expert','master','append']},'sync':{'mode':'incremental'},'songs':[{'charts':{'expert':{}},'jacket':{},'errors':[{}] if mode=='partial' else []}],'summary':dict(summary)}
if mode=='badsummary': manifest['summary']['charts']=5
if mode=='badsongs': manifest['songs']=[]
if mode=='badscope': manifest['scope']['difficulties']=['easy']*6
(root/'manifest.json').write_text(json.dumps(manifest))
token=sys.argv[sys.argv.index('--result-token')+1]
if mode!='noresult': print('MAAPJSK_SYNC_RESULT='+json.dumps({'token':'wrong' if mode=='wrongtoken' else token,'exit_code':2 if mode=='partial' else 0,'summary':summary}),flush=True)
sys.exit(2 if mode=='partial' else 0)
""");
        var configPath = Path.Combine(AppContext.BaseDirectory, "config", "chart-sync.json");
        var config = Newtonsoft.Json.Linq.JObject.Parse(File.ReadAllText(configPath));
        config["script_path"] = script;
        File.WriteAllText(configPath, config.ToString());
        void Dismiss()
        {
            foreach (var dialog in dialogs.ToArray())
                dialog.ActionButtons.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            Dispatcher.UIThread.RunJobs();
            dialogs.Clear();
        }
        foreach (var mode in new[] { "fatal", "noresult", "wrongtoken", "badsummary", "badsongs", "badscope" })
        {
            File.WriteAllText(modePath, mode);
            using var execution = MaaPjskMaintenance.BeginExecution();
            var operation = MaaPjskFirstLiveSync.PrepareAsync(CancellationToken.None);
            try { Wait(operation); throw new InvalidOperationException("无效结果意外通过"); }
            catch (InvalidOperationException) when (operation.IsFaulted) { }
            Require(!MaaPjskFirstLiveSync.IsPrepared && !MaaPjskMaintenance.IsBusy,
                "fatal、缺结果及错误token须清理后重试");
            Dismiss();
        }
        File.WriteAllText(modePath, "slow");
        var processor = (MaaProcessor)RuntimeHelpers.GetUninitializedObject(typeof(MaaProcessor));
        var reserve = typeof(MaaProcessor).GetMethod("ReserveProjectExecution", BindingFlags.NonPublic | BindingFlags.Instance)!;
        var release = typeof(MaaProcessor).GetMethod("ReleaseProjectExecution", BindingFlags.NonPublic | BindingFlags.Instance)!;
        var stop = typeof(MaaProcessor).GetMethod("StopInternal", BindingFlags.NonPublic | BindingFlags.Instance)!;
        Require((bool)reserve.Invoke(processor, null)!, "最早pending阶段预留启动和取消源");
        stop.Invoke(processor, [MFATask.MFATaskStatus.STOPPED, false, false, null]);
        Require(processor.CancellationTokenSource!.IsCancellationRequested && processor.HasProjectExecution,
            "StartTask初始化前Stop必须取消本次CTS并保持租约，不能替换为新CTS继续");
        typeof(MaaProcessor).GetField("_pjskStarting", BindingFlags.NonPublic | BindingFlags.Instance)!.SetValue(processor, 1);
        var retainedCancellation = processor.CancellationTokenSource;
        Wait(processor.StartTask([]));
        Require(ReferenceEquals(retainedCancellation, processor.CancellationTokenSource) && processor.HasProjectExecution,
            "旧停止回调尚未释放时，第二次public StartTask必须拒绝，不能复用旧租约或取消源");
        release.Invoke(processor, null);
        Require((bool)reserve.Invoke(processor, null)!, "pending停止清理后可重试");
        try
        {
            var operation = MaaPjskFirstLiveSync.PrepareAsync(processor.CancellationTokenSource!.Token);
            var deadline = DateTime.UtcNow.AddSeconds(5);
            while ((!MaaPjskFirstLiveSync.Model.IsSyncing || !MaaPjskFirstLiveSync.Model.ProgressText.Contains("automatic fixture")) && DateTime.UtcNow < deadline)
            { Dispatcher.UIThread.RunJobs(); Thread.Sleep(10); }
            Require(MaaPjskMaintenance.IsBusy && MaaPjskMaintenance.HasExecuting
                && !MaaPjskMaintenance.TryBeginPreparation(null) && !MaaPjskMaintenance.TryBegin(),
                "前置同步独占维护，取消前保留启动租约");
            stop.Invoke(processor, [MFATask.MFATaskStatus.STOPPED, false, false, null]);
            Require(processor.HasProjectExecution && MaaPjskMaintenance.IsBusy,
                "同步中Stop发出后，child未退出时不得提前释放启动或维护保护");
            try { Wait(operation); } catch (OperationCanceledException) { }
            Require(!MaaPjskMaintenance.IsBusy && MaaPjskMaintenance.HasExecuting
                && !MaaPjskFirstLiveSync.IsPrepared, "child退出后才解除维护，启动租约仍由外层收尾");
            Dismiss();
        }
        finally { release.Invoke(processor, null); }
        // 定时取消使用共享取消源，独立于用户Stop，必须报告超时且下次可重试。
        using (var execution = MaaPjskMaintenance.BeginExecution())
        {
            var operation = MaaPjskFirstLiveSync.PrepareAsync(CancellationToken.None);
            var deadline = DateTime.UtcNow.AddSeconds(5);
            var cancellationField = typeof(MFAAvalonia.ViewModels.UsersControls.Settings.ChartCatalogSettingsUserControlModel)
                .GetField("_cancellation", BindingFlags.NonPublic | BindingFlags.Instance)!;
            while ((!MaaPjskFirstLiveSync.Model.IsSyncing || cancellationField.GetValue(MaaPjskFirstLiveSync.Model) is null)
                && DateTime.UtcNow < deadline) { Dispatcher.UIThread.RunJobs(); Thread.Sleep(10); }
            var cancellation = (CancellationTokenSource?)cancellationField.GetValue(MaaPjskFirstLiveSync.Model);
            cancellation!.Cancel();
            try { Wait(operation); } catch (InvalidOperationException) when (operation.IsFaulted) { }
            Require(MaaPjskFirstLiveSync.Model.StatusText.Contains("超过 45 分钟") && !MaaPjskFirstLiveSync.IsPrepared,
                "超时不得启动演出或消耗首次同步状态");
            Dismiss();
        }
        File.WriteAllText(modePath, "partial");
        var existing = manager.CreateDialog().WithTitle("自动同步前已有提示").WithActionButton("确定", _ => { }, true);
        existing.TryShow(); dialogs.Clear();
        using (var execution = MaaPjskMaintenance.BeginExecution()) Wait(MaaPjskFirstLiveSync.PrepareAsync(CancellationToken.None));
        Require(MaaPjskFirstLiveSync.IsPrepared && entries.All(entry => !MaaPjskFirstLiveSync.NeedsSync([entry])),
            "partial完整索引允许继续，同进程后续演出不重复同步，已有弹窗不阻塞");
        var warningShown = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        SukiDialogManagerEventHandler onWarningShown = (_, args) =>
        {
            if (args.Dialog.Title == "谱面同步部分失败") warningShown.TrySetResult();
        };
        manager.OnDialogShown += onWarningShown;
        try
        {
            existing.Dialog.ActionButtons.OfType<Button>().Single().RaiseEvent(new RoutedEventArgs(Button.ClickEvent));
            // 异步续体未必赶上一次 RunJobs；等待实际目标事件，并沿既有有界等待持续处理 UI 队列。
            Wait(warningShown.Task);
        }
        finally { manager.OnDialogShown -= onWarningShown; }
        Require(dialogs.Count == 1 && dialogs[0].Title == "谱面同步部分失败", "自动同步警告须在已有弹窗关闭后保留显示");
        Dismiss();
        var fresh = Process.Start(new ProcessStartInfo(Environment.ProcessPath!) { UseShellExecute = false,
            CreateNoWindow = true, ArgumentList = { "--first-sync-state" } })!;
        fresh.WaitForExit(); Require(fresh.ExitCode == 0, "新进程重置首次同步状态");
    }
}
