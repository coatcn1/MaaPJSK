using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MaaFramework.Binding;
using Newtonsoft.Json.Linq;
using System;
using System.IO;

namespace MFAAvalonia.Helper;

public static class MaaPjskDiagnostics
{
    public static readonly object LoggerSync = new();
    private static bool? _disabled;
    public static bool Disabled { get { lock (LoggerSync) return _disabled ??= ReadDisabled(); } }
    private static bool ReadDisabled()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "config", "performance-settings.json");
        try { return File.Exists(path) && JObject.Parse(File.ReadAllText(path)).Value<bool?>("disable_logging") == true; }
        catch { return false; }
    }
    public static MaaGlobal CreateGlobal()
    {
        var global = new MaaGlobal();
        if (!global.SetOption_LogDir(Disabled ? "" : Path.Combine(AppContext.BaseDirectory, "debug"))
            || !global.SetOption_SaveDraw(false)
            || !global.SetOption(GlobalOption.SaveOnError, !Disabled))
            throw new InvalidOperationException("MaaFramework 拒绝启动日志设置。");
        return global;
    }
    public static void Apply(bool disabled)
    {
        lock (LoggerSync)
        {
        var previous = Disabled;
        _disabled = disabled;
        try { ApplyFramework(); }
        catch { _disabled = previous; ApplyFramework(); throw; }
        LoggerHelper.DisposeLogger();
        LoggerHelper.InitializeLogger();
        }
    }
    public static void ApplyFramework()
    {
        var logAccepted = MaaProcessor.Global.SetOption_LogDir(Disabled ? "" : Path.Combine(AppContext.BaseDirectory, "debug"));
        var drawAccepted = MaaProcessor.Global.SetOption_SaveDraw(!Disabled && ConfigurationManager.Maa.GetValue(ConfigurationKeys.SaveDraw, false));
        var errorAccepted = MaaProcessor.Global.SetOption(GlobalOption.SaveOnError, !Disabled && ConfigurationManager.Maa.GetValue(ConfigurationKeys.SaveOnError, true));
        if (!logAccepted || !drawAccepted || !errorAccepted)
            throw new InvalidOperationException("MaaFramework 拒绝文件日志设置，请重启后重试。");
    }
}
