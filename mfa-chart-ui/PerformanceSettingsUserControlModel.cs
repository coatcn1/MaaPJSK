using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using System;
using System.IO;
using System.Linq;
using System.Text;

namespace MFAAvalonia.ViewModels.UsersControls.Settings;

public sealed partial class PerformanceSettingsUserControlModel : ViewModelBase
{
    [ObservableProperty] private int _engineIndex;
    [ObservableProperty] private int _touchOffsetMs;
    [ObservableProperty] private int _bonusIndex;
    [ObservableProperty] private bool _useCalibrationProfile = true;
    [ObservableProperty] private bool _cooperativeGameTimingFeedback;
    [ObservableProperty] private bool _disableLogging;
    [ObservableProperty] private int _playFailureRetryCount;
    [ObservableProperty] private string _statusText = "保存后在下一次启动任务时生效。";
    [ObservableProperty] private string _calibrationText = "尚未生成已通过验证的校准配置。";

    private static string ConfigPath => Path.Combine(AppContext.BaseDirectory, "config", "performance-settings.json");

    private static string FeedbackText(JToken? token) => token?.Type == JTokenType.Integer
        && token.Value<long>() >= 0 ? token.ToString() : "未记录";

    [RelayCommand]
    private void Refresh()
    {
        try
        {
            if (File.Exists(ConfigPath))
            {
                var value = JObject.Parse(File.ReadAllText(ConfigPath));
                if (value.Value<int?>("schema_version") != 1)
                    throw new InvalidDataException("演奏设置版本无效");
                EngineIndex = value.Value<string>("engine") switch
                {
                    "legacy" => 0, "native" => 1, _ => throw new InvalidDataException("演奏引擎无效")
                };
                TouchOffsetMs = value.Value<int>("touch_offset_ms");
                BonusIndex = value.Value<string>("bonus_consumption") == "current" ? 0 : value.Value<int>("bonus_consumption") + 1;
                UseCalibrationProfile = value.Value<bool>("use_calibration_profile");
                var feedback = value["cooperative_game_timing_feedback"];
                if (feedback is not null && feedback.Type != JTokenType.Boolean)
                    throw new InvalidDataException("协力 FAST / LATE 微调开关必须为布尔值");
                CooperativeGameTimingFeedback = feedback?.Value<bool>() ?? false;
                var logging = value["disable_logging"];
                if (logging is not null && logging.Type != JTokenType.Boolean)
                    throw new InvalidDataException("禁用日志开关必须为布尔值");
                DisableLogging = logging?.Value<bool>() ?? false;
                var retry = value["play_failure_retry_count"];
                if (retry is not null && (retry.Type != JTokenType.Integer || retry.Value<long>() is < 0 or > 99))
                    throw new InvalidDataException("中途失败重试次数必须为 0 到 99 的整数");
                PlayFailureRetryCount = retry?.Value<int>() ?? 0;
                if (TouchOffsetMs is < -300 or > 300 || BonusIndex is < 0 or > 11)
                    throw new InvalidDataException("触控偏移或体力数量越界");
            }
            var profileRoot = Path.Combine(AppContext.BaseDirectory, "config", "calibration-profiles");
            var profiles = Directory.Exists(profileRoot)
                ? Directory.GetFiles(profileRoot, "*.json").Select(path => JObject.Parse(File.ReadAllText(path)))
                    .Where(profile => profile.Value<bool?>("accepted") == true).ToArray()
                : Array.Empty<JObject>();
            CalibrationText = profiles.Length == 0 ? "尚未生成已通过验证的校准配置。"
                : string.Join(Environment.NewLine, profiles.Select(profile =>
                    $"{profile.Value<string>("difficulty")?.ToUpperInvariant()}：校准偏移 {profile.Value<int>("offset_ms")} ms · "
                    + $"FAST {FeedbackText(profile["validation"]?["fast"])} / LATE {FeedbackText(profile["validation"]?["late"])}"));
            StatusText = "已读取保存的演奏设置。";
        }
        catch (Exception error)
        {
            StatusText = $"读取演奏设置失败：{error.Message}";
        }
    }

    [RelayCommand]
    private void Save()
    {
        if (!MaaPjskMaintenance.TryBegin())
        {
            StatusText = "请先停止演出任务，再保存演奏设置。";
            return;
        }
        try
        {
        if (EngineIndex is < 0 or > 1 || TouchOffsetMs is < -300 or > 300 || BonusIndex is < 0 or > 11
            || PlayFailureRetryCount is < 0 or > 99)
        {
            StatusText = "触控偏移须为 -300 到 300 ms，每局体力须沿用游戏设置或为 0 到 10，中途失败重试为 0 到 99。";
            return;
        }
        var previousLogging = MaaPjskDiagnostics.Disabled;
        try
        {
            var value = new JObject
            {
                ["schema_version"] = 1, ["engine"] = EngineIndex == 1 ? "native" : "legacy",
                ["touch_offset_ms"] = TouchOffsetMs,
                ["bonus_consumption"] = BonusIndex == 0 ? new JValue("current") : new JValue(BonusIndex - 1),
                ["use_calibration_profile"] = UseCalibrationProfile,
                ["cooperative_game_timing_feedback"] = CooperativeGameTimingFeedback,
                ["disable_logging"] = DisableLogging, ["play_failure_retry_count"] = PlayFailureRetryCount
            };
            MaaPjskDiagnostics.Apply(DisableLogging);
            Directory.CreateDirectory(Path.GetDirectoryName(ConfigPath)!);
            // 与任务启动读取并行时只会得到完整旧值或完整新值，不暴露写到一半的 JSON。
            var temporary = ConfigPath + "." + Guid.NewGuid().ToString("N") + ".tmp";
            try
            {
                File.WriteAllText(temporary, value.ToString(), new UTF8Encoding(false));
                File.Move(temporary, ConfigPath, overwrite: true);
            }
            finally
            {
                if (File.Exists(temporary)) File.Delete(temporary);
            }
            StatusText = "演奏设置已保存，下次启动谱面演奏任务生效。";
        }
        catch (Exception error)
        {
            try { MaaPjskDiagnostics.Apply(previousLogging); } catch { }
            StatusText = $"保存演奏设置失败：{error.Message}";
        }
        }
        finally { MaaPjskMaintenance.End(); }
    }
}
