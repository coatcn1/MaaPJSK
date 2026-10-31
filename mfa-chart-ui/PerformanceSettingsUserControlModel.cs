using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using MFAAvalonia.Helper;
using Newtonsoft.Json.Linq;
using System;
using System.IO;
using System.Linq;
using System.Text;
using System.Globalization;
using System.Collections.Generic;
using Avalonia.Threading;

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

    [ObservableProperty] private string _touchOffsetText = "0";
    [ObservableProperty] private string _retryCountText = "0";
    private bool _loading;
    private bool _ready;
    private bool _dirty;
    private bool _saved;
    private bool _active;
    private bool _loadFailed;
    private readonly DispatcherTimer _saveTimer;
    private static readonly List<PerformanceSettingsUserControlModel> Editors = new();
    // 所有窗口共享同一编辑快照，切页失败后重新打开仍可修复，避免旧窗口覆盖新值。
    public static PerformanceSettingsUserControlModel Shared { get; } = new();

    public PerformanceSettingsUserControlModel()
    {
        _saveTimer = new DispatcherTimer { Interval = TimeSpan.FromMilliseconds(600) };
        _saveTimer.Tick += (_, _) => { _saveTimer.Stop(); FlushPending(); };
        PropertyChanged += (_, args) =>
        {
            if (_loading || !_ready) return;
            if (args.PropertyName is nameof(TouchOffsetMs)) TouchOffsetText = TouchOffsetMs.ToString(CultureInfo.InvariantCulture);
            if (args.PropertyName is nameof(PlayFailureRetryCount)) RetryCountText = PlayFailureRetryCount.ToString(CultureInfo.InvariantCulture);
            if (args.PropertyName is not (nameof(EngineIndex) or nameof(TouchOffsetMs) or nameof(BonusIndex)
                or nameof(UseCalibrationProfile) or nameof(CooperativeGameTimingFeedback) or nameof(DisableLogging)
                or nameof(PlayFailureRetryCount) or nameof(TouchOffsetText) or nameof(RetryCountText))) return;
            _dirty = true;
            StatusText = "修改尚未保存，停止编辑后自动保存。";
            _saveTimer.Stop();
            _saveTimer.Start();
        };
    }

    public void Activate()
    {
        _active = true;
        if (!Editors.Contains(this)) Editors.Add(this);
        if (!_ready && !_dirty) Refresh();
    }

    public void Deactivate()
    {
        _active = false;
        FlushPending();
        // 保存失败时保留编辑器强引用，切页后仍阻止使用旧配置启动任务。
        if (!_dirty && !ReferenceEquals(this, Shared)) Editors.Remove(this);
    }

    public bool FlushPending()
    {
        _saveTimer.Stop();
        if (!_dirty) return true;
        Save();
        return _saved;
    }

    public static bool FlushBeforeTaskStart()
    {
        // 启动入口在 UI 线程、持有维护门锁时调用，先落盘再取得执行租约。
        return Editors.ToArray().All(editor => editor.FlushPending());
    }

    private static string ConfigPath => Path.Combine(AppContext.BaseDirectory, "config", "performance-settings.json");

    private static string FeedbackText(JToken? token) => token?.Type == JTokenType.Integer
        && token.Value<long>() >= 0 ? token.ToString() : "未记录";

    [RelayCommand]
    private void Refresh()
    {
        if (_dirty && !_loadFailed && !FlushPending()) return;
        _loading = true;
        _loadFailed = false;
        try
        {
            if (File.Exists(ConfigPath))
            {
                var value = JObject.Parse(File.ReadAllText(ConfigPath));
                if (value.Value<int?>("schema_version") != 1)
                    throw new InvalidDataException("演奏设置版本无效");
                var engineIndex = value.Value<string>("engine") switch
                {
                    "legacy" => 0, "native" => 1, _ => throw new InvalidDataException("演奏引擎无效")
                };
                var touchOffsetMs = value.Value<int>("touch_offset_ms");
                var bonusIndex = value.Value<string>("bonus_consumption") == "current" ? 0 : value.Value<int>("bonus_consumption") + 1;
                var useCalibrationProfile = value.Value<bool>("use_calibration_profile");
                var feedback = value["cooperative_game_timing_feedback"];
                if (feedback is not null && feedback.Type != JTokenType.Boolean)
                    throw new InvalidDataException("协力 FAST / LATE 微调开关必须为布尔值");
                var cooperativeGameTimingFeedback = feedback?.Value<bool>() ?? false;
                var logging = value["disable_logging"];
                if (logging is not null && logging.Type != JTokenType.Boolean)
                    throw new InvalidDataException("禁用日志开关必须为布尔值");
                var disableLogging = logging?.Value<bool>() ?? false;
                var retry = value["play_failure_retry_count"];
                if (retry is not null && (retry.Type != JTokenType.Integer || retry.Value<long>() is < 0 or > 99))
                    throw new InvalidDataException("中途失败重试次数必须为 0 到 99 的整数");
                var playFailureRetryCount = retry?.Value<int>() ?? 0;
                if (touchOffsetMs is < -300 or > 300 || bonusIndex is < 0 or > 11)
                    throw new InvalidDataException("触控偏移或体力数量越界");
                EngineIndex = engineIndex;
                TouchOffsetMs = touchOffsetMs;
                BonusIndex = bonusIndex;
                UseCalibrationProfile = useCalibrationProfile;
                CooperativeGameTimingFeedback = cooperativeGameTimingFeedback;
                DisableLogging = disableLogging;
                PlayFailureRetryCount = playFailureRetryCount;
            }
            TouchOffsetText = TouchOffsetMs.ToString(CultureInfo.InvariantCulture);
            RetryCountText = PlayFailureRetryCount.ToString(CultureInfo.InvariantCulture);
            _ready = true;
            var profileRoot = Path.Combine(AppContext.BaseDirectory, "config", "calibration-profiles");
            var profiles = Directory.Exists(profileRoot)
                ? Directory.GetFiles(profileRoot, "*.json").Select(path => JObject.Parse(File.ReadAllText(path)))
                    .Where(profile => profile.Value<bool?>("accepted") == true).ToArray()
                : Array.Empty<JObject>();
            CalibrationText = profiles.Length == 0 ? "尚未生成已通过验证的校准配置。"
                : string.Join(Environment.NewLine, profiles.Select(profile =>
                    $"{profile.Value<string>("difficulty")?.ToUpperInvariant()}：校准偏移 {profile.Value<int>("offset_ms")} ms · "
                    + $"FAST {FeedbackText(profile["validation"]?["fast"])} / LATE {FeedbackText(profile["validation"]?["late"])}"));
            _dirty = false;
            StatusText = "已读取保存的演奏设置。";
        }
        catch (Exception error)
        {
            _loadFailed = true;
            StatusText = $"读取演奏设置失败：{error.Message}";
        }
        finally { _loading = false; }
    }

    [RelayCommand]
    private void Save()
    {
        _saveTimer.Stop();
        _saved = false;
        if (_loadFailed)
        {
            StatusText = "修改未保存：请先修复配置并刷新，不能覆盖读取失败的演奏设置。";
            return;
        }
        if (_ready)
        {
            if (!int.TryParse(TouchOffsetText, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var offset)
                || !int.TryParse(RetryCountText, NumberStyles.None, CultureInfo.InvariantCulture, out var retry))
            {
                StatusText = "修改未保存：触控偏移和重试次数必须填写完整整数。";
                return;
            }
            _loading = true;
            try { TouchOffsetMs = offset; PlayFailureRetryCount = retry; }
            finally { _loading = false; }
        }
        if (!MaaPjskMaintenance.TryBegin())
        {
            StatusText = "修改未保存：请先停止演出任务或等待维护结束，再重试保存。";
            return;
        }
        try
        {
        if (EngineIndex is < 0 or > 1 || TouchOffsetMs is < -300 or > 300 || BonusIndex is < 0 or > 11
            || PlayFailureRetryCount is < 0 or > 99)
        {
            StatusText = "修改未保存：触控偏移须为 -300 到 300 ms，每局体力须沿用游戏设置或为 0 到 10，中途失败重试为 0 到 99。";
            return;
        }
        var previousLogging = MaaPjskDiagnostics.Disabled;
        try
        {
            // 保留本页不认识的扩展字段，损坏文件须显式修复，不能以默认值覆盖。
            var value = File.Exists(ConfigPath) ? JObject.Parse(File.ReadAllText(ConfigPath)) : new JObject();
            var edited = new JObject
            {
                ["schema_version"] = 1, ["engine"] = EngineIndex == 1 ? "native" : "legacy",
                ["touch_offset_ms"] = TouchOffsetMs,
                ["bonus_consumption"] = BonusIndex == 0 ? new JValue("current") : new JValue(BonusIndex - 1),
                ["use_calibration_profile"] = UseCalibrationProfile,
                ["cooperative_game_timing_feedback"] = CooperativeGameTimingFeedback,
                ["disable_logging"] = DisableLogging, ["play_failure_retry_count"] = PlayFailureRetryCount
            };
            value.Merge(edited);
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
            _dirty = false;
            _saved = true;
            if (!_active && !ReferenceEquals(this, Shared)) Editors.Remove(this);
            StatusText = "演奏设置已自动保存，下次启动任务生效。";
        }
        catch (Exception error)
        {
            try { MaaPjskDiagnostics.Apply(previousLogging); } catch { }
            StatusText = $"修改未保存，保存演奏设置失败：{error.Message}";
        }
        }
        finally { MaaPjskMaintenance.End(); }
    }
}
