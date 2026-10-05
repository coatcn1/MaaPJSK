from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from project_sekai.chart_player import SLIDE_NODE_TOUCH_PLAN_VERSION, TOUCH_PLAN_VERSION, compile_touches
from project_sekai.sus_chart import parse_sus


def serialized(events):
    return json.dumps([asdict(event) for event in events], ensure_ascii=False, separators=(",", ":"))


def main():
    parser = argparse.ArgumentParser(description="只读 SUS 并离线比较滑条节点采样，不连接设备")
    parser.add_argument("sus", type=Path)
    parser.add_argument("--baseline-touch-plan", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    chart = parse_sus(args.sus.read_text(encoding="utf-8-sig"))
    baseline = compile_touches(chart)
    candidate = compile_touches(chart, sample_slide_nodes=True)
    baseline_set = set(baseline)
    extra = [asdict(event) for event in candidate if event not in baseline_set]
    output = {"baseline_version": TOUCH_PLAN_VERSION, "candidate_version": SLIDE_NODE_TOUCH_PLAN_VERSION,
              "baseline_actions": len(baseline), "candidate_actions": len(candidate),
              "baseline_preserved": set(baseline).issubset(candidate), "added_moves": extra,
              "baseline_sha256": hashlib.sha256(serialized(baseline).encode()).hexdigest(),
              "candidate_sha256": hashlib.sha256(serialized(candidate).encode()).hexdigest(),
              "device_acceptance": "未进行；离线采样差异不能证明 MISS 已解决"}
    if args.baseline_touch_plan:
        # 本轮 trace 的序号只用于存储，比较时保留全部真实触控字段和事件顺序。
        recorded = [{key: row[key] for key in ("time", "order", "contact", "kind", "x", "y")}
                    for line in args.baseline_touch_plan.read_text(encoding="utf-8-sig").splitlines()
                    if line.strip() for row in [json.loads(line)]]
        output["recorded_baseline_matches"] = recorded == [asdict(event) for event in baseline]
    text = json.dumps(output, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
