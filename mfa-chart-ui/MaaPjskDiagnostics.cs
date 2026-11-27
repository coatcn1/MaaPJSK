using MFAAvalonia.Configuration;
using MFAAvalonia.Extensions.MaaFW;
using MaaFramework.Binding;
using Newtonsoft.Json.Linq;
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;

namespace MFAAvalonia.Helper;

public sealed record MaaPjskFrameworkStartupOptions(bool LoggingEnabled, int StdoutLevel, int DrawQuality, bool DebugMode);

public static class MaaPjskDiagnostics
{
    public const string FrameworkLoggingEnvironment = "MAAPJSK_MAAFW_LOGGING";
    public static readonly object LoggerSync = new();
    private static bool? _disabled;
    private static MaaPjskFrameworkStartupOptions? _startup;
    public static bool Disabled
    {
        get
        {
            lock (LoggerSync)
            {
                var value = _disabled;
                if (value.HasValue) return value.Value;
                var token = ReadSettings()["disable_logging"];
                return (_disabled = token?.Type == JTokenType.Boolean && token.Value<bool>()).Value;
            }
        }
    }
    public static MaaPjskFrameworkStartupOptions FrameworkStartup
    {
        get { lock (LoggerSync) return _startup ??= CaptureFrameworkStartup(ReadSettings(), ConfigurationManager.Maa.Config); }
    }
    public static bool StartupFrameworkLoggingEnabled => FrameworkStartup.LoggingEnabled;

    private static JObject ReadSettings()
    {
        var path = Path.Combine(AppContext.BaseDirectory, "config", "performance-settings.json");
        try { return File.Exists(path) ? JObject.Parse(File.ReadAllText(path)) : new JObject(); }
        catch { return new JObject(); }
    }

    public static MaaPjskFrameworkStartupOptions CaptureFrameworkStartup(JObject settings, IReadOnlyDictionary<string, object> options)
    {
        bool logging = settings["framework_logging"]?.Type == JTokenType.Boolean
            && settings.Value<bool>("framework_logging")
            && (settings["disable_logging"] is null || settings["disable_logging"]?.Type == JTokenType.Boolean
                && !settings.Value<bool>("disable_logging"));
        int Integer(string key, int fallback, int maximum)
        {
            if (!options.TryGetValue(key, out var raw)) return fallback;
            long? value;
            try
            {
                value = raw switch
                {
                    int number => number,
                    long number => number,
                    JValue { Type: JTokenType.Integer } token => token.Value<long>(),
                    _ => null
                };
            }
            catch { return fallback; }
            return value is >= 0 && value <= maximum ? (int)value.Value : fallback;
        }
        bool debug = options.TryGetValue(ConfigurationKeys.ShowHitDraw, out var rawDebug)
            && (rawDebug is true || rawDebug is JValue { Type: JTokenType.Boolean } token && token.Value<bool>());
        return new(logging, Integer("stdout_level", 2, 7), Integer("draw_quality", 85, 100), debug);
    }

    private static void ApplyOption(Func<bool> apply, string name)
    {
        try
        {
            if (apply()) return;
            Warn($"MaaFramework 未接受{name}设置，继续运行");
        }
        catch (Exception error) { Warn($"MaaFramework {name}设置失败：{error.GetType().Name}，继续运行"); }
    }

    private static void Warn(string message)
    {
        try { LoggerHelper.Warning(message); }
        catch { /* 附加诊断通道故障不能阻断任务和必要清理。 */ }
    }

    public static MaaGlobal CreateGlobal()
    {
        var global = new MaaGlobal();
        // 已打开的文件流无法可靠热关闭，目录只在框架启动前按冻结快照应用一次。
        ApplyOption(() => global.SetOption_LogDir(StartupFrameworkLoggingEnabled
            ? Path.Combine(AppContext.BaseDirectory, "debug") : string.Empty), "日志目录");
        ApplyOption(() => global.SetOption_SaveDraw(false), "识别调试图");
        ApplyOption(() => global.SetOption(GlobalOption.SaveOnError, false), "错误截图");
        return global;
    }

    public static MaaToolkit CreateToolkit()
    {
        // 不调用 InitOption，避免旧 maa_option.json 的 logging=true 覆盖项目启动策略。
        var toolkit = new MaaToolkit(false);
        var options = FrameworkStartup;
        ApplyOption(() => MaaProcessor.Global.SetOption_StdoutLevel((LoggingLevel)options.StdoutLevel), "控制台日志等级");
        ApplyOption(() => MaaProcessor.Global.SetOption(GlobalOption.DrawQuality, options.DrawQuality), "调试图质量");
        ApplyOption(() => MaaProcessor.Global.SetOption_DebugMode(options.DebugMode), "内存调试信息");
        ApplyFramework();
        return toolkit;
    }

    public static void ApplyAgentEnvironment(ProcessStartInfo startInfo) =>
        startInfo.Environment[FrameworkLoggingEnvironment] = StartupFrameworkLoggingEnabled ? "1" : "0";

    public static bool ShouldSaveFrameworkFile(bool saved) => StartupFrameworkLoggingEnabled && !Disabled && saved;

    public static void Apply(bool disabled)
    {
        lock (LoggerSync)
        {
            _disabled = disabled;
            ApplyFramework();
            LoggerHelper.DisposeLogger();
            LoggerHelper.InitializeLogger();
        }
    }

    public static void ApplyFramework()
    {
        ApplyOption(() => MaaProcessor.Global.SetOption_SaveDraw(ShouldSaveFrameworkFile(
            ConfigurationManager.Maa.GetValue(ConfigurationKeys.SaveDraw, false))), "识别调试图");
        ApplyOption(() => MaaProcessor.Global.SetOption(GlobalOption.SaveOnError, ShouldSaveFrameworkFile(
            ConfigurationManager.Maa.GetValue(ConfigurationKeys.SaveOnError, true))), "错误截图");
    }
}
