from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from .chart_catalog import _write_json


@dataclass(frozen=True)
class PerformanceSettings:
    schema_version: int = 1
    engine: str = "legacy"
    touch_offset_ms: int = 0
    bonus_consumption: str | int = "current"
    use_calibration_profile: bool = True

    @classmethod
    def load(cls, path: Path):
        if not path.is_file():
            return cls()
        values = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict) or values.get("schema_version") != 1:
            raise ValueError("演奏设置版本无效，请在 MFA 演奏设置中重新保存")
        result = cls(**{key: values[key] for key in cls.__dataclass_fields__})
        if (result.engine not in {"legacy", "native"} or type(result.touch_offset_ms) is not int
                or not -300 <= result.touch_offset_ms <= 300 or type(result.use_calibration_profile) is not bool):
            raise ValueError("演奏引擎或谱面触控偏移无效")
        bonus = result.bonus_consumption
        if bonus != "current" and (type(bonus) is not int or not 0 <= bonus <= 10):
            raise ValueError("每局体力消耗必须沿用游戏设置或指定 0 到 10")
        return result


def migrate_settings(runtime: Path):
    path = runtime / "config/performance-settings.json"
    if path.exists():
        PerformanceSettings.load(path)
    else:
        settings = PerformanceSettings()
        application = runtime / "appsettings.json"
        instance_id = json.loads(application.read_text(encoding="utf-8-sig")).get("Instances.LastActive", "default") if application.exists() else "default"
        if not isinstance(instance_id, str) or not instance_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in instance_id):
            raise ValueError("保存的 MFA 实例 ID 无效")
        instance = runtime / "config/instances" / f"{instance_id}.json"
        if instance.is_file():
            value = json.loads(instance.read_text(encoding="utf-8-sig"))
            tasks = value.get("TaskItems", [])
            solo = next((task for task in tasks if task.get("name") == "SoloChartLive"), {})
            options = {option["name"]: option for option in solo.get("option", [])}
            offset = options.get("SoloChartLiveOffset", {}).get("data", {}).get("Value", 0)
            bonus = options.get("SoloChartLiveBonusConsumption", {}).get("index", 0)
            settings = PerformanceSettings(touch_offset_ms=int(offset), bonus_consumption="current" if bonus == 0 else int(bonus) - 1)
        _write_json(path, asdict(settings))
        PerformanceSettings.load(path)
    # 已迁移后去掉实例缓存的旧字段，其他任务选择、难度和次数原样保留。
    for instance in (runtime / "config/instances").glob("*.json"):
        value = json.loads(instance.read_text(encoding="utf-8-sig"))
        changed = False
        for task in value.get("TaskItems", []):
            if task.get("name") == "SoloChartLive":
                before = task.get("option", [])
                after = [option for option in before if option.get("name") not in {"SoloChartLiveOffset", "SoloChartLiveBonusConsumption"}]
                if after != before:
                    task["option"], changed = after, True
        if changed:
            _write_json(instance, value)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate-root", type=Path, required=True)
    migrate_settings(parser.parse_args().migrate_root)
