using MaaFramework.Binding;

namespace MFAAvalonia.Extensions.MaaFW;

public static class MaaPjskTaskStatus
{
    public static void Check(string entry, MaaJobStatus status, bool continueRunningWhenError)
    {
        // 可恢复异常已由任务流程内部处理；传到框架的终止失败不能再被队列吞成成功。
        bool performance = entry is "AutoLive" or "SoloChartLive" or "CooperativeChartLive"
            or "OneShotChartLive" or "SoloChartCalibration" or "AdRewards";
        if (performance || !continueRunningWhenError)
            status.ThrowIfNot(MaaJobStatus.Succeeded);
    }
}
