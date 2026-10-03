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
    [ObservableProperty] private string _statusText = "保存后在下一次启动任务时生效。";
    [ObservableProperty] private string _calibrationText = "尚未生成已通过验证的校准配置。";

    private static string ConfigPath => Path.Combine(AppContext.BaseDirectory, "config", "performance-settings.json");

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
                    + $"PERFECT {profile["validation"]?.Value<double>("perfect_rate"):P2}"));
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
        if (Instances.InstanceTabBarViewModel.Tabs.Any(tab => tab.IsRunning))
        {
            StatusText = "请先停止演出任务，再保存演奏设置。";
            return;
        }
        if (EngineIndex is < 0 or > 1 || TouchOffsetMs is < -300 or > 300 || BonusIndex is < 0 or > 11)
        {
            StatusText = "触控偏移须为 -300 到 300 ms，每局体力须沿用游戏设置或为 0 到 10。";
            return;
        }
        try
        {
            var value = new JObject
            {
                ["schema_version"] = 1, ["engine"] = EngineIndex == 1 ? "native" : "legacy",
                ["touch_offset_ms"] = TouchOffsetMs,
                ["bonus_consumption"] = BonusIndex == 0 ? new JValue("current") : new JValue(BonusIndex - 1),
                ["use_calibration_profile"] = UseCalibrationProfile
            };
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
            StatusText = "演奏设置已保存，下次启动单人谱面任务生效。";
        }
        catch (Exception error)
        {
            StatusText = $"保存演奏设置失败：{error.Message}";
        }
    }
}
